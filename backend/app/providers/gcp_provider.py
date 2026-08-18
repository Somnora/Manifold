import logging
import uuid
from typing import List, Optional, Dict
from datetime import datetime, timezone
from app.providers.base import (
    CloudProvider,
    CloudInstanceTypeSpec,
    CloudInstanceInfo,
    ProviderError,
    ProviderUnavailable,
)

logger = logging.getLogger("manifold.providers.gcp")

class GCPProvider(CloudProvider):
    # This serves as the base for Real/Mock GCP providers
    pass

class MockGCPProvider(GCPProvider):
    def __init__(self):
        self.instances: Dict[str, CloudInstanceInfo] = {}
        self._regions = ["us-central1", "us-east1", "us-west1", "europe-west4", "asia-east1"]
        self._types = {
            "g2-standard-4": CloudInstanceTypeSpec(
                name="g2-standard-4",
                description="1x NVIDIA L4 24GB VRAM",
                vcpus=4,
                ram_gb=16,
                gpus=1,
                gpu_type="L4",
                gpu_ram_gb=24,
                price_cents_per_hour=70,
                regions_available=self._regions
            ),
            "g2-standard-12": CloudInstanceTypeSpec(
                name="g2-standard-12",
                description="1x NVIDIA L4 24GB VRAM",
                vcpus=12,
                ram_gb=48,
                gpus=1,
                gpu_type="L4",
                gpu_ram_gb=24,
                price_cents_per_hour=115,
                regions_available=self._regions
            ),
            "a2-highgpu-1g": CloudInstanceTypeSpec(
                name="a2-highgpu-1g",
                description="1x NVIDIA A100 40GB VRAM",
                vcpus=12,
                ram_gb=85,
                gpus=1,
                gpu_type="A100",
                gpu_ram_gb=40,
                price_cents_per_hour=367,
                regions_available=self._regions
            ),
            "n1-standard-8-t4": CloudInstanceTypeSpec(
                name="n1-standard-8-t4",
                description="1x NVIDIA T4 16GB VRAM",
                vcpus=8,
                ram_gb=30,
                gpus=1,
                gpu_type="T4",
                gpu_ram_gb=16,
                price_cents_per_hour=35,
                regions_available=self._regions
            )
        }

    async def list_instance_types(self) -> List[CloudInstanceTypeSpec]:
        return list(self._types.values())

    async def list_instances(self, *, fresh: bool = False) -> List[CloudInstanceInfo]:
        # No cache here; the mock's dict is always current, so fresh is moot.
        return [i for i in self.instances.values() if i.status != "terminated"]

    async def get_instance(self, instance_id: str) -> Optional[CloudInstanceInfo]:
        return self.instances.get(instance_id)

    async def launch_instance(self, region: str, instance_type: str, ssh_key_names: List[str], filesystem_names: List[str], name: str = "", user_data: str = "") -> str:
        instance_id = uuid.uuid4().hex
        ts = self._types.get(instance_type)
        if not ts:
            raise ValueError(f"Unknown instance type: {instance_type}")
        self.instances[instance_id] = CloudInstanceInfo(
            id=instance_id,
            provider="gcp",
            name=name,
            instance_type=instance_type,
            region=region,
            ip_address="192.168.1.100",
            status="active",
            created_at=datetime.now(timezone.utc),
            price_cents_per_hour=ts.price_cents_per_hour,
            ssh_key_name=ssh_key_names[0] if ssh_key_names else None
        )
        return instance_id

    async def terminate_instance(self, instance_id: str) -> bool:
        if instance_id in self.instances:
            self.instances[instance_id].status = "terminated"
            return True
        return False

    async def ensure_ssh_key(self, public_key: str, name: str) -> str:
        return name

