"""Model router — one call surface over Anthropic + OpenAI-compatible providers.

Lifted from `draftsmith/server/draftsmith/ai/client.py` and trimmed to what
CADenza needs. The two things worth keeping from the original are the two that
are easy to get subtly wrong:

  * **Anthropic gets schema-valid JSON via forced tool use.** A tool with the
    payload as its `input_schema` plus `tool_choice={"type":"tool"}` means the
    model cannot reply with prose — the API itself enforces the shape. Free-text
    JSON with a regex fallback is how you end up parsing "Sure! Here's the
    patch:" at 2am.
  * **A `max_tokens` truncation is reported as truncation.** A run that hits the
    ceiling mid-payload still comes back as a well-formed tool call with keys
    missing; without the explicit check that surfaces downstream as a bare
    KeyError naming a field the model never got to write.

Changed from the original: the caller passes `effort`, and this module never
disables thinking. On Claude Opus 5 thinking is on by default, and disabling it
has a documented failure mode where the model writes a tool call into its
visible text instead of emitting a `tool_use` block — the turn "succeeds", the
call never happens, and nothing raises. For an agent whose entire job is to
return a structured patch, that is the worst available failure.
"""

from __future__ import annotations

import base64
import json
import re
import math
import sys
from contextlib import asynccontextmanager
from time import monotonic
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from cadenza_backend.config import ENV_FILES, get_settings
from cadenza_backend.agents.core import AgentCard
from cadenza_backend.tools import registry
from cadenza_backend.session import SessionLogger
from cadenza_contracts.errors import ErrorCode

class AIError(RuntimeError):
    """Any model-call failure. Mapped to ErrorCode.AGENT_* by the agent layer."""


class AINotConfigured(AIError):
    """No usable provider key. Distinct so the transport can say so plainly."""


class AIRefused(AIError):
    """The model declined (stop_reason == "refusal"). Not retryable as-is."""


class AIProviderError(AIError):
    """Provider transport/request failure, distinct from invalid model output."""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.AGENT_UNAVAILABLE):
        super().__init__(message)
        self.code = code


def _provider_error(provider: str, exc: Exception) -> AIProviderError:
    import anthropic
    if isinstance(exc, (anthropic.APITimeoutError, httpx.TimeoutException)):
        return AIProviderError(f"{provider} request timed out. Try again later.", ErrorCode.AGENT_TIMEOUT)
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return AIProviderError(f"{provider} rejected the API request (HTTP {status}). Check provider access, model configuration and diagnostics.")
    return AIProviderError(f"Could not connect to {provider}. Check network access and diagnostics.")


def _anthropic_schema(schema: dict, field: str) -> tuple[dict, bool]:
    if not any(key in schema for key in ("oneOf", "anyOf", "allOf")):
        return schema, False
    # Keep local references rooted at /$defs while nesting the union.
    wrapped = {"type": "object", "properties": {field: {
        key: value for key, value in schema.items() if key != "$defs"}},
        "required": [field], "additionalProperties": False}
    if "$defs" in schema:
        wrapped["$defs"] = schema["$defs"]
    return wrapped, True


def _unwrap_tool_input(value: Any, field: str) -> dict:
    if not isinstance(value, dict) or set(value) != {field} or not isinstance(value[field], dict):
        raise AIError("Model returned an invalid tool-input envelope")
    return value[field]


