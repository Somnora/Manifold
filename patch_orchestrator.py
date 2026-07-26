import re

with open("backend/app/orchestrator.py", "r") as f:
    content = f.read()

# Update Orchestrator.__init__ signature
content = re.sub(
    r"lambda_client: LambdaClient,",
    r"providers: 'ProviderRegistry',",
    content
)
content = re.sub(
    r"self\.client = lambda_client",
    r"self.providers = providers\n        self.client = providers.get_provider('lambda').client",
    content
)

# Update request_launch
content = re.sub(
    r"idle_timeout_seconds: float \| None = None,\n    \) -> dict:",
    r"idle_timeout_seconds: float | None = None,\n        provider: str = 'lambda',\n    ) -> dict:",
    content
)

# In request_launch, use self.providers.get_provider(provider) for instance stuff
content = re.sub(
    r"types = await self\.client\.list_instance_types\(\)",
    r"cloud_provider = self.providers.get_provider(provider)\n        types = {t.name: t for t in await cloud_provider.list_instance_types()}",
    content
)

# ssh keys:
content = re.sub(
    r"registered = \[k\.name for k in await self\.client\.list_ssh_keys\(\)\]",
    r"if provider == 'lambda':\n            registered = [k.name for k in await self.client.list_ssh_keys()]\n        else:\n            registered = [resolved_key]",
    content
)

# list_instances fresh=True
content = re.sub(
    r"running = \[i for i in await self\.client\.list_instances\(fresh=True\)",
    r"running = [i for i in await cloud_provider.list_instances() # fresh=True not strictly supported on CloudProvider, but list_instances fetches fresh for GCP",
    content
)

# update create_launch call to pass provider
content = re.sub(
    r"idle_timeout_seconds=idle_timeout_seconds,\n        \)",
    r"idle_timeout_seconds=idle_timeout_seconds,\n            provider=provider,\n        )",
    content
)

# In terminate
content = re.sub(
    r"await self\.client\.terminate_instance\(instance_id\)",
    r"launch = self.db.find_launch_by_instance(instance_id)\n        provider_name = launch['provider'] if launch else 'lambda'\n        await self.providers.get_provider(provider_name).terminate_instance(instance_id)",
    content
)

# In instances_with_state
content = re.sub(
    r"listed = await self\.client\.list_instances\(\)",
    r"listed = []\n        for p in self.providers._providers.values():\n            listed.extend(await p.list_instances())",
    content
)

# In _run_launch
content = re.sub(
    r"instance_id = await self\.client\.launch_instance\(",
    r"launch_rec = self.db.get_launch(plan.launch_id)\n                    p_name = launch_rec['provider'] if launch_rec else 'lambda'\n                    instance_id = await self.providers.get_provider(p_name).launch_instance(",
    content
)

# In _run_launch list_instance_types
content = re.sub(
    r"types = await self\.client\.list_instance_types\(\)",
    r"launch_rec = self.db.get_launch(plan.launch_id)\n            p_name = launch_rec['provider'] if launch_rec else 'lambda'\n            types = {t.name: t for t in await self.providers.get_provider(p_name).list_instance_types()}",
    content
)

# In _run_launch get_instance
content = re.sub(
    r"instance = await self\.client\.get_instance\(instance_id\)",
    r"launch_rec = self.db.get_launch(plan.launch_id)\n            p_name = launch_rec['provider'] if launch_rec else 'lambda'\n            instance = await self.providers.get_provider(p_name).get_instance(instance_id)",
    content
)

# In _adopt_orphans
content = re.sub(
    r"instances = await self\.client\.list_instances\(\)",
    r"instances = []\n            for p in self.providers._providers.values():\n                instances.extend(await p.list_instances())",
    content
)

with open("backend/app/orchestrator.py", "w") as f:
    f.write(content)
