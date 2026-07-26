from typing import List, Optional
from datetime import datetime, timezone
from app.lambda_api import LambdaClient
from app.providers.base import CloudProvider, CloudInstanceTypeSpec, CloudInstanceInfo

class LambdaProvider(CloudProvider):
    def __init__(self, client: LambdaClient):
        self.client = client

    async def list_instance_types(self) -> List[CloudInstanceTypeSpec]:
        types = await self.client.list_instance_types()
        specs = []
        for name, info in types.items():
            specs.append(
                CloudInstanceTypeSpec(
                    name=info.name,
                    description=info.description,
                    vcpus=info.specs.get("vcpus", 0),
                    ram_gb=info.specs.get("memory_gib", 0),
                    gpus=info.specs.get("gpus", 0),
                    gpu_type=info.gpu_description,
                    gpu_ram_gb=0, # Not provided strictly by API
                    price_cents_per_hour=info.price_cents_per_hour,
                    regions_available=info.regions_with_capacity,
                )
            )
        return specs

    async def list_instances(self) -> List[CloudInstanceInfo]:
        instances = await self.client.list_instances()
        return [
            CloudInstanceInfo(
                id=i.id,
                provider="lambda",
                name=i.name,
                instance_type=i.instance_type,
                region=i.region,
                ip_address=i.ip,
                status=i.status,
                created_at=datetime.now(timezone.utc), # mock created at for lambda since it's not in InstanceInfo
                price_cents_per_hour=i.hourly_rate_cents,
                gpu_description=i.gpu_description,
                file_system_names=i.file_system_names,
            ) for i in instances
        ]

    async def get_instance(self, instance_id: str) -> Optional[CloudInstanceInfo]:
        try:
            i = await self.client.get_instance(instance_id)
            return CloudInstanceInfo(
                id=i.id,
                provider="lambda",
                name=i.name,
                instance_type=i.instance_type,
                region=i.region,
                ip_address=i.ip,
                status=i.status,
                created_at=datetime.now(timezone.utc),
                price_cents_per_hour=i.hourly_rate_cents,
                gpu_description=i.gpu_description,
                file_system_names=i.file_system_names,
            )
        except Exception:
            return None

    async def launch_instance(self, region: str, instance_type: str, ssh_key_names: List[str], filesystem_names: List[str], name: str = "", user_data: str = "") -> str:
        return await self.client.launch_instance(
            instance_type=instance_type,
            region=region,
            ssh_key_names=ssh_key_names,
            filesystem_names=filesystem_names,
            name=name,
            user_data=user_data,
        )

    async def terminate_instance(self, instance_id: str) -> bool:
        await self.client.terminate_instance(instance_id)
        return True

    async def ensure_ssh_key(self, public_key: str, name: str) -> str:
        # Lambda API does not currently support creating SSH keys
        # For Lambda we just require the user to configure an existing one
        keys = await self.client.list_ssh_keys()
        for k in keys:
            if k.name == name:
                return k.name
        if keys:
            return keys[0].name
        return name
