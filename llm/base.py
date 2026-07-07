# llm/base.py
from abc import ABC, abstractmethod
from typing import Any


class LLMClient(ABC):
    """
    Base class for all LLM providers.
    Add a new provider by subclassing this and implementing `run`.
    Switch provider via LLM_PROVIDER env var.
    """

    @abstractmethod
    async def run(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Send a request to the LLM.

        Returns a dict:
          {
            "type":     "text" | "tool_calls",
            "content":  str                        # if type == "text"
            "calls": [                             # if type == "tool_calls"
              { "id": str, "name": str, "arguments": dict }
            ]
          }
        """
        ...