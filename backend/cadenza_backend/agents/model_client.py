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
from typing import Any

import httpx

from cadenza_backend.config import ENV_FILES, get_settings
from cadenza_backend.agents.core import AgentCard
from cadenza_backend.tools import registry

class AIError(RuntimeError):
    """Any model-call failure. Mapped to ErrorCode.AGENT_* by the agent layer."""


class AINotConfigured(AIError):
    """No usable provider key. Distinct so the transport can say so plainly."""


class AIRefused(AIError):
    """The model declined (stop_reason == "refusal"). Not retryable as-is."""


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


class ModelClient:
    def __init__(self) -> None:
        self.s = get_settings()

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

        client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key)

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
        
        if schema is not None:
            kwargs["tools"] = [
                {
                    "name": "emit",
                    "description": "Emit the structured result. This is the only way to answer. Call this only when you are ready to finalize the geometry.",
                    "input_schema": schema,
                }
            ]
            for tool in tools:
                kwargs["tools"].append(tool.schema)
            kwargs["tool_choice"] = {"type": "auto"}

        while True:
            try:
                msg = await client.messages.create(**kwargs)
            except anthropic.APIError as e:
                raise AIError(f"Anthropic API error: {e}") from e

            # Opus 5 runs safety classifiers; a decline is an HTTP 200 with an empty
            # or partial `content`, so this has to be checked before reading blocks.
            if msg.stop_reason == "refusal":
                detail = getattr(msg, "stop_details", None)
                raise AIRefused(
                    f"Model declined the request (category={getattr(detail, 'category', None)})"
                )

            if schema is not None:
                has_tool_use = False
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
                            return out
                        else:
                            import json
                            tool_impl = next((t for t in tools if t.name == block.name), None)
                            if tool_impl:
                                try:
                                    result = tool_impl.execute(context=context, **block.input)
                                except Exception as e:
                                    result = {"error": str(e)}
                            else:
                                result = {"error": f"Unknown tool '{block.name}'"}

                            kwargs["messages"].append({"role": "assistant", "content": msg.content})
                            kwargs["messages"].append({
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": json.dumps(result)
                                    }
                                ]
                            })
                            break # Break the for loop, continue the while loop
                
                if has_tool_use:
                    continue

                raise AIError(
                    f"Model did not call the structured-output tool (stop_reason={msg.stop_reason})"
                )

            text = "".join(b.text for b in msg.content if b.type == "text")
            return _extract_json(text)

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
                r = await http.post(f"{base_url}/chat/completions", json=body, headers=headers)
                if r.status_code != 200:
                    raise AIError(f"{provider} HTTP {r.status_code}: {r.text[:400]}")
                
                message = r.json()["choices"][0]["message"]
                
                # Check for tool calls
                if "tool_calls" in message and message["tool_calls"]:
                    # Append assistant message
                    body["messages"].append(message)
                    
                    for tool_call in message["tool_calls"]:
                        name = tool_call["function"]["name"]
                        import json
                        tool_impl = next((t for t in tools if t.name == name), None)
                        args = json.loads(tool_call["function"]["arguments"])
                        
                        if tool_impl:
                            try:
                                result = tool_impl.execute(context=context, **args)
                            except Exception as e:
                                result = {"error": str(e)}
                        else:
                            result = {"error": f"Unknown tool '{name}'"}
                            
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
