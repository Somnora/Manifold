import re

with open("backend/app/main.py", "r") as f:
    content = f.read()

# Add imports
if "from .providers.registry import ProviderRegistry" not in content:
    content = re.sub(
        r"from \.orchestrator import Orchestrator, LaunchRejected, TerminationBlocked",
        r"from .orchestrator import Orchestrator, LaunchRejected, TerminationBlocked\nfrom .providers.registry import ProviderRegistry\nfrom .providers.lambda_provider import LambdaProvider\nfrom .providers.gcp_provider import MockGCPProvider, RealGCPProvider",
        content
    )

# Update LaunchRequest
content = re.sub(
    r"idle_timeout_seconds: float \| None = None",
    r"idle_timeout_seconds: float | None = None\n    provider: str = 'lambda'",
    content
)

# Update request_launch call
content = re.sub(
    r"idle_timeout_seconds=req\.idle_timeout_seconds,",
    r"idle_timeout_seconds=req.idle_timeout_seconds,\n        provider=req.provider,",
    content
)

# Orchestrator instantiation
content = re.sub(
    r"orchestrator = Orchestrator\(\n        settings, lambda_client, db,",
    r"providers = ProviderRegistry()\n    providers.register('lambda', LambdaProvider(lambda_client))\n    if mock:\n        providers.register('gcp', MockGCPProvider())\n    else:\n        providers.register('gcp', RealGCPProvider(settings.gcp.project_id, settings.gcp.default_zone, settings.gcp.credentials_file))\n    orchestrator = Orchestrator(\n        settings, providers, db,",
    content
)

with open("backend/app/main.py", "w") as f:
    f.write(content)

