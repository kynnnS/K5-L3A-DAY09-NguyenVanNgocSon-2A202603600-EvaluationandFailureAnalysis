"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _report_problems(name: str, rep: dict, spec: dict, graded: list[str]) -> list[str]:
    """Checks one report against itself: shape, coverage, grader health, costs, and that the headline
    metrics are the numbers its own rows give. Applied to the PR AND the baseline: a doctored baseline
    makes every comparison against it meaningless."""
    from lab9.eval import aggregate

    if not isinstance(rep, dict):
        return [f"{name} report is not a JSON object"]
    problems: list[str] = []
    pm, rows, metrics = rep.get("manifest"), rep.get("rows"), rep.get("metrics")
    if not isinstance(pm, dict):
        problems.append(f"{name} manifest missing")
        pm = {}
    if not isinstance(rows, list):
        problems.append(f"{name} rows missing")
        rows = []
    if not isinstance(metrics, dict):
        problems.append(f"{name} metrics block missing")
        metrics = {}
    if any(not isinstance(r, dict) for r in rows):
        problems.append(f"{name} has {sum(not isinstance(r, dict) for r in rows)} row(s) that are not objects")
        rows = [r for r in rows if isinstance(r, dict)]

    # Broken graders: a crash must never turn into "missing" and drop out of the mean.
    if "evaluator_failures" not in rep:
        problems.append(f"{name} report has no evaluator_failures field (cannot tell a green run from a crashed one)")
    elif not isinstance(rep["evaluator_failures"], list):
        problems.append(f"{name} evaluator_failures is not a list: {rep['evaluator_failures']!r}")
    elif rep["evaluator_failures"]:
        problems.append(f"{name} {len(rep['evaluator_failures'])} evaluator failure(s), e.g. {rep['evaluator_failures'][0]}")

    # Non-finite or non-numeric metric values: NaN < min is False, so a naive gate passes them.
    for k, v in metrics.items():
        if isinstance(v, float) and not math.isfinite(v):
            problems.append(f"{name} metric {k} is {v}")
        elif v is not None and not _is_num(v):
            problems.append(f"{name} metric {k} is not a number: {v!r}")
    absent = [m.get("id") for m in spec.get("metrics") or [] if m.get("id") not in metrics]
    if absent:
        problems.append(f"{name} report lacks spec metric(s) {absent} (re-run it with this spec)")

    # Coverage: every (item, seed) exactly once.
    seeds, items = pm.get("seeds"), pm.get("items")
    if not isinstance(seeds, list) or not seeds or not all(isinstance(s, int) and not isinstance(s, bool) for s in seeds):
        problems.append(f"{name} manifest seeds are invalid: {seeds!r}")
        seeds = []
    elif len(set(seeds)) != len(seeds):
        problems.append(f"{name} manifest lists duplicate seeds {seeds}")
    if not isinstance(items, int) or isinstance(items, bool) or items < 1:
        problems.append(f"{name} manifest items is invalid: {items!r}")
        items = 0
    want = items * len(set(seeds))
    if len(rows) != want:
        problems.append(f"{name} has {len(rows)} rows, manifest says {items} items x {len(set(seeds))} seeds = {want}")
    counts: dict = {}
    for r in rows:
        pair = (r.get("item_id"), r.get("seed"))
        counts[pair] = counts.get(pair, 0) + 1
    dups = sorted((p for p, c in counts.items() if c > 1), key=str)
    if dups:
        problems.append(f"{name} duplicated (item, seed) rows: {dups[:5]}")
    stray = sorted({r.get("seed") for r in rows} - set(seeds), key=str)
    if stray:
        problems.append(f"{name} rows use seed(s) {stray} that the manifest does not list")
    n_items = len({r.get("item_id") for r in rows})
    if n_items != items:
        problems.append(f"{name} rows cover {n_items} items, manifest says {items}")
    bad_slices = sorted({str(r.get("item_id")) for r in rows
                         if not isinstance(r.get("slices", []), list)
                         or not all(isinstance(s, str) for s in r.get("slices") or [])})
    if bad_slices:
        problems.append(f"{name} slice tags are not a list of names for {bad_slices[:5]}")

    bad_cells = [(r.get("item_id"), r.get("seed"), m) for r in rows for m in graded if not isinstance(r.get(m), bool)]
    if bad_cells:
        problems.append(f"{name} {len(bad_cells)} graded cell(s) are None/missing/non-boolean, e.g. {bad_cells[:3]}")

    # Cost: optional as a whole (unit fixtures carry none), but all-or-nothing and always a finite amount >= 0.
    with_cost = [r for r in rows if "cost" in r]
    bad_cost = [(r.get("item_id"), r.get("seed")) for r in with_cost
                if not _is_num(r["cost"]) or not math.isfinite(float(r["cost"])) or r["cost"] < 0]
    if bad_cost:
        problems.append(f"{name} {len(bad_cost)} row(s) have an invalid cost, e.g. {bad_cost[:3]}")
    if with_cost and len(with_cost) != len(rows):
        problems.append(f"{name} {len(rows) - len(with_cost)} row(s) have no cost while others do")
    has_cost = bool(rows) and len(with_cost) == len(rows) and not bad_cost
    if not with_cost and metrics.get("cost_per_success") is not None:
        problems.append(f"{name} reports cost_per_success but its rows carry no cost")

    # The headline numbers must be the rows' numbers (hand-edited metrics block).
    if rows and not bad_cells:
        try:
            again = aggregate([r if has_cost else {**r, "cost": 0.0} for r in rows], graded,
                              k_values=spec.get("pass_k", []))
        except Exception as exc:  # noqa: BLE001 -- a report we cannot re-aggregate is untrusted
            problems.append(f"{name} cannot recompute metrics from rows: {exc!r}")
            again = {}
        if not has_cost:
            again.pop("cost_per_success", None)
        for k, v in again.items():
            if k not in metrics:
                continue          # absent spec metrics are reported above
            got = metrics[k]
            if v is None and got is None:
                continue
            if v is None or not _is_num(got) or not math.isfinite(float(got)) or abs(float(got) - float(v)) > 1e-4:
                problems.append(f"{name} metric {k}={got!r} but the rows give {v!r}")
    return problems


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.

    Each of PR and baseline, on its own (_report_problems):
      - manifest / rows / metrics / evaluator_failures present and well-typed; evaluator_failures empty
      - every metric finite and numeric; every spec metric present
      - manifest seeds valid and unique; rows == items x seeds; no duplicated (item, seed); no stray seed
      - every graded spec metric is a boolean in every row (a crashed grader is not "missing")
      - row costs all present or all absent, finite and >= 0
      - headline metrics equal the metrics recomputed from the rows
    PR against baseline:
      - same (item, seed) set, same seeds (and the spec's seeds), same item count
      - same slice tags per item (relabelling an item out of a slice hides its regression)
    Spec against base_spec (ratchet_problems), when base_spec is given."""
    from lab9.graders import GRADERS

    graded = [m["id"] for m in spec.get("metrics") or [] if m.get("id") in GRADERS]
    problems = _report_problems("PR", pr, spec, graded) + _report_problems("baseline", base, spec, graded)

    pm = (pr.get("manifest") if isinstance(pr, dict) else None) or {}
    bm = (base.get("manifest") if isinstance(base, dict) else None) or {}
    rows = [r for r in ((pr.get("rows") if isinstance(pr, dict) else None) or []) if isinstance(r, dict)]
    brows = [r for r in ((base.get("rows") if isinstance(base, dict) else None) or []) if isinstance(r, dict)]

    pairs = {(r.get("item_id"), r.get("seed")) for r in rows}
    bpairs = {(r.get("item_id"), r.get("seed")) for r in brows}
    if pairs != bpairs:
        missing, extra = sorted(bpairs - pairs, key=str), sorted(pairs - bpairs, key=str)
        problems.append(f"(item, seed) set differs from baseline: missing {missing[:5]}, extra {extra[:5]}")
    seeds = pm.get("seeds") if isinstance(pm.get("seeds"), list) else []
    bseeds = bm.get("seeds") if isinstance(bm.get("seeds"), list) else []
    if sorted(seeds, key=str) != sorted(bseeds, key=str):
        problems.append(f"seeds {seeds} differ from baseline seeds {bseeds}")
    if spec.get("seeds") and sorted(seeds, key=str) != sorted(spec["seeds"], key=str):
        problems.append(f"seeds {seeds} differ from the spec's seeds {spec['seeds']}")
    if pm.get("items") != bm.get("items"):
        problems.append(f"manifest items {pm.get('items')} != baseline {bm.get('items')}")

    # Slice tags are part of the eval set, not of the PR.
    btags = {r.get("item_id"): sorted(map(str, r.get("slices") or [])) for r in brows}
    moved = sorted({r.get("item_id") for r in rows
                    if r.get("item_id") in btags and sorted(map(str, r.get("slices") or [])) != btags[r.get("item_id")]},
                   key=str)
    if moved:
        problems.append(f"slice tags differ from baseline for {moved[:5]}")

    if base_spec is not None:
        problems += ratchet_problems(spec, base_spec)
    return problems


DATA_KEYS = ("eval_set", "labels", "adversarial_pack")


def _data_fingerprint(spec: dict) -> tuple:
    """What the eval set IS, not what it is called: content hash of each data file (path if unreadable)."""
    import hashlib

    out = []
    for key in DATA_KEYS:
        p = spec.get(key)
        if not p:
            out.append((key, None))
            continue
        try:
            out.append((key, hashlib.sha256(Path(p).read_bytes()).hexdigest()))
        except OSError:
            out.append((key, f"path:{p}"))
    return tuple(out)


def ratchet_problems(spec: dict, base_spec: dict) -> list[str]:
    """Same eval-set version => the spec may only get stricter. A new version must come with new data."""
    problems: list[str] = []
    same_data = _data_fingerprint(spec) == _data_fingerprint(base_spec)
    if spec.get("eval_set_version") != base_spec.get("eval_set_version"):
        if same_data:
            problems.append("RATCHET eval_set_version changed but the eval set / labels / pack content did not")
        return problems
    if not same_data:
        problems.append("RATCHET eval set / labels / pack content changed without an eval_set_version bump")
    if sorted(spec.get("seeds") or [], key=str) != sorted(base_spec.get("seeds") or [], key=str):
        problems.append(f"RATCHET seeds changed {base_spec.get('seeds')} -> {spec.get('seeds')}")

    bb, sb = base_spec.get("bootstrap") or {}, spec.get("bootstrap") or {}
    for key, default in (("confidence", 0.95), ("n_resamples", 1000)):
        old, new = bb.get(key, default), sb.get(key, default)
        if not _is_num(new) or (_is_num(old) and new < old):
            problems.append(f"RATCHET bootstrap {key} lowered {old} -> {new}")
    for key in ("rng", "method"):
        if bb.get(key) != sb.get(key):
            problems.append(f"RATCHET bootstrap {key} changed {bb.get(key)!r} -> {sb.get(key)!r} (re-rolling the CI)")

    ids = [m.get("id") for m in spec.get("metrics") or []]
    keys = [(s.get("name"), s.get("metric")) for s in spec.get("slices") or []]
    for kind, seq in (("metric", ids), ("slice", keys)):
        dup = sorted({str(x) for x in seq if seq.count(x) > 1})
        if dup:
            problems.append(f"RATCHET duplicate {kind} entries {dup}: one copy can shadow the other")

    def compare(kind: str, key: str, old: dict, new: dict | None, th_old: dict, th_new: dict) -> None:
        if new is None:
            problems.append(f"RATCHET {kind} {key} removed")
            return
        if old.get("blocking") and new.get("blocking") is not True:
            problems.append(f"RATCHET {kind} {key} no longer blocking")
        for t in ("max", "max_increase"):
            if t in th_old and (t not in th_new or not _is_num(th_new[t]) or th_new[t] > th_old[t]):
                problems.append(f"RATCHET {kind} {key} {t} loosened {th_old[t]} -> {th_new.get(t)}")
        if "min" in th_old and ("min" not in th_new or not _is_num(th_new["min"]) or th_new["min"] < th_old["min"]):
            problems.append(f"RATCHET {kind} {key} min loosened {th_old['min']} -> {th_new.get('min')}")
        if th_old.get("no_significant_drop") and th_new.get("no_significant_drop") is not True:
            problems.append(f"RATCHET {kind} {key} dropped no_significant_drop")

    new_metrics = {m.get("id"): m for m in spec.get("metrics") or []}
    for m in base_spec.get("metrics") or []:
        n = new_metrics.get(m.get("id"))
        compare("metric", m.get("id"), m, n, m.get("threshold") or {}, (n or {}).get("threshold") or {})
    new_slices = {(s.get("name"), s.get("metric")): s for s in spec.get("slices") or []}
    for s in base_spec.get("slices") or []:
        n = new_slices.get((s.get("name"), s.get("metric")))
        compare("slice", f"{s.get('name')}/{s.get('metric')}", s, n, s, n or {})
    return problems


def _ci(a: dict[str, float], b: dict[str, float], bs: dict) -> tuple[float, float, float] | None:
    """Paired bootstrap of (PR - main) over the items both reports grade, with the spec's bootstrap
    settings (n_resamples, rng AND confidence). None when there is nothing to pair."""
    keys = sorted(set(a) & set(b))
    if not keys:
        return None
    return stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                  n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027),
                                  confidence=bs.get("confidence", 0.95))


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        (spec bootstrap: n_resamples, rng, confidence) lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for sl in spec.get("slices") or []:
        name, metric = sl["name"], sl["metric"]
        sid = f"slice:{name}/{metric}"
        a, b = per_item(pr, metric, slice_name=name), per_item(base, metric, slice_name=name)
        verdict, note, val = "ok", "", None
        if not a:
            # An empty slice is not a pass: either the items were relabelled away or never graded.
            verdict = "missing"
        else:
            val = round(sum(a.values()) / len(a), 4)
            if "min" in sl and val < sl["min"]:
                verdict = "fail"
            if "max" in sl and val > sl["max"]:
                verdict = "fail"
            if sl.get("no_significant_drop"):
                ci = _ci(a, b, bs)
                if ci is None:
                    verdict, note = "missing", "no baseline items to pair"   # cannot test the drop: not a pass
                else:
                    d, lo, hi = ci
                    note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                    if hi < 0:
                        verdict = "fail"
        if verdict != "ok" and sl.get("blocking"):
            blocked.append(sid)
        th = {k: sl[k] for k in ("min", "max", "no_significant_drop") if k in sl}
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if sl.get("blocking") else "warn")
        lines.append(f"  {sid:<38} {val!s:<8} n={len(a):<3} {json.dumps(th):<34} {note:<28} {label}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    sha = lambda rep: ((rep.get("manifest") if isinstance(rep, dict) else None) or {}).get("eval_set_sha")
    if sha(pr) is not None and sha(base) is not None and sha(pr) != sha(base):
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if sha(pr) is None or sha(base) is None:
        problems.append("manifest eval_set_sha missing: cannot tell which eval set was run")
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            ci = _ci(per_item(pr, mid), per_item(base, mid), bs)
            if ci is None:
                verdict, note = "missing", "not a per-row metric: no paired CI"   # never a silent pass
            else:
                d, lo, hi = ci
                note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                if hi < 0:
                    verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
