from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from datetime import datetime

class ProviderError(Exception):
    """A cloud provider could not satisfy a request.

    Typed so callers (routes, orchestrator sweeps) can catch it cleanly
    instead of letting a bare NotImplementedError escape and take down an
    unrelated code path. Used for explicit, not-yet-supported user actions
    like launching on a provider whose API isn't wired up yet.
    """
    pass

class CloudInstanceTypeSpec(BaseModel):
    name: str
    description: str
    vcpus: int
    ram_gb: int
    gpus: int
    gpu_type: Optional[str] = None
    gpu_ram_gb: int
    price_cents_per_hour: int
    regions_available: List[str]

    @property
    def regions_with_capacity(self) -> List[str]:
        return self.regions_available

class CloudInstanceInfo(BaseModel):
    id: str
    provider: str
    name: str
    instance_type: str
    region: str
    ip_address: Optional[str] = None
    status: str
    created_at: datetime
    price_cents_per_hour: int
    ssh_key_name: Optional[str] = None
    gpu_description: str = ""
    gpu_type: Optional[str] = None
    file_system_names: List[str] = []

    @property
    def filesystem_names(self) -> List[str]:
        return self.file_system_names

    @property
    def ip(self) -> Optional[str]:
        return self.ip_address

    @property
    def hourly_rate_cents(self) -> int:
        return self.price_cents_per_hour

    @property
    def is_running(self) -> bool:
        return self.status in ("booting", "active", "unhealthy")

class CloudProvider(ABC):
    @abstractmethod
    async def list_instance_types(self) -> List[CloudInstanceTypeSpec]:
        pass

    @abstractmethod
    async def list_instances(self, *, fresh: bool = False) -> List[CloudInstanceInfo]:
        # fresh=True must bypass any client-side cache: spend guards call
        # this and must never decide on a stale snapshot. Providers that
        # always fetch live data may simply ignore the flag.
        pass

    @abstractmethod
    async def get_instance(self, instance_id: str) -> Optional[CloudInstanceInfo]:
        pass

    @abstractmethod
    async def launch_instance(self, region: str, instance_type: str, ssh_key_names: List[str], filesystem_names: List[str], name: str = "", user_data: str = "") -> str:
        pass

    @abstractmethod
    async def terminate_instance(self, instance_id: str) -> bool:
        pass

    @abstractmethod
    async def ensure_ssh_key(self, public_key: str, name: str) -> str:
        pass
