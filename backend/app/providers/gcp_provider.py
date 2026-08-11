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
    """GCP provider whose live API is not wired up yet.

    An operator can set GCP_PROJECT_ID before the integration lands, and
    nothing here may take down the paths that sweep every provider
    (/instances, sync, adoption). So each method answers with the strongest
    true statement it can make:

    - CATALOG (list_instance_types): degrades to empty. "You cannot launch
      GCP types yet" is simply true, and an empty catalog says exactly that.
    - LIVENESS (list_instances, get_instance): raises ProviderUnavailable.
      Empty here would NOT be true — it would claim the account is idle when
      we never looked, and the reconcile sweep would then close every GCP
      launch row as terminated. The sweep catches this type per provider and
      skips GCP's rows while Lambda reconciles normally.
    - WRITES (launch/terminate/ssh key): explicit user actions, so they
      raise a typed, catchable ProviderError.
    """

    _NOT_WIRED = "Real GCP API integration pending credentials"

    def __init__(self, project_id: str, default_zone: str, credentials_file: Optional[str] = None):
        self.project_id = project_id
        self.default_zone = default_zone
        self.credentials_file = credentials_file
        self._warned = False

    def _warn_once(self) -> None:
        """Log the not-yet-implemented notice a single time per provider, so
        a 30s adoption sweep can't fill the log with the same line forever."""
        if not self._warned:
            logger.warning(
                "GCP project %r is set but the live API is not implemented "
                "yet; GCP is skipped by every sweep. %s",
                self.project_id, self._NOT_WIRED,
            )
            self._warned = True

    async def list_instance_types(self) -> List[CloudInstanceTypeSpec]:
        if not self.project_id:
            return []
        self._warn_once()
        return []

    async def list_instances(self, *, fresh: bool = False) -> List[CloudInstanceInfo]:
        # GCP fetches live data on every call; fresh is accepted for the
        # CloudProvider contract and ignored.
        if not self.project_id:
            # No project configured: there is genuinely nothing to run on
            # GCP, so empty is the truth rather than an absence of an answer.
            return []
        self._warn_once()
        raise ProviderUnavailable(self._NOT_WIRED)

    async def get_instance(self, instance_id: str) -> Optional[CloudInstanceInfo]:
        if not self.project_id:
            return None
        self._warn_once()
        raise ProviderUnavailable(self._NOT_WIRED)

    async def launch_instance(self, region: str, instance_type: str, ssh_key_names: List[str], filesystem_names: List[str], name: str = "", user_data: str = "") -> str:
        raise ProviderError("GCP launch not yet supported")

    async def terminate_instance(self, instance_id: str) -> bool:
        raise ProviderError("GCP terminate not yet supported")

    async def ensure_ssh_key(self, public_key: str, name: str) -> str:
        raise ProviderError("GCP SSH key management not yet supported")
