"""Import shim for `cadenza_contracts` while the package is half-landed.

THIS FILE IS TEMPORARY. Delete it the moment the architect lands
`cadenza_contracts/geometry.py` and `messages.py`, and replace every
`from cadenza_backend.contracts_bridge import X` with `from cadenza_contracts import X`.

Why it exists
-------------
`cadenza_contracts/__init__.py` eagerly imports `.geometry` and `.messages`,
neither of which exists yet. Python executes a package's `__init__` before any
submodule, so *every* import — including `from cadenza_contracts.ledger import
Ledger`, which needs none of the missing modules — dies with:

    ModuleNotFoundError: No module named 'cadenza_contracts.geometry'

Rather than fork the contracts (which would let the three workstreams diverge —
exactly what the architect's header warns against), we register a synthetic
parent package whose `__path__` points at the real directory. Submodule imports
then resolve against the real files while the unrunnable `__init__` is skipped.
The ledger/patch/ids/errors definitions used are byte-for-byte the architect's.

The shim is self-disabling: as soon as the real `__init__` imports cleanly, the
normal import path is taken and none of this code runs.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path


def _real_import_works() -> bool:
    try:
        import cadenza_contracts  # noqa: F401

        return True
    except Exception:
        # A failed import leaves no entry in sys.modules, so there is nothing
        # to clean up before we install the stand-in below.
        return False


def _locate_package_dir() -> Path:
    """Find the on-disk `cadenza_contracts/` directory."""
    candidates = [Path(p) / "cadenza_contracts" for p in sys.path if p]
    # Fall back to the monorepo layout: <repo>/cadenza/{backend,contracts}/
    candidates.append(Path(__file__).resolve().parents[3] / "contracts" / "cadenza_contracts")
    for candidate in candidates:
        if (candidate / "ledger.py").is_file():
            return candidate
    raise ImportError(
        "Could not locate the cadenza_contracts package. Expected it on sys.path "
        "or at <repo>/cadenza/contracts/cadenza_contracts/."
    )


def _install_stand_in_package() -> None:
    pkg_dir = _locate_package_dir()
    pkg = types.ModuleType("cadenza_contracts")
    pkg.__path__ = [str(pkg_dir)]  # type: ignore[attr-defined]
    pkg.__doc__ = "Stand-in parent package installed by cadenza_backend.contracts_bridge."
    sys.modules["cadenza_contracts"] = pkg


CONTRACTS_FULLY_LANDED = _real_import_works()
if not CONTRACTS_FULLY_LANDED:
    _install_stand_in_package()

# --------------------------------------------------------------------------- #
# Re-exports — these all come from the architect's real files.
# --------------------------------------------------------------------------- #

from cadenza_contracts.errors import CadenzaError, ErrorCode  # noqa: E402
from cadenza_contracts.ids import (  # noqa: E402
    new_feature_id,
    new_message_id,
    new_project_id,
    new_session_id,
)
from cadenza_contracts.ledger import (  # noqa: E402
    FEATURE_KINDS,
    SCHEMA_VERSION,
    AIContext,
    Assumption,
    BoxFeature,
    BoxParameters,
    CylinderFeature,
    CylinderParameters,
    Feature,
    GearFeature,
    GearParameters,
    HoleFeature,
    HoleParameters,
    Ledger,
    Metadata,
    Placement,
    TargetRef,
    empty_ledger,
    feature_by_id,
    feature_index,
)
from cadenza_contracts.patch import (  # noqa: E402
    MAX_PATCH_OPS,
    AgentPatchOutput,
    PatchOp,
    PointerResolutionError,
    normalize_patch,
    normalize_pointer,
)
from cadenza_contracts.version import CONTRACT_VERSION  # noqa: E402

__all__ = [
    "CONTRACTS_FULLY_LANDED",
    "CONTRACT_VERSION",
    "SCHEMA_VERSION",
    "MAX_PATCH_OPS",
    "Ledger",
    "Metadata",
    "Feature",
    "FEATURE_KINDS",
    "BoxFeature",
    "CylinderFeature",
    "HoleFeature",
    "GearFeature",
    "BoxParameters",
    "CylinderParameters",
    "HoleParameters",
    "GearParameters",
    "Placement",
    "TargetRef",
    "AIContext",
    "Assumption",
    "empty_ledger",
    "feature_by_id",
    "feature_index",
    "PatchOp",
    "AgentPatchOutput",
    "normalize_pointer",
    "normalize_patch",
    "PointerResolutionError",
    "ErrorCode",
    "CadenzaError",
    "new_project_id",
    "new_session_id",
    "new_feature_id",
    "new_message_id",
]
