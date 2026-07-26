import os
import re

SSH_CONFIG_PATH = os.path.expanduser("~/.ssh/config")

def _get_block_delimiters(instance_id: str):
    start = f"# >>> manifold managed {instance_id} >>>"
    end = f"# <<< manifold managed {instance_id} <<<"
    return start, end

def write_ssh_config_block(instance_id: str, host_ip: str, ssh_key_path: str, host_keys_file_path: str):
    start, end = _get_block_delimiters(instance_id)
    block = f"""{start}
Host manifold-{instance_id}
    HostName {host_ip}
    User ubuntu
    IdentityFile {ssh_key_path}
    StrictHostKeyChecking yes
    UserKnownHostsFile {host_keys_file_path}
{end}
"""
    
    os.makedirs(os.path.dirname(SSH_CONFIG_PATH), exist_ok=True)
    
    if not os.path.exists(SSH_CONFIG_PATH):
        with open(SSH_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(block)
        os.chmod(SSH_CONFIG_PATH, 0o600)
        return
        
    with open(SSH_CONFIG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
        
    pattern = re.compile(rf"{re.escape(start)}.*?{re.escape(end)}\n?", re.DOTALL)
    if pattern.search(content):
        new_content = pattern.sub(block, content)
    else:
        new_content = content.rstrip() + "\n\n" + block
        
    with open(SSH_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)
    os.chmod(SSH_CONFIG_PATH, 0o600)

def remove_ssh_config_block(instance_id: str):
    if not os.path.exists(SSH_CONFIG_PATH):
        return
        
    start, end = _get_block_delimiters(instance_id)
    with open(SSH_CONFIG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
        
    pattern = re.compile(rf"{re.escape(start)}.*?{re.escape(end)}\n?", re.DOTALL)
    if pattern.search(content):
        new_content = pattern.sub("", content)
        with open(SSH_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(new_content)

def get_ide_urls(instance_id: str):
    alias = f"manifold-{instance_id}"
    vscode_url = f"vscode://vscode-remote/ssh-remote+{alias}/workspace/ephemeral"
    cursor_url = f"cursor://vscode-remote/ssh-remote+{alias}/workspace/ephemeral"
    ssh_command = f"ssh {alias}"
    return {
        "vscode_url": vscode_url,
        "cursor_url": cursor_url,
        "ssh_alias": alias,
        "ssh_command": ssh_command
    }
