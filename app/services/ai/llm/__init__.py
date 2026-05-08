"""LLM provider abstraction for the agentic learning-path chatbot.

Public surface
--------------
* :class:`ChatProvider` — protocol every concrete provider must satisfy.
* :class:`ChatMessage` / :class:`ToolCall` / :class:`ToolDefinition` — the
  canonical message + tool-calling shape (loosely modelled on OpenAI's
  function-calling format so adapters stay thin).
* :func:`get_provider` — factory that returns the configured provider
  based on environment variables. Defaults to :class:`StubProvider`,
  which is purely deterministic and never hits the network — handy when
  no API key is wired up yet (the user asked us to stop and prompt for
  secrets before the real provider is enabled).

Adding a new provider
---------------------
1. Subclass :class:`ChatProvider` and implement :meth:`generate`.
2. Register it in :func:`get_provider` behind a config flag.
3. Make sure secrets come from ``app.core.config.get_settings`` so we
   stay consistent with the rest of the backend.
"""

from __future__ import annotations

from .base import (
    ChatMessage,
    ChatProvider,
    ChatResponse,
    ToolCall,
    ToolDefinition,
)
from .stub_provider import StubProvider
from .factory import get_fallback_provider, get_provider

__all__ = [
    "ChatMessage",
    "ChatProvider",
    "ChatResponse",
    "ToolCall",
    "ToolDefinition",
    "StubProvider",
    "get_provider",
    "get_fallback_provider",
]
