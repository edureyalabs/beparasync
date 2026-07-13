# beparasync/llm/groq.py
import os
import re
import json
import httpx
from typing import Any
from llm.base import LLMClient

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"


def _extract_bad_tool_name(error_text: str) -> str | None:
    """
    Parse the hallucinated tool name from Groq's error response.
    Handles both error formats we've seen:
      - "attempted to call tool 'web_browse' which was not in request.tools"
      - failed_generation JSON containing {"name": "browser.open", ...}
    """
    # Try to get tool name from failed_generation JSON
    fg_match = re.search(r'"failed_generation"\s*:\s*"({.*?})"', error_text)
    if fg_match:
        try:
            inner = json.loads(fg_match.group(1).replace('\\"', '"'))
            if "name" in inner:
                return inner["name"]
        except Exception:
            pass

    # Try plain text pattern
    match = re.search(r"call(?:ed)? tool ['\"]([^'\"]+)['\"]", error_text)
    if match:
        return match.group(1)

    return None


class GroqClient(LLMClient):
    def __init__(self):
        self.api_key = os.environ["GROQ_API_KEY"]
        self.model   = os.getenv("GROQ_MODEL", DEFAULT_MODEL)

    async def run(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_completion_tokens": 4096,
        }

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name":        t["name"],
                        "description": t["description"],
                        "parameters":  t["parameters"],
                    },
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(GROQ_API_URL, json=payload, headers=headers)

            if not resp.is_success:
                error_text = resp.text
                error_data = {}
                try:
                    error_data = resp.json()
                except Exception:
                    pass

                # Groq rejects requests when the model hallucinates a tool name
                # that wasn't in our tools list. Return a recoverable signal
                # instead of crashing so the caller can inject a corrective
                # message and retry.
                code = (error_data.get("error") or {}).get("code", "")
                if resp.status_code == 400 and code == "tool_use_failed":
                    bad_tool = _extract_bad_tool_name(error_text)
                    valid_names = [t["name"] for t in tools]
                    return {
                        "type":       "hallucinated_tool",
                        "bad_tool":   bad_tool or "unknown",
                        "valid_tools": valid_names,
                        "raw_error":  error_text,
                    }

                # All other errors — raise as before
                raise Exception(f"Groq {resp.status_code}: {error_text}")

            data = resp.json()

        message = data["choices"][0]["message"]

        if message.get("tool_calls"):
            calls = [
                {
                    "id":        tc["id"],
                    "name":      tc["function"]["name"],
                    "arguments": json.loads(tc["function"]["arguments"]),
                }
                for tc in message["tool_calls"]
            ]
            return {"type": "tool_calls", "calls": calls}

        return {"type": "text", "content": message.get("content") or ""}