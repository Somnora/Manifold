from typing import Dict, List, Tuple, Type
from app.providers.base import CloudProvider

class ProviderRegistry:
    def __init__(self):
        self._providers: Dict[str, CloudProvider] = {}
        self._active_provider_name: str = "lambda"

    def register(self, name: str, provider: CloudProvider):
        self._providers[name] = provider

    def get_provider(self, name: str) -> CloudProvider:
        if name not in self._providers:
            raise ValueError(f"Provider '{name}' not found")
        return self._providers[name]

    def items(self) -> List[Tuple[str, CloudProvider]]:
        """(name, provider) pairs for every registered provider.

        The public way to sweep all providers. Callers must NOT reach into
        the private `_providers` dict — this keeps the name available so a
        failing provider can be named in a log line and skipped.
        """
        return list(self._providers.items())

    def all_providers(self) -> List[CloudProvider]:
        """Every registered provider, order unspecified."""
        return list(self._providers.values())

    @property
    def active_provider(self) -> CloudProvider:
        return self.get_provider(self._active_provider_name)

    def set_active_provider(self, name: str):
        if name not in self._providers:
            raise ValueError(f"Provider '{name}' not found")
        self._active_provider_name = name
