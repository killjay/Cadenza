"""Kernel-free C1 admission; persisted historical pattern definitions stay readable."""
from __future__ import annotations

import math

PATTERN_KINDS = {"linear_pattern", "circular_pattern"}
MAX_PATTERN_COUNT = 32
MAX_PATTERN_COPIES = 128
DIRECTIONS = {"+x": [1, 0, 0], "-x": [-1, 0, 0], "+y": [0, 1, 0],
              "-y": [0, -1, 0], "+z": [0, 0, 1], "-z": [0, 0, -1]}


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and abs(value) <= 1e9


def seed_reason(seed: dict) -> str | None:
    if seed.get("kind") not in {"box", "cylinder", "hole"} or seed.get("suppressed", False):
        return "Choose an active box, cylinder or hole as the seed."
    placement, params = seed.get("placement", {}), seed.get("parameters", {})
    if placement.get("relative_to") or placement.get("rotation_deg", [0, 0, 0]) != [0, 0, 0]:
        return "Pattern seeds require absolute placement and zero rotation."
    if not all(_finite(v) for v in placement.get("origin", [0, 0, 0])):
        return "Seed coordinates must be finite and within +/- 1e9 mm."
    if seed["kind"] != "hole" and (params.get("through") or params.get("draft_angle", 0) != 0):
        return "Pattern seeds do not support draft or primitive through mode."
    return None


def actual_radius(pattern: dict, seed: dict) -> float:
    center = pattern.get("placement", {}).get("origin", [0, 0, 0])
    origin = seed.get("placement", {}).get("origin", [0, 0, 0])
    return math.hypot(origin[0] - center[0], origin[1] - center[1])


def pattern_reason(pattern: dict, source: dict) -> str | None:
    """Return why a validated, materialized definition is outside C1."""
    if pattern.get("kind") not in PATTERN_KINDS:
        return "Not a pattern."
    p = pattern["parameters"]
    features = source.get("features", [])
    seed = next((f for f in features if f["id"] == p["target_feature"]), None)
    if seed is None:
        return "Pattern seed is missing."
    reason = seed_reason(seed)
    if reason:
        return reason
    if features.index(seed) >= features.index(pattern):
        return "Pattern seed must precede the pattern."
    if pattern.get("operation", "add") != seed.get("operation", "add"):
        return "A pattern must inherit its seed's operation."
    if type(p["count"]) is not int or not 2 <= p["count"] <= MAX_PATTERN_COUNT:
        return "Pattern count must be an integer from 2 to 32, including the seed."
    placement = pattern.get("placement", {})
    origin = placement.get("origin", [0, 0, 0])
    if placement.get("relative_to") or placement.get("rotation_deg", [0, 0, 0]) != [0, 0, 0]:
        return "C1 pattern frames require absolute placement and zero rotation."
    if any(b.get("feature_id") == pattern["id"] for b in source.get("relationships", {}).get("bindings", [])):
        return "C1 pattern settings cannot be bound; edit the seed's drivers."
    if pattern["kind"] == "linear_pattern":
        if origin != [0, 0, 0] or p.get("axis", [1, 0, 0]) not in DIRECTIONS.values():
            return "Linear patterns require an identity frame and a cardinal direction."
        if not _finite(p["spacing"]) or p["spacing"] <= 0:
            return "Pattern spacing must be positive, finite and at most 1e9 mm."
        if seed["kind"] == "hole" and p.get("axis", [1, 0, 0])[2] != 0:
            return "Hole patterns support X/Y directions only."
    else:
        if p.get("axis", [0, 0, 1]) != [0, 0, 1] or p.get("sweep_angle", 360) != 360:
            return "C1 circles require source +Z and a full 360-degree sweep."
        if not all(_finite(v) for v in origin) or origin[2] != 0:
            return "Circular center requires finite X/Y coordinates and Z=0."
        if actual_radius(pattern, seed) <= 1e-7:
            return "Move the seed off the circular axis before creating a pattern."
    return None


def _sources(document: dict) -> dict[str, dict]:
    return {"root": document, **{p["id"]: p for p in (document.get("assembly") or {}).get("parts", [])}}


def validate_pattern_changes(before: dict, raw_after: dict, after: dict) -> None:
    """Compare identities across the complete patch, including embedded sources.

    `after` has passed ledger validation; raw values retain strict count types.
    Geometry-only changes trigger admission, while metadata changes and legacy
    reads preserve old definitions. Radius/axis policy cannot be bypassed by
    parent replacement or remove/re-add of the same identity.
    """
    old_sources, raw_sources = _sources(before), _sources(raw_after)
    for sid, source in _sources(after).items():
        previous = {f["id"]: f for f in old_sources.get(sid, {}).get("features", [])}
        raw = {f["id"]: f for f in raw_sources[sid].get("features", [])}
        affected = False
        for f in source.get("features", []):
            old = previous.get(f["id"])
            if old and old["kind"] in PATTERN_KINDS and f["kind"] != old["kind"]:
                raise ValueError("Pattern kind is fixed; remove and recreate with a new identity.")
            if old and old["kind"] == "circular_pattern" and f["kind"] == "circular_pattern":
                for field in ("radius", "axis"):
                    if old["parameters"][field] != f["parameters"][field]:
                        raise ValueError(f"{f['id']}: {field} is compatibility data; select and move the seed to change radius.")
            if f["kind"] not in PATTERN_KINDS:
                continue
            changed = old is None or any(old.get(k) != f.get(k) for k in ("kind", "parameters", "placement", "operation"))
            if old and old["kind"] in PATTERN_KINDS and old["parameters"]["target_feature"] != f["parameters"]["target_feature"]:
                raise ValueError("Pattern seed is fixed; remove and recreate the pattern to choose another seed.")
            if type(raw[f["id"]]["parameters"]["count"]) is not int:
                raise ValueError("Pattern count must be a strict integer, not a string, boolean or fraction.")
            if not changed:
                if old and not f.get("suppressed") and pattern_reason(old, old_sources[sid]) is None:
                    reason = pattern_reason(f, source)
                    if reason:
                        raise ValueError(f"{f['id']}: dependent pattern is no longer supported: {reason}")
                    affected = affected or source != old_sources[sid]
                continue
            affected = True
            reason = pattern_reason(f, source)
            if reason:
                raise ValueError(f"{f['id']}: {reason}")
            if (old is None or old["kind"] != f["kind"]) and f["kind"] == "circular_pattern":
                seed = next(s for s in source["features"] if s["id"] == f["parameters"]["target_feature"])
                if not math.isclose(f["parameters"]["radius"], actual_radius(f, seed), rel_tol=1e-9, abs_tol=1e-7):
                    raise ValueError("New circular radius must match seed placement and center; radius is read-only.")
        if affected and sum(f["parameters"]["count"] - 1 for f in source.get("features", [])
                            if f["kind"] in PATTERN_KINDS and not f.get("suppressed")) > MAX_PATTERN_COPIES:
            raise ValueError("Source pattern budget exceeds 128 generated copies.")
