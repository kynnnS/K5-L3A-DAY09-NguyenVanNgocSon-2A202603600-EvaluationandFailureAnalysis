"""Research card R1 ablation: how many read tools the investigator is given.

One variable changes: the `allowed_tools` list in the handoff. Model (MockLLM),
seeds, corpus, prices, evaluator, host, and the orchestrator's own code are the
same as shape C. Run from `src/`:

    python -m lab8.benchmark --shapes orchestrator_min,orchestrator,orchestrator_wide --extra-shape orchestrator_min=ablation.orchestrator_variants:run_claim_min --extra-shape orchestrator_wide=ablation.orchestrator_variants:run_claim_wide --cases data/public_claims.jsonl --seeds 11,22,33,44,55 --out reports/ablation_investigator_tools.json

Each variant gets its own run id (`R-orchestrator_min-…`, `R-orchestrator_wide-…`)
and its own trace file. Without this, all three variants wrote
`traces/R-orchestrator-<claim>-s<seed>.json` and the last one to run overwrote
the others, so a trace could not be matched to the variant that produced it.
The orchestrator shape file is not changed: the trace is moved after the run.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from lab8.durability import InjectedCrash
from lab8.schemas import RunResult, make_run_id
from lab8.shapes.orchestrator import SHAPE as BASE_SHAPE
from lab8.shapes.orchestrator import run_with_tools

# 2 tools: only the chain the decision depends on (order -> quote).
MIN_TOOLS = ["lookup_order", "compute_compensation"]
# 5 tools: everything read-only the runner can execute from a handoff.
WIDE_TOOLS = ["get_claim_view", "lookup_order", "lookup_policy", "lookup_refund_history", "compute_compensation"]

MIN_SHAPE = "orchestrator_min"
WIDE_SHAPE = "orchestrator_wide"


def run_claim_min(claim_id: str, *, seed: int, store_path: str, crash_at: str | None = None) -> RunResult:
    return _run_variant(MIN_SHAPE, MIN_TOOLS, claim_id, seed=seed, store_path=store_path, crash_at=crash_at)


def run_claim_wide(claim_id: str, *, seed: int, store_path: str, crash_at: str | None = None) -> RunResult:
    return _run_variant(WIDE_SHAPE, WIDE_TOOLS, claim_id, seed=seed, store_path=store_path, crash_at=crash_at)


def _run_variant(
    shape: str,
    tools: list[str],
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None,
) -> RunResult:
    base_path = Path("traces") / f"{make_run_id(BASE_SHAPE, claim_id, seed)}.json"
    own_id = make_run_id(shape, claim_id, seed)
    # The shape writes to the orchestrator's trace name. Keep whatever the real
    # orchestrator wrote there and put it back afterwards.
    saved = base_path.read_bytes() if base_path.is_file() else None
    try:
        result = run_with_tools(claim_id, seed=seed, store_path=store_path, crash_at=crash_at, investigator_tools=tools)
        base_path = Path(result.trace_path)
        return dataclasses.replace(result, run_id=own_id, shape=shape, trace_path=str(_copy_trace(base_path, own_id)))
    except InjectedCrash:
        # The crashed run's trace is still evidence: keep it under the variant's name too.
        _copy_trace(base_path, own_id)
        raise
    finally:
        _restore(base_path, saved)


def _copy_trace(path: Path, run_id: str) -> Path:
    """Write the trace again under the variant's run id."""
    target = path.with_name(f"{run_id}.json")
    if path.is_file():
        body = json.loads(path.read_text(encoding="utf-8"))
        body["run_id"] = run_id
        target.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _restore(path: Path, saved: bytes | None) -> None:
    """Leave the orchestrator's trace name as it was before the variant ran."""
    if saved is not None:
        path.write_bytes(saved)
    elif path.is_file():
        path.unlink()
