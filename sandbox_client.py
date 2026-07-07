# beparasync/sandbox_client.py

import os
import uuid
import base64
import httpx
from typing import Any

SANDBOX_URL    = os.getenv("SANDBOX_URL", "http://localhost:9000")
SANDBOX_SECRET = os.getenv("SANDBOX_SECRET", "")
SANDBOX_TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT", "120"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {SANDBOX_SECRET}", "Content-Type": "application/json"}


def encode_file(content: bytes) -> str:
    return base64.b64encode(content).decode()


def decode_file(b64: str) -> bytes:
    return base64.b64decode(b64)


async def run_in_sandbox(
    code: str,
    entry_point: str,
    files: dict[str, bytes],
    env_vars: dict[str, str],
    packages: list[str],
    agent_id: str,
    timeout: int = 60,
) -> dict[str, Any]:
    execution_id = str(uuid.uuid4())

    encoded_files = {name: encode_file(content) for name, content in files.items()}
    env_with_agent = {**env_vars, "__AGENT_ID__": agent_id}

    payload = {
        "execution_id": execution_id,
        "code":         code,
        "entry_point":  entry_point,
        "files":        encoded_files,
        "env_vars":     env_with_agent,
        "packages":     packages,
        "timeout":      timeout,
    }

    async with httpx.AsyncClient(timeout=SANDBOX_TIMEOUT) as client:
        resp = await client.post(
            f"{SANDBOX_URL}/execute/run",
            json=payload,
            headers=_headers(),
        )
        if not resp.is_success:
            return {
                "stdout":       "",
                "stderr":       f"Sandbox error {resp.status_code}: {resp.text}",
                "exit_code":    -1,
                "duration_ms":  0,
                "output_files": {},
            }
        return resp.json()


async def install_in_sandbox(agent_id: str, packages: list[str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{SANDBOX_URL}/execute/install",
            json={"agent_id": agent_id, "packages": packages},
            headers=_headers(),
        )
        if not resp.is_success:
            return {"ok": False, "error": f"Sandbox error {resp.status_code}: {resp.text}"}
        return resp.json()