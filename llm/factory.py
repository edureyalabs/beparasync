# llm/factory.py
import os
from llm.base import LLMClient


def get_llm_client() -> LLMClient:
    """
    Returns the configured LLM client.
    Set LLM_PROVIDER in .env to switch providers.

    Supported:
      groq  (default)

    To add a new provider:
      1. Create llm/<provider>.py subclassing LLMClient
      2. Add a case here
    """
    provider = os.getenv("LLM_PROVIDER", "groq").lower()

    if provider == "groq":
        from llm.groq import GroqClient
        return GroqClient()

    raise ValueError(f"Unknown LLM_PROVIDER: '{provider}'. Supported: groq")