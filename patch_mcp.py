import re

with open("backend/app/mcp_server.py", "r") as f:
    content = f.read()

# Update launch_gpu tool
content = re.sub(
    r"idle_timeout_seconds: float = None,",
    r"idle_timeout_seconds: float = None,\n        provider: str = 'lambda',",
    content
)

content = re.sub(
    r"idle_timeout_seconds=idle_timeout_seconds,\n        \)",
    r"idle_timeout_seconds=idle_timeout_seconds,\n            provider=provider,\n        )",
    content
)

with open("backend/app/mcp_server.py", "w") as f:
    f.write(content)

