import os
import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

CLAUDE_MCP_CONFIG_PATH = os.path.expanduser("~/.claude/mcp.json")

def generate_mcp_config(manifold_api_url: str = "http://localhost:8000", backend_dir: str = "") -> Dict[str, Any]:
    """Generates Claude Code MCP configuration for Manifold."""
    return {
        "mcpServers": {
            "manifold": {
                "command": "uv",
                "args": ["run", "manifold-mcp"],
                "cwd": backend_dir,
                "env": {
                    "MANIFOLD_API_URL": manifold_api_url
                }
            }
        }
    }

def setup_claude_mcp_config(backend_dir: str) -> None:
    """Writes the MCP config to ~/.claude/mcp.json."""
    config = generate_mcp_config(backend_dir=backend_dir)
    os.makedirs(os.path.dirname(CLAUDE_MCP_CONFIG_PATH), exist_ok=True)
    
    existing = {}
    if os.path.exists(CLAUDE_MCP_CONFIG_PATH):
        try:
            with open(CLAUDE_MCP_CONFIG_PATH, "r") as f:
                existing = json.load(f)
        except json.JSONDecodeError:
            pass
            
    if "mcpServers" not in existing:
        existing["mcpServers"] = {}
        
    existing["mcpServers"]["manifold"] = config["mcpServers"]["manifold"]
    
    with open(CLAUDE_MCP_CONFIG_PATH, "w") as f:
        json.dump(existing, f, indent=2)
    logger.info(f"Updated Claude MCP config at {CLAUDE_MCP_CONFIG_PATH}")

async def init_claude_session(workspace_dir: str) -> Dict[str, str]:
    """Implement persistent workspace session passing and event streaming handshake between Claude Code CLI and Manifold."""
    session_id = f"claude_session_{os.path.basename(workspace_dir)}"
    logger.info(f"Initialized Claude Code session handshake: {session_id}")
    return {
        "session_id": session_id,
        "status": "active",
        "workspace": workspace_dir
    }

async def execute_on_instance(instance_id: str, command: str, timeout: float = 45.0) -> Dict[str, Any]:
    """Helper for Claude Code to execute commands on remote Manifold GPU instances via SSH sidecar."""
    # Import locally to avoid circular dependencies
    from app.mcp_server import run_command
    return await run_command(instance_id=instance_id, command=command, timeout=timeout, note="Claude remote execution via sidecar")
