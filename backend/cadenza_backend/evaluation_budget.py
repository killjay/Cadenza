# Shared admission for explicitly authorized evaluation runs.
import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import httpx
from cadenza_backend.agents.model_client import ModelClient


class EvaluationLimit(RuntimeError):
    pass


class Budget:
    def __init__(self, provider: str, model: str, max_calls: int, cap_usd: float):
        if provider != "openrouter" or not model.strip() or type(max_calls) is not int or not 1 <= max_calls <= 1000:
            raise ValueError("Live evaluation requires openrouter, an explicit model and 1..1000 calls")
        if not math.isfinite(cap_usd) or cap_usd <= 0:
            raise ValueError("Supply a positive finite spending cap")
        self.provider, self.model = provider, model
        self.max_calls, self.cap_usd = max_calls, cap_usd
        self.calls = 0
        self.remaining: float | None = None
        self.turn_calls = 0
        self.verification_requests = 0

    def verify(self, data: dict) -> None:
        self.remaining = None
        limit, remaining = data.get("limit"), data.get("limit_remaining")
        if (not isinstance(limit, (int, float)) or isinstance(limit, bool) or not math.isfinite(limit) or not 0 < limit <= self.cap_usd
                or not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or not math.isfinite(remaining) or not 0 < remaining <= limit
                or "limit_reset" not in data or data["limit_reset"] is not None
                or data.get("include_byok_in_limit") is not True):
            raise EvaluationLimit("Use a non-resetting OpenRouter key limit within the run cap, including BYOK")
        self.remaining = remaining
        self.turn_calls = 0

    def admit(self, provider: str, model: str) -> None:
        if ((provider, model) != (self.provider, self.model) or self.remaining is None
                or self.calls >= self.max_calls or self.turn_calls >= 1):
            raise EvaluationLimit("Provider/model, call limit or one-request-per-turn guard prevented a call")
        self.calls += 1
        self.turn_calls += 1

    async def refresh(self, client: ModelClient) -> None:
        self.remaining = None
        if self.calls >= self.max_calls:
            raise EvaluationLimit("The run call limit is exhausted")
        # Never send a key to an overridden/proxy host for budget verification.
        if client.s.openrouter_base_url.rstrip("/") != "https://openrouter.ai/api/v1":
            raise EvaluationLimit("Live evaluation requires the official OpenRouter endpoint")
        if not client.s.openrouter_api_key:
            raise EvaluationLimit("No OpenRouter key configured")
        async with httpx.AsyncClient(timeout=15) as http:
            self.verification_requests += 1
            try:
                response = await http.get("https://openrouter.ai/api/v1/key",
                    headers={"Authorization": f"Bearer {client.s.openrouter_api_key}"})
                if response.status_code != 200:
                    raise EvaluationLimit("Could not verify the provider-side key cap")
                self.verify(response.json().get("data", {}))
            except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
                raise EvaluationLimit("Provider-side cap verification failed") from exc


