"""Model provider. All the provider-specific code lives here"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ProviderError(Exception):
    """A call failed for this alert, but the run can carry on."""


class FatalProviderError(Exception):
    """A failure that would repeat on every call (bad key, unknown model, no quota, ...)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  


@dataclass
class ModelReply:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: dict[str, int]
    assistant_message: dict = field(default_factory=dict)  # appended to the conversation


class OpenAIProvider:
    name = "openai"
    API_KEY_ENV = "OPENAI_API_KEY"

    def __init__(self, model: str, output_allowance: int, reasoning_effort: str | None = None,
                 timeout_s: float = 120.0):
        if not os.environ.get(self.API_KEY_ENV):
            raise FatalProviderError(f"environment variable {self.API_KEY_ENV} is not set")
        import openai  # lazy import so the tests don't need the SDK

        self._openai = openai
        self.client = openai.OpenAI(timeout=timeout_s, max_retries=3)
        self.model = model
        self.output_allowance = output_allowance
        self.reasoning_effort = reasoning_effort

    def complete(self, messages: list[dict], tools: list[dict], tool_choice) -> ModelReply:
        o = self._openai
        kwargs = dict(model=self.model, messages=messages, tools=tools, tool_choice=tool_choice,
                      max_completion_tokens=self.output_allowance)
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except (o.AuthenticationError, o.PermissionDeniedError, o.NotFoundError) as exc:
            raise FatalProviderError(f"{type(exc).__name__}: {exc}") from exc
        except o.BadRequestError as exc:
            if "context_length" in str(exc):
                raise ProviderError(f"context too long: {exc}") from exc
            raise FatalProviderError(f"BadRequestError: {exc}") from exc
        except o.RateLimitError as exc:
            if "insufficient_quota" in str(exc):
                raise FatalProviderError(f"insufficient quota: {exc}") from exc
            raise ProviderError(f"rate limited after retries: {exc}") from exc
        except o.APIError as exc:  
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc

        if resp.usage is None:
            raise ProviderError("provider returned no usage; refusing to invent one")
        usage = {"input": int(resp.usage.prompt_tokens), "output": int(resp.usage.completion_tokens)}
        details = getattr(resp.usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        if cached is not None:
            usage["cache_read"] = int(cached)

        choice = resp.choices[0]
        msg = choice.message
        calls = [ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "")
                 for tc in (msg.tool_calls or []) if getattr(tc, "function", None) is not None]
        assistant = {"role": "assistant", "content": msg.content or ""}
        if calls:
            assistant["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls]
        return ModelReply(text=msg.content or "", tool_calls=calls,
                          finish_reason=choice.finish_reason or "", usage=usage,
                          assistant_message=assistant)


def make_provider(provider: str, model: str, output_allowance: int, reasoning_effort: str | None,
                  timeout_s: float):
    if provider == "openai":
        return OpenAIProvider(model, output_allowance, reasoning_effort, timeout_s)
    raise FatalProviderError(f"unsupported model.provider {provider!r} (this agent implements 'openai')")