def _extract_json(text: str) -> Any:
    """Best-effort JSON extraction from a model reply (OpenAI-compatible path only)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
    raise AIError("Model reply contained no parseable JSON")


def normalize_usage(provider: str, raw: dict | None) -> dict:
    """Total input includes cached input; unknown counters stay unknown."""
    raw = raw if isinstance(raw, dict) else {}
    def count(value):
        return value if type(value) is int and value >= 0 else None
    if provider == "anthropic":
        counts = [count(raw.get(k)) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
        input_tokens = sum(v for v in counts if v is not None) if all(v is not None for v in counts) else None
        cached, written = counts[1:]
        output_tokens = count(raw.get("output_tokens"))
    else:
        details = raw.get("prompt_tokens_details") or {}
        details = details if isinstance(details, dict) else {}
        input_tokens, output_tokens = count(raw.get("prompt_tokens")), count(raw.get("completion_tokens"))
        cached = count(raw.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
        written = count(details.get("cache_write_tokens"))
    cost = raw.get("cost") if provider == "openrouter" else None
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost) or cost < 0:
        cost = None
    return dict(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cached,
                cache_write_tokens=written, reported_cost_usd=cost)


class ModelClient:
    def __init__(self, logger: SessionLogger | None = None, *,
                 observe: Callable[[dict], None] | None = None,
                 before_request: Callable[[str, str], None] | None = None,
                 before_anthropic_request: Callable[[Any, dict], Awaitable[None]] | None = None) -> None:
        self.s = get_settings()
        from cadenza_backend.diagnostics import CURRENT
        self.logger = logger if logger is not None else CURRENT.get()
        self.observe = observe
        self.before_request = before_request
        self.before_anthropic_request = before_anthropic_request

    @asynccontextmanager
    async def _request(self, provider: str, model: str):
        """Observe each network attempt, without prompts, keys or response content."""
        if self.before_request:
            self.before_request(provider, model)
        started = monotonic()
        record: dict[str, Any] = dict(provider=provider, model=model, outcome="response", usage=None)
        try:
            yield record
        except BaseException as exc:
            record["outcome"] = "interrupted_or_failed"
            record["error_type"] = type(exc).__name__
            status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            if type(status) is int and 400 <= status <= 599: record["http_status"] = status
            cause = exc.__cause__
            causes = []
            while cause is not None and len(causes) < 4:
                causes.append(type(cause).__name__)
                cause = cause.__cause__
            if causes: record["cause_types"] = causes
            raise
        finally:
            record["duration_ms"] = (monotonic() - started) * 1000
            if self.logger:
                self.logger.log("model.attempt", **record)
            if self.observe:
                self.observe(record)

    # ── public ──────────────────────────────────────────────────────────────

    async def complete_json(
        self,
        stage: str,
        agent_card: AgentCard,
        user: str,
        images: list[tuple[str, bytes]] | None = None,  # (media_type, data)
        max_tokens: int | None = None,
        effort: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict:
        """Call the model routed for `stage` and return a JSON object.

        `agent_card` defines the system prompt, tools, and the final output schema.
        """
        if not self.s.ai_available:
            raise AINotConfigured(
                "No AI provider configured. Set ANTHROPIC_API_KEY and/or DEEPSEEK_API_KEY in "
                + " or ".join(str(p) for p in ENV_FILES)
            )
        max_tokens = max_tokens or self.s.agent_max_tokens
        provider, model = self.s.stage_model(stage)
        if self.logger:
            self.logger.log("model.stage", stage=stage)

        if images and provider == "deepseek":
            # Route image input to a provider that accepts it rather than
            # silently dropping the sketch the user just uploaded.
            if self.s.anthropic_api_key:
                provider, model = "anthropic", "claude-sonnet-5"
            elif self.s.openrouter_api_key:
                provider, model = "openrouter", "anthropic/claude-sonnet-4.5"

        # Resolve tools
        tools = [registry.get(name) for name in agent_card.tools]
        system = agent_card.system_prompt
        if tools:
            system += "\n\nTOOLS AVAILABLE:\n"
            for tool in tools:
                system += f"- {tool.name}: {tool.description}\n"

        if provider == "anthropic" and self.s.anthropic_api_key:
            return await self._anthropic(
                model, system, user, agent_card.output_schema, tools, images or [], max_tokens, effort, context
            )

        key = self.s.key_for(provider)
        if key:
            return await self._openai_compatible(
                provider, model, system, user, agent_card.output_schema, tools, images or [], max_tokens, context
            )
        raise AINotConfigured(
            f"Stage '{stage}' routes to '{provider}', which has no API key set. "
            "Add it to cadenza/.env (ANTHROPIC_API_KEY / DEEPSEEK_API_KEY / OPENROUTER_API_KEY)."
        )

    # ── anthropic ───────────────────────────────────────────────────────────

    async def _anthropic(
        self, model, system, user, schema, tools, images, max_tokens, effort, context
    ) -> dict:
        import anthropic

        options: dict[str, Any] = {"max_retries": 0} if self.before_request or self.observe or self.before_anthropic_request else {}
        client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key, **options)

        try:
            content: list[dict] = []
            for media_type, data in images:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(data).decode(),
                        },
                    }
                )
            content.append({"type": "text", "text": user})

            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                # The system prompt is the stable prefix and is identical across every
                # turn of a session, so it is worth a cache breakpoint.
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": content}],
            }
            if effort:
                kwargs["output_config"] = {"effort": effort}
            wrapped_result = False
            wrapped_tools: set[str] = set()
            if schema is not None:
                input_schema, wrapped_result = _anthropic_schema(schema, "result")
                kwargs["tools"] = [
                    {
                        "name": "emit",
                        "description": "Emit the structured result. This is the only way to answer. Call this only when you are ready to finalize the geometry.",
                        "input_schema": input_schema,
                    }
                ]
                for tool in tools:
                    tool_schema, wrapped = _anthropic_schema(tool.schema["input_schema"], "input")
                    kwargs["tools"].append({**tool.schema, "input_schema": tool_schema})
                    if wrapped: wrapped_tools.add(tool.name)
                kwargs["tool_choice"] = {"type": "auto"}

            while True:
                try:
                    if self.before_anthropic_request:
                        await self.before_anthropic_request(client, kwargs)
                    if self.logger:
                        self.logger.log("llm_request", provider="anthropic", model=model, system=system, messages=kwargs["messages"])
                    async with self._request("anthropic", model) as record:
                        msg = await client.messages.create(**kwargs)
                        usage = getattr(msg, "usage", None)
                        record["usage"] = normalize_usage("anthropic", usage.model_dump() if usage else None)
                    if self.logger:
                        self.logger.log("llm_response", provider="anthropic", stop_reason=msg.stop_reason, content=[c.model_dump() for c in msg.content])
                except anthropic.APIError as e:
                    raise _provider_error("anthropic", e) from e

                # Opus 5 runs safety classifiers; a decline is an HTTP 200 with an empty
                # or partial `content`, so this has to be checked before reading blocks.
                if msg.stop_reason == "refusal":
                    detail = getattr(msg, "stop_details", None)
                    raise AIRefused(
                        f"Model declined the request (category={getattr(detail, 'category', None)})"
                    )

                if schema is not None:
                    has_tool_use = False
                    tool_results_content = []
                    for block in msg.content:
                        if block.type == "tool_use":
                            has_tool_use = True
                            if block.name == "emit":
                                out = dict(block.input)
                                if msg.stop_reason == "max_tokens":
                                    raise AIError(
                                        f"Model output was truncated at max_tokens={max_tokens} "
                                        f"(got keys: {sorted(out)}). Raise max_tokens or simplify the request."
                                    )
                                if wrapped_result:
                                    return _unwrap_tool_input(out, "result")
                                return out
                            else:
                                import json
                                tool_impl = next((t for t in tools if t.name == block.name), None)
                                if tool_impl:
                                    try:
                                        inputs = _unwrap_tool_input(block.input, "input") if block.name in wrapped_tools else block.input
                                        result = await tool_impl.execute_async(context=context, **inputs)
                                        if self.logger:
                                            self.logger.log("tool_use", tool_name=block.name, inputs=inputs, result=result)
                                    except Exception as e:
                                        result = {"error": str(e)}
                                        if self.logger:
                                            self.logger.log("tool_use_error", tool_name=block.name, inputs=block.input, error=str(e))
                                else:
                                    result = {"error": f"Unknown tool '{block.name}'"}
                                    if self.logger:
                                        self.logger.log("tool_not_found", tool_name=block.name)

                                content_str = result if isinstance(result, str) else json.dumps(result)
                                tool_results_content.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": content_str
                                })
                                
                    if has_tool_use:
                        # Convert Pydantic objects to dicts to ensure the Anthropic SDK serializes them correctly
                        # when continuing the conversation.
                        kwargs["messages"].append({
                            "role": "assistant", 
                            "content": [b.model_dump() for b in msg.content]
                        })
                        kwargs["messages"].append({
                            "role": "user",
                            "content": tool_results_content
                        })
                        continue

                    raise AIError(
                        f"Model did not call the structured-output tool (stop_reason={msg.stop_reason})"
                    )

                text = "".join(b.text for b in msg.content if b.type == "text")
                return _extract_json(text)
        finally:
            if self.before_anthropic_request:
                active_error = sys.exc_info()[1]
                try:
                    await client.close()
                except Exception:
                    if active_error is None: raise

    # ── OpenAI-compatible providers (DeepSeek direct, OpenRouter) ───────────

    async def _openai_compatible(
        self, provider, model, system, user, schema, tools, images, max_tokens, context
    ) -> dict:
        content: list[dict] = []
        for media_type, data in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{base64.b64encode(data).decode()}"
                    },
                }
            )
        prompt = user
        if schema is not None:
            prompt += (
                "\n\nRespond with ONLY a JSON object valid against this JSON Schema:\n"
                + json.dumps(schema)
            )
        content.append({"type": "text", "text": prompt})

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
        }
        
        # Add tools for OpenAI
        if tools:
            body["tools"] = []
            for tool in tools:
                body["tools"].append({
                    "type": "function",
                    "function": {
                        "name": tool.schema["name"],
                        "description": tool.schema["description"],
                        "parameters": tool.schema.get("input_schema", {})
                    }
                })
        
        headers = {"Authorization": f"Bearer {self.s.key_for(provider)}"}
        if provider == "openrouter":
            headers |= {"HTTP-Referer": "https://cadenza.local", "X-Title": "CADenza"}
        base_url = self.s.base_url_for(provider)

        async with httpx.AsyncClient(timeout=180) as http:
            while True:
                if self.logger:
                    self.logger.log("llm_request", provider=provider, model=model, body=body)
                try:
                    async with self._request(provider, model) as record:
                        r = await http.post(f"{base_url}/chat/completions", json=body, headers=headers)
                        r.raise_for_status()
                        record["usage"] = normalize_usage(provider, r.json().get("usage"))
                except httpx.HTTPError as exc:
                    raise _provider_error(provider, exc) from exc
                
                choice = r.json()["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise AIError("Model output was truncated. Simplify the request or increase the output allowance.")
                message = choice["message"]
                if self.logger:
                    self.logger.log("llm_response", provider=provider, message=message)
                
                # Check for tool calls
                if "tool_calls" in message and message["tool_calls"]:
                    # Append assistant message
                    body["messages"].append(message)
                    
                    for tool_call in message["tool_calls"]:
                        name = tool_call["function"]["name"]
                        tool_impl = next((t for t in tools if t.name == name), None)
                        args = json.loads(tool_call["function"]["arguments"])
                        
                        if tool_impl:
                            try:
                                result = await tool_impl.execute_async(context=context, **args)
                                if self.logger:
                                    self.logger.log("tool_use", tool_name=name, inputs=args, result=result)
                            except Exception as e:
                                result = {"error": str(e)}
                                if self.logger:
                                    self.logger.log("tool_use_error", tool_name=name, inputs=args, error=str(e))
                        else:
                            result = {"error": f"Unknown tool '{name}'"}
                            if self.logger:
                                self.logger.log("tool_not_found", tool_name=name)
                            
                        body["messages"].append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": json.dumps(result)
                        })
                    continue
                
                text = message.get("content", "")
                try:
                    return _extract_json(text)
                except (AIError, json.JSONDecodeError):
                    # In a robust implementation we would retry here, but for brevity we'll just fail
                    raise AIError("Model returned invalid JSON instead of a tool call or valid object.")
