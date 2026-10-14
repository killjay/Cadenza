"""Optional, best-effort observation of isolated evaluations; never controls CAD."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$"
MAX_JSON = 2 * 1024 * 1024
MAX_GLB = 64 * 1024 * 1024


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sequence: int = Field(ge=1)
    step: str = Field(max_length=200)
    phase: str = Field(max_length=80)
    prompt: str = Field(max_length=32000)
    target: str = Field(max_length=200)
    detail: str = Field(default="", max_length=4000)
    before: str | None = Field(default=None, pattern=r"^[0-9]+\.glb$")
    proposal: str | None = Field(default=None, pattern=r"^[0-9]+\.glb$")
    accepted: str | None = Field(default=None, pattern=r"^[0-9]+\.glb$")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    version: Literal[1] = 1
    run_id: str = Field(pattern=IDENTIFIER)
    sequence: int = Field(ge=0)
    updated: str
    status: Literal["running", "completed", "failed", "interrupted"]
    checkpoints: list[Checkpoint] = Field(max_length=2000)
    observer_error: str | None = None
    estimated_usd: float | None = Field(default=None, ge=0)


class Observer:
    def __init__(self, output: Path):
        if not re.fullmatch(IDENTIFIER, output.name):
            raise ValueError("Observed output directory needs a simple alphanumeric, dash or underscore name")
        self.directory = output / "observer"
        self.manifest = Manifest(run_id=output.name, sequence=0, updated="", status="running", checkpoints=[])
        self.current: Checkpoint | None = None
        self.failed = False

    async def publish(self, phase: str, *, glb: bytes | None = None, **changes):
        if self.failed: return
        try:
            self.manifest.sequence += 1
            if self.current is None:
                self.current = Checkpoint(sequence=1, step="Run", phase=phase, prompt="", target="")
            data = self.current.model_dump()
            data.update(changes, phase=phase, sequence=self.manifest.sequence)
            if glb:
                if len(glb) > MAX_GLB: raise ValueError("Observer model exceeds 64 MiB")
                name = f"{self.manifest.sequence}.glb"
                await asyncio.to_thread(self._write, name, glb)
                data["before" if phase == "before" else "proposal"] = name
                if phase == "before": data["accepted"] = name
            self.current = Checkpoint.model_validate(data)
            self.manifest.checkpoints.append(self.current)
            await self.flush()
        except Exception as exc:
            self.failed = True
            self.manifest.observer_error = f"Observation unavailable: {type(exc).__name__}"
            logging.getLogger(__name__).warning(self.manifest.observer_error)
            try: await self.flush()
            except Exception: pass

    async def flush(self):
        self.manifest.updated = datetime.now(timezone.utc).isoformat()
        payload = Manifest.model_validate(self.manifest.model_dump()).model_dump_json().encode()
        if len(payload) > MAX_JSON: raise ValueError("Observer manifest exceeds 2 MiB")
        await asyncio.to_thread(self._write, "manifest.json", payload)

    def _write(self, name: str, payload: bytes):
        self.directory.mkdir(exist_ok=True)
        target = self.directory / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(payload)
        import time
        for attempt in range(3):
            try:
                temporary.replace(target)
                return
            except PermissionError:
                if attempt == 2: raise
                time.sleep(.02)

    async def begin(self, session, step, raw: dict, identity: str):
        self.current = None
        target = session.primary_ledger_id if step.route == "assembly" else raw.get("ledger_id") or session.primary_ledger_id
        await self.publish("preparing view", step=identity, prompt=raw.get("prompt", ""), target="New part" if step.route == "part" else target or "None")
        try:
            if step.route != "part" and target and target in session.stores:
                from cadenza_backend.geometry_jobs import build_design
                outcome = await build_design(session.snapshot(), target)
                if outcome.ok and not outcome.stub and outcome.glb:
                    await self.publish("before", glb=outcome.glb, detail="Initial view built separately; not inference time")
                else:
                    await self.publish("before", detail="Initial geometry unavailable")
            else:
                await self.publish("before", detail="New part: no starting geometry" if step.route == "part" else "Initial geometry unavailable")
        except Exception as exc:
            await self.publish("before", detail=f"Initial view unavailable: {type(exc).__name__}")
        await self.publish("requesting response", detail="")

    async def finish(self, status: str, budget: dict | None = None):
        self.manifest.status = cast(Literal["running", "completed", "failed", "interrupted"], status)
        if budget:
            value = budget.get("estimated_cost_usd")
            if value is not None:
                try: self.manifest.estimated_usd = float(value)
                except (TypeError, ValueError): pass
        await self.publish(status)
