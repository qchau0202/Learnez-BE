"""Provider-agnostic types for the chatbot.

The LLM provider only ever sees three things:

* The conversation history (``messages``).
* The list of tools it's allowed to call (``tools``).
* A system prompt that pins down its persona and scope.

It returns either a free-form ``content`` string *or* a list of
``tool_calls`` that the agent runtime will execute deterministically.
This split mirrors OpenAI's function calling so adding a real provider
later is a thin adapter — no broad refactor in the API layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


# Roles we accept on the wire. ``tool`` represents the result of a tool
# call being fed back into the model on the next turn.
ChatRole = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """A single tool invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatMessage:
    role: ChatRole
    content: str
    name: str | None = None  # tool name when role == "tool"
    tool_call_id: str | None = None
    # When role == "assistant" and this turn was a tool-call response from
    # the model, populate ``tool_calls`` so providers can replay the
    # canonical functionCall / functionResponse pair on the next round.
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolDefinition:
    """One callable surface exposed to the LLM.

    ``parameters`` follows JSON Schema conventions so it works with both
    Gemini's ``function_declarations`` and OpenAI's ``functions`` /
    ``tools`` formats with minimal adaptation.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ChatResponse:
    """What every provider must return after one inference round."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] | None = None


class ChatProvider(Protocol):
    """Minimal interface the agentic runtime depends on."""

    name: str

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        system_prompt: str,
        tools: list[ToolDefinition],
    ) -> ChatResponse:
        """Run one inference round and return either content or tool calls."""
        ...