class AnthropicBudget:
    """Sequential reservations with explicit workspace or shared-key admission.

    Token counts and calculated costs are estimates, never provider billing proof.
    Prices reviewed 2026-10-10: https://platform.claude.com/docs/en/about-claude/pricing
    """
    # Input, output, five-minute cache write, cache read; USD per million tokens.
    RATES = {"claude-sonnet-5-5": tuple(map(Decimal, ("2", "10", "2.5", ".1"))),
             "claude-opus-5-5": tuple(map(Decimal, ("4", "20", "5", ".2")))}

    def __init__(self, model: str, max_calls: int, cap_usd: float, *, workspace_id: str | None,
                 workspace_cap_usd: float | None, workspace_usage_usd: float | None,
                 workspace_month: str | None, workspace_confirmed: bool, allow_shared_key: bool = False):
        def money(value):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Supply finite nonnegative dollar limits and starting usage")
            return Decimal(str(value))
        self.cap = money(cap_usd)
        if (model not in self.RATES or type(max_calls) is not int or not 1 <= max_calls <= 22
                or not 0 < self.cap <= 6 or type(allow_shared_key) is not bool):
            raise ValueError("Supply an exact reviewed model, 1..22 attempts and a run allowance at most $6")
        if allow_shared_key:
            if workspace_confirmed is not False or any(v is not None for v in (
                    workspace_id, workspace_cap_usd, workspace_usage_usd, workspace_month)):
                raise ValueError("Shared-key mode cannot attest a dedicated workspace cap")
            self.attestation = dict(method="shared_key_no_provider_cap", workspace_id=None,
                cap_usd=None, starting_usage_usd=None, month=None)
        else:
            workspace_cap, usage = money(workspace_cap_usd), money(workspace_usage_usd)
            if (not self.cap <= workspace_cap <= 6 or self.cap > workspace_cap - usage
                or workspace_confirmed is not True or not re.fullmatch(r"wrkspc_[A-Za-z0-9_-]{1,100}", workspace_id or "")
                or workspace_month != datetime.now(timezone.utc).strftime("%Y-%m")):
                raise ValueError("Confirm this month's dedicated workspace cap (at most $6), remaining funds, exact model and 1..22 attempts")
            self.attestation = dict(method="user_confirmed_not_provider_verified", workspace_id=workspace_id,
                cap_usd=workspace_cap_usd, starting_usage_usd=workspace_usage_usd, month=workspace_month)
        self.provider, self.model = "anthropic", model
        self.max_calls, self.cap_usd = max_calls, cap_usd
        self.calls = self.turn_calls = self.verification_requests = self.token_count_requests = 0
        self.reserved = Decimal(0)
        self.pending: Decimal | None = None
        self.current = Decimal(0)
        self.estimates: list[Decimal | None] = []
        self.counts: list[dict] = []
        self.stop_reason: str | None = None
        self.ready = False

    async def refresh(self, client: ModelClient) -> None:
        self.ready, self.pending = False, None
        if self.stop_reason or self.calls >= self.max_calls or not client.s.anthropic_api_key:
            raise EvaluationLimit("Anthropic admission stopped, exhausted, or missing its key")
        if (self.attestation["method"] != "shared_key_no_provider_cap"
                and self.attestation["month"] != datetime.now(timezone.utc).strftime("%Y-%m")):
            raise EvaluationLimit("Workspace cap confirmation expired at the month boundary")
        self.turn_calls = 0
        self.ready = True

    async def prepare(self, client: Any, payload: dict) -> None:
        if not self.ready or self.turn_calls or self.pending is not None or payload.get("model") != self.model:
            raise EvaluationLimit("Unexpected Anthropic route or extra request")
        self.ready = False  # Count failures cannot leave a reusable admission.
        if str(client.base_url).rstrip("/") != "https://api.anthropic.com":
            raise EvaluationLimit("Anthropic evaluation requires the official endpoint")
        allowed = {"model", "max_tokens", "system", "messages", "tools", "tool_choice", "output_config"}
        output = payload.get("max_tokens")
        if (set(payload) - allowed or type(output) is not int or not 1 <= output <= 8000
                or payload.get("output_config") != {"effort": "medium"}
                or payload.get("tool_choice") != {"type": "auto"}
                or len(payload.get("tools", [])) != 1 or payload["tools"][0].get("name") != "emit"
                or set(payload["tools"][0]) != {"name", "description", "input_schema"}
                or any(block.get("cache_control") != {"type": "ephemeral"} for block in payload.get("system", []))):
            raise EvaluationLimit("Unreviewed Anthropic request options or output allowance")
        self.token_count_requests += 1
        try:
            counted = await client.messages.count_tokens(**{k: v for k, v in payload.items() if k != "max_tokens"}, timeout=15)
        except Exception as exc:
            failure: dict[str, Any] = dict(outcome="failed", error_type=type(exc).__name__)
            status = getattr(exc, "status_code", None)
            if type(status) is int and 400 <= status <= 599: failure["http_status"] = status
            self.counts.append(failure)
            raise EvaluationLimit("Anthropic token counting failed") from exc
        tokens = getattr(counted, "input_tokens", None)
        if type(tokens) is not int or not 0 <= tokens <= 1_000_000:
            raise EvaluationLimit("Invalid token count")
        _, output_rate, write_rate, _ = self.RATES[self.model]
        reservation = ((Decimal(tokens) * Decimal("1.1") + 512) * write_rate + output * output_rate) / 1_000_000
        self.counts.append(dict(input_tokens_estimate=tokens, output_allowance=output, reservation_usd=str(reservation)))
        if self.reserved + reservation > self.cap:
            raise EvaluationLimit("Next request reservation exceeds the remaining run budget")
        self.pending = reservation

    def admit(self, provider: str, model: str) -> None:
        if (provider, model) != (self.provider, self.model) or self.pending is None or self.turn_calls or self.calls >= self.max_calls:
            raise EvaluationLimit("Anthropic request was not reserved for this exact route")
        self.current = self.pending
        self.reserved += self.current
        self.pending = None
        self.calls += 1
        self.turn_calls += 1

    def observe(self, attempt: dict) -> None:
        usage = attempt.get("usage") or {}
        values: list[Any] = [usage.get(k) for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")]
        estimate = None
        if all(type(v) is int and v >= 0 for v in values):
            total, output, written, cached = values
            if written + cached <= total:
                input_rate, output_rate, write_rate, read_rate = self.RATES[self.model]
                estimate = ((total - written - cached) * input_rate + output * output_rate
                    + written * write_rate + cached * read_rate) / 1_000_000
        self.estimates.append(estimate)
        attempt["estimated_cost_usd"] = str(estimate) if estimate is not None else None
        attempt["reserved_cost_usd"] = str(self.current)
        if attempt["outcome"] != "response":
            self.stop_reason = "provider_failure"  # Includes timeout/auth/spend failures; no automatic retry.
        elif estimate is not None and estimate > self.current:
            self.stop_reason = "observed_usage_exceeds_reservation"
            raise EvaluationLimit("Observed usage exceeds the request reservation; stop and review pricing/limits")

    def summary(self) -> dict:
        return dict(workspace_cap=self.attestation, reserved_usd=str(self.reserved),
            estimated_cost_usd=str(sum(v for v in self.estimates if v is not None)) if self.estimates and all(v is not None for v in self.estimates) else None,
            token_count_requests=self.token_count_requests, counts=self.counts, stop_reason=self.stop_reason,
            price_date="2026-10-10", rates_per_million=dict(zip(("input", "output", "cache_write", "cache_read"),
                (str(v) for v in self.RATES[self.model]))))


