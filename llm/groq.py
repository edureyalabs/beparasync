# llm/groq.py
import os
import json
import httpx
from typing import Any
from llm.base import LLMClient

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"


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
                raise Exception(f"Groq {resp.status_code}: {resp.text}")
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