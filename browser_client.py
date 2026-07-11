# beparasync/browser_client.py
import os
import httpx
from typing import Any

BROWSER_URL     = os.getenv("BROWSER_URL", "http://localhost:9001")
BROWSER_SECRET  = os.getenv("BROWSER_SECRET", "")
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "180"))


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {BROWSER_SECRET}",
        "Content-Type":  "application/json",
    }


async def run_in_browser(
    task: str,
    start_url: str | None,
    env_vars: dict[str, str],
    timeout: int = 120,
) -> dict[str, Any]:
    payload = {
        "task":      task,
        "start_url": start_url,
        "env_vars":  env_vars,
        "timeout":   timeout,
    }

    try:
        async with httpx.AsyncClient(timeout=BROWSER_TIMEOUT) as client:
            resp = await client.post(
                f"{BROWSER_URL}/browse/run",
                json=payload,
                headers=_headers(),
            )
            if not resp.is_success:
                return {
                    "result": "",
                    "steps":  [],
                    "error":  f"Browser sandbox error {resp.status_code}: {resp.text}",
                    "ok":     False,
                }
            return resp.json()
    except httpx.TimeoutException:
        return {
            "result": "",
            "steps":  [],
            "error":  f"Browser sandbox did not respond within {BROWSER_TIMEOUT}s.",
            "ok":     False,
        }
    except Exception as e:
        return {
            "result": "",
            "steps":  [],
            "error":  f"Browser client error: {e}",
            "ok":     False,
        }