class RealGCPProvider(GCPProvider):
    """GCE over the operator's own Google credentials.

    AUTH IS ADC, NOT A PASTED KEY. google-cloud-compute resolves
    Application Default Credentials on its own: `gcloud auth
    application-default login` (a browser OAuth into the user's Google
    account, cached under ~/.config/gcloud) or GOOGLE_APPLICATION_CREDENTIALS
    pointing at a service-account file for headless installs. Manifold never
    sees, stores or logs a Google credential either way - the same rule the
    CLI brains follow.

    THE SHELF IS CURATED, THE AVAILABILITY IS LIVE. GCE has no Lambda-style
    "GPU types with prices" endpoint, so gcp_catalog.py names a dozen
    machine shapes and this class intersects them with ONE live
    acceleratorTypes.aggregatedList call: a zone appears only if Google says
    that accelerator exists there. Prices are dated list prices and every
    entry says so in price_basis (the money rule: exact or labelled).

    ONLY MANIFOLD'S OWN INSTANCES EXIST. Every launch carries the label
    manifold=true, and every list/get/terminate filters on it. A user's
    unrelated VMs in the same project must be invisible here - the reconcile
    sweep would otherwise adopt-or-close machines Manifold never launched,
    and terminate must never be able to name one.

    The SDK is synchronous; every call rides asyncio.to_thread. Imports are
    lazy so a backend without the package (or without GCP configured) boots
    exactly as before.
    """

    LABEL_KEY = "manifold"
    # Ubuntu because it consumes cloud-init from the `user-data` metadata
    # key, which is the same rider Lambda boots with - one bootstrap, two
    # providers. The driver block in cloud_init.py no-ops where drivers
    # already exist (Lambda) and installs them here.
    IMAGE = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
    BOOT_DISK_GB = 200
    # GPUs cannot live-migrate; GCE refuses GPU instances without this.
    SCHEDULING = {"on_host_maintenance": "TERMINATE", "automatic_restart": True}

    def __init__(self, project_id: str, default_zone: str,
                 credentials_file: Optional[str] = None,
                 public_key_fn=None):
        self.project_id = project_id
        self.default_zone = default_zone
        self.credentials_file = credentials_file
        # () -> str: the local SSH public key, resolved lazily by main.py
        # from the same private key ManagedConnection dials with. GCE takes
        # key MATERIAL in metadata, not a registered name like Lambda.
        self._public_key_fn = public_key_fn
        self._warned = False

    def unconfigured_reason(self) -> Optional[str]:
        """No project id means nothing here can work, and the catalog says
        so by being empty (list_instance_types returns [] rather than
        guessing). Said in words instead, so a launch that resolves to GCP
        before anyone has set it up refuses with the two steps that fix it
        rather than with an empty list of valid instance types."""
        if not self.project_id:
            return ("Google Cloud is not configured yet: Manifold has no GCP "
                    "project id, so it cannot list or launch anything there. "
                    "Run `gcloud auth application-default login`, then add "
                    "your project id under Settings -> Google Cloud "
                    "Configuration.")
        return None

    # -- SDK plumbing -------------------------------------------------------------

    def _compute(self):
        """The google.cloud.compute_v1 module, imported on first use.

        Lazy for two reasons: boot cost when GCP is unconfigured, and an
        importable error message when the package is missing rather than a
        backend that cannot start."""
        import os
        if self.credentials_file:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
                                  self.credentials_file)
        try:
            from google.cloud import compute_v1
        except ImportError as exc:
            raise ProviderUnavailable(
                "google-cloud-compute is not installed; run `uv sync` in "
                "backend/ to add it") from exc
        return compute_v1

    async def _run(self, fn, *args, **kwargs):
        """Run one sync SDK call off the event loop, translating auth
        failures into the sentence that fixes them."""
        import asyncio
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ProviderError:
            raise
        except Exception as exc:
            text = str(exc)
            lower = text.lower()
            # Two distinct auth failures, one fix each. Both are NORMAL
            # states (ADC refresh tokens hit Google's reauth window after
            # weeks), so they must read as the one command that fixes them,
            # not as a traceback - this exact wording was hit live at the
            # 2026-08-15 gate when a 21-day-old ADC token expired.
            if ("reauthentication" in lower
                    or type(exc).__name__ == "RefreshError"):
                raise ProviderUnavailable(
                    "Your Google login has expired (ADC refresh tokens "
                    "age out). Run `gcloud auth application-default login` "
                    "to sign in again; nothing else needs changing."
                ) from exc
            if "default credentials" in lower or "ADC" in text:
                raise ProviderUnavailable(
                    "No Google credentials found. Run `gcloud auth "
                    "application-default login` (browser OAuth, keyless) or "
                    "set GOOGLE_APPLICATION_CREDENTIALS to a service-account "
                    "file.") from exc
            raise

    # -- catalog ------------------------------------------------------------------

    async def _accelerator_zones(self) -> Dict[str, List[str]]:
        """accelerator-type id -> zones Google offers it in. ONE aggregated
        call covers the whole shelf."""
        compute = self._compute()

        def fetch():
            client = compute.AcceleratorTypesClient()
            out: Dict[str, List[str]] = {}
            # GAPIC does not promote `filter` (or much else) to keywords on
            # aggregated_list: pass the request object. A bare keyword call
            # raised TypeError, which read as a transient provider outage
            # and vetoed connection reaping - caught by the reconcile suite
            # before the gate could pay to find it.
            request = compute.AggregatedListAcceleratorTypesRequest(
                project=self.project_id)
            for zone_path, scoped in client.aggregated_list(request=request):
                if not scoped.accelerator_types:
                    continue
                zone = zone_path.rsplit("/", 1)[-1]
                for acc in scoped.accelerator_types:
                    if acc.deprecated and acc.deprecated.state:
                        continue
                    out.setdefault(acc.name, []).append(zone)
            return out

        return await self._run(fetch)

    async def list_instance_types(self) -> List[CloudInstanceTypeSpec]:
        if not self.project_id:
            return []
        from . import gcp_catalog
        try:
            zones = await self._accelerator_zones()
        except ProviderUnavailable:
            raise
        except Exception as exc:
            # A catalog that cannot be fetched is EMPTY, not invented; the
            # log carries the why. Sweeps and the launch form stay alive.
            logger.warning("GCP catalog unavailable: %s", exc)
            return []
        specs = []
        for entry in gcp_catalog.SHELF:
            specs.append(CloudInstanceTypeSpec(
                name=entry.name,
                description=(
                    f"{entry.accelerator_count}x NVIDIA {entry.gpu_type} "
                    f"{entry.gpu_ram_gb}GB VRAM"),
                vcpus=entry.vcpus,
                ram_gb=entry.ram_gb,
                gpus=entry.accelerator_count,
                gpu_type=entry.gpu_type,
                gpu_ram_gb=entry.gpu_ram_gb,
                price_cents_per_hour=entry.price_cents_per_hour,
                regions_available=gcp_catalog.zones_for(entry, zones),
            ))
        return specs

    # -- liveness -----------------------------------------------------------------

    def _to_info(self, inst, zone: str) -> CloudInstanceInfo:
        from . import gcp_catalog
        ip = None
        for nic in inst.network_interfaces:
            for ac in nic.access_configs:
                if ac.nat_i_p:
                    ip = ac.nat_i_p
        status = {
            "PROVISIONING": "booting", "STAGING": "booting",
            "RUNNING": "active", "REPAIRING": "booting",
            "STOPPING": "terminated", "SUSPENDED": "terminated",
            "TERMINATED": "terminated",
        }.get(inst.status, "booting")
        machine = inst.machine_type.rsplit("/", 1)[-1]
        shelf = next((e for e in gcp_catalog.SHELF
                      if e.machine_type == machine), None)
        return CloudInstanceInfo(
            id=inst.name,
            provider="gcp",
            name=inst.name,
            instance_type=shelf.name if shelf else machine,
            region=zone,
            ip_address=ip,
            status=status,
            created_at=datetime.now(timezone.utc),
            price_cents_per_hour=(shelf.price_cents_per_hour if shelf else 0),
            gpu_description=(gcp_catalog.gpu_description(shelf)
                             if shelf else ""),
            gpu_type=(shelf.gpu_type if shelf else None),
        )

    async def _find(self, instance_id: str):
        """(instance, zone) for a manifold-labelled instance, else None.
        Aggregated by name so restarts need no zone memory."""
        compute = self._compute()

        def fetch():
            client = compute.InstancesClient()
            request = compute.AggregatedListInstancesRequest(
                project=self.project_id, filter=f'name = "{instance_id}"')
            for zone_path, scoped in client.aggregated_list(request=request):
                for inst in (scoped.instances or []):
                    if inst.labels.get(self.LABEL_KEY) == "true":
                        return inst, zone_path.rsplit("/", 1)[-1]
            return None

        return await self._run(fetch)

    async def list_instances(self, *, fresh: bool = False) -> List[CloudInstanceInfo]:
        if not self.project_id:
            return []
        compute = self._compute()

        def fetch():
            client = compute.InstancesClient()
            found = []
            request = compute.AggregatedListInstancesRequest(
                project=self.project_id,
                filter=f'labels.{self.LABEL_KEY} = "true"')
            for zone_path, scoped in client.aggregated_list(request=request):
                zone = zone_path.rsplit("/", 1)[-1]
                for inst in (scoped.instances or []):
                    found.append((inst, zone))
            return found

        pairs = await self._run(fetch)
        return [self._to_info(i, z) for i, z in pairs
                if i.status != "TERMINATED"]

    async def get_instance(self, instance_id: str) -> Optional[CloudInstanceInfo]:
        if not self.project_id:
            return None
        pair = await self._find(instance_id)
        return self._to_info(*pair) if pair else None

    # -- writes -------------------------------------------------------------------

    async def launch_instance(self, region: str, instance_type: str,
                              ssh_key_names: List[str],
                              filesystem_names: List[str],
                              name: str = "", user_data: str = "") -> str:
        from . import gcp_catalog
        from .base import ProviderCapacityError
        if not self.project_id:
            raise ProviderError(
                "No GCP project configured. Set GCP_PROJECT_ID in .env (and "
                "authenticate with `gcloud auth application-default login`).")
        if filesystem_names:
            raise ProviderError(
                "GCP launches are scratch-only for now: Filestore is not "
                "wired up. Launch without a filesystem.")
        entry = gcp_catalog.BY_NAME.get(instance_type)
        if entry is None:
            raise ProviderError(
                f"'{instance_type}' is not on the GCP shelf. Types: "
                f"{', '.join(sorted(gcp_catalog.BY_NAME))}")
        zone = region if region.count("-") >= 2 else self.default_zone
        public_key = (self._public_key_fn() if self._public_key_fn else "")
        if not public_key:
            raise ProviderError(
                "No local SSH public key available. GCE takes key material "
                "in instance metadata (not a registered name); ensure "
                "ssh.private_key_path in config.yaml points at a readable "
                "key pair.")
        compute = self._compute()
        instance_name = name or f"manifold-{uuid.uuid4().hex[:10]}"

        def create():
            inst = compute.Instance()
            inst.name = instance_name
            inst.machine_type = (
                f"zones/{zone}/machineTypes/{entry.machine_type}")
            inst.labels = {self.LABEL_KEY: "true"}
            disk = compute.AttachedDisk()
            disk.boot = True
            disk.auto_delete = True
            disk.initialize_params = compute.AttachedDiskInitializeParams(
                source_image=self.IMAGE, disk_size_gb=self.BOOT_DISK_GB,
                disk_type=f"zones/{zone}/diskTypes/pd-balanced")
            inst.disks = [disk]
            nic = compute.NetworkInterface()
            nic.access_configs = [compute.AccessConfig(
                name="External NAT", type_="ONE_TO_ONE_NAT")]
            inst.network_interfaces = [nic]
            inst.scheduling = compute.Scheduling(
                on_host_maintenance="TERMINATE", automatic_restart=True)
            if entry.attach_count:
                inst.guest_accelerators = [compute.AcceleratorConfig(
                    accelerator_type=(
                        f"zones/{zone}/acceleratorTypes/{entry.accelerator}"),
                    accelerator_count=entry.attach_count)]
            items = [compute.Items(key="ssh-keys",
                                   value=f"ubuntu:{public_key}")]
            if user_data:
                items.append(compute.Items(key="user-data", value=user_data))
            inst.metadata = compute.Metadata(items=items)
            op = compute.InstancesClient().insert(
                project=self.project_id, zone=zone, instance_resource=inst)
            # Wait the operation out: quota and capacity failures surface on
            # the OPERATION, not the insert call, and returning an id for a
            # machine that will never exist would leave a launch row polling
            # forever.
            op.result(timeout=180)
            return instance_name

        try:
            return await self._run(create)
        except ProviderError:
            raise
        except Exception as exc:
            text = str(exc)
            upper = text.upper()
            if "RESOURCE_POOL_EXHAUSTED" in upper or "does not have enough resources" in text:
                raise ProviderCapacityError(
                    f"GCE has no {entry.gpu_type} capacity in {zone} right "
                    f"now: {text[:200]}") from exc
            if "QUOTA" in upper:
                raise ProviderError(
                    f"GPU quota exceeded for {entry.gpu_type} in "
                    f"{gcp_catalog.zone_to_region(zone)}. Fresh projects "
                    f"hold ZERO GPU quota; request an increase at "
                    f"https://console.cloud.google.com/iam-admin/quotas"
                    f"?project={self.project_id} (metric "
                    f"{gcp_catalog.quota_metric(entry)}). Google usually "
                    f"answers small requests in minutes to hours. "
                    f"({text[:200]})") from exc
            raise ProviderError(f"GCE launch failed: {text[:300]}") from exc

    async def terminate_instance(self, instance_id: str) -> bool:
        if not self.project_id:
            raise ProviderError("No GCP project configured.")
        pair = await self._find(instance_id)
        if pair is None:
            # Idempotent like Lambda's terminate: already gone is success.
            return True
        _inst, zone = pair
        compute = self._compute()

        def delete():
            op = compute.InstancesClient().delete(
                project=self.project_id, zone=zone, instance=instance_id)
            op.result(timeout=180)
            return True

        return await self._run(delete)

    async def ensure_ssh_key(self, public_key: str, name: str) -> str:
        # GCE has no key registry to ensure anything in: material rides
        # per-instance metadata at launch. Accepting the call keeps the
        # orchestrator provider-agnostic.
        return name

    # -- quota --------------------------------------------------------------------

    async def gpu_quota(self, region: str) -> List[Dict]:
        """GPU-relevant quota rows for one region plus the global GPU cap.

        This is the number that actually gates a first launch - fresh
        projects hold zero of every GPU metric - so the launch form shows
        it BEFORE the click instead of letting the operation fail after.
        """
        if not self.project_id:
            return []
        compute = self._compute()

        def fetch():
            rows = []
            project = compute.ProjectsClient().get(project=self.project_id)
            for q in project.quotas:
                if "GPU" in q.metric:
                    rows.append({"metric": q.metric, "limit": q.limit,
                                 "usage": q.usage, "scope": "global"})
            reg = compute.RegionsClient().get(
                project=self.project_id, region=region)
            for q in reg.quotas:
                if "GPU" in q.metric:
                    rows.append({"metric": q.metric, "limit": q.limit,
                                 "usage": q.usage, "scope": region})
            return rows

        return await self._run(fetch)
