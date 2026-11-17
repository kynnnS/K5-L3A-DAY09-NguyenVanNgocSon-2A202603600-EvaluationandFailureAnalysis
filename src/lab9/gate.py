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


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    from lab9.eval import aggregate
    from lab9.graders import GRADERS

    problems: list[str] = []
    pm, bm = pr.get("manifest") or {}, base.get("manifest") or {}
    rows, brows = pr.get("rows") or [], base.get("rows") or []

    # 1. Non-finite or non-numeric metric values: NaN < min is False, so a naive gate passes them.
    for name, rep in (("PR", pr), ("baseline", base)):
        for k, v in (rep.get("metrics") or {}).items():
            if isinstance(v, float) and not math.isfinite(v):
                problems.append(f"{name} metric {k} is {v}")
            elif v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                problems.append(f"{name} metric {k} is not a number: {v!r}")

    gone = [m["id"] for m in spec.get("metrics") or []
            if m["id"] in (base.get("metrics") or {}) and m["id"] not in (pr.get("metrics") or {})]
    if gone:
        problems.append(f"spec metric(s) {gone} present in the baseline but missing from the PR report")

    # 2. Row count and coverage: every (item, seed) exactly once, same set as the baseline.
    seeds = list(pm.get("seeds") or [])
    want = int(pm.get("items") or 0) * len(seeds)
    if len(rows) != want:
        problems.append(f"{len(rows)} rows, manifest says {pm.get('items')} items x {len(seeds)} seeds = {want}")
    pairs = [(r.get("item_id"), r.get("seed")) for r in rows]
    counts: dict = {}
    for pair in pairs:
        counts[pair] = counts.get(pair, 0) + 1
    dups = sorted((p for p, c in counts.items() if c > 1), key=str)
    if dups:
        problems.append(f"duplicated (item, seed) rows: {dups[:5]}")
    bpairs = {(r.get("item_id"), r.get("seed")) for r in brows}
    if set(pairs) != bpairs:
        missing, extra = sorted(bpairs - set(pairs), key=str), sorted(set(pairs) - bpairs, key=str)
        problems.append(f"(item, seed) set differs from baseline: missing {missing[:5]}, extra {extra[:5]}")
    if sorted(seeds) != sorted(bm.get("seeds") or []):
        problems.append(f"seeds {seeds} differ from baseline seeds {bm.get('seeds')}")
    if spec.get("seeds") and sorted(seeds) != sorted(spec["seeds"]):
        problems.append(f"seeds {seeds} differ from the spec's seeds {spec['seeds']}")
    if pm.get("items") != bm.get("items"):
        problems.append(f"manifest items {pm.get('items')} != baseline {bm.get('items')}")
    n_items = len({r.get("item_id") for r in rows})
    if n_items != pm.get("items"):
        problems.append(f"rows cover {n_items} items, manifest says {pm.get('items')}")

    # 3. Broken graders: a crash must never turn into "missing" and drop out of the mean.
    if pr.get("evaluator_failures"):
        problems.append(f"{len(pr['evaluator_failures'])} evaluator failure(s), e.g. {pr['evaluator_failures'][0]}")
    graded = [m["id"] for m in spec.get("metrics") or [] if m["id"] in GRADERS]
    bad_cells = [(r.get("item_id"), r.get("seed"), m) for r in rows for m in graded
                 if not isinstance(r.get(m), bool)]
    if bad_cells:
        problems.append(f"{len(bad_cells)} graded cell(s) are None/missing/non-boolean, e.g. {bad_cells[:3]}")

    # 4. The headline numbers must be the rows' numbers (hand-edited metrics block).
    if rows and not bad_cells:
        has_cost = all(isinstance(r.get("cost"), (int, float)) for r in rows)
        try:
            again = aggregate([r if has_cost else {**r, "cost": 0.0} for r in rows], graded,
                              k_values=spec.get("pass_k", []))
        except Exception as exc:  # noqa: BLE001 -- a report we cannot re-aggregate is untrusted
            problems.append(f"cannot recompute metrics from rows: {exc!r}")
            again = {}
        if not has_cost:
            again.pop("cost_per_success", None)
        reported = pr.get("metrics") or {}
        for k, v in again.items():
            if k not in reported:
                continue          # a missing metric is reported as MISSING by check()
            got = reported[k]
            if v is None and got is None:
                continue
            if v is None or not isinstance(got, (int, float)) or isinstance(got, bool) \
                    or not math.isfinite(float(got)) or abs(float(got) - float(v)) > 1e-4:
                problems.append(f"metric {k}={got!r} but the rows give {v!r}")

    # 5. Slice tags are part of the eval set, not of the PR.
    btags = {r.get("item_id"): sorted(r.get("slices") or []) for r in brows}
    moved = sorted({r.get("item_id") for r in rows
                    if r.get("item_id") in btags and sorted(r.get("slices") or []) != btags[r.get("item_id")]}, key=str)
    if moved:
        problems.append(f"slice tags differ from baseline for {moved[:5]}")

    # 6. Ratchet: same eval-set version => thresholds may only get stricter.
    if base_spec is not None:
        problems += ratchet_problems(spec, base_spec)
    return problems


def ratchet_problems(spec: dict, base_spec: dict) -> list[str]:
    problems: list[str] = []
    if spec.get("eval_set_version") != base_spec.get("eval_set_version"):
        if spec.get("eval_set") == base_spec.get("eval_set") and spec.get("labels") == base_spec.get("labels"):
            problems.append("RATCHET eval_set_version changed but the eval set files did not")
        return problems
    if sorted(spec.get("seeds") or []) != sorted(base_spec.get("seeds") or []):
        problems.append(f"RATCHET seeds changed {base_spec.get('seeds')} -> {spec.get('seeds')}")
    bb, sb = base_spec.get("bootstrap") or {}, spec.get("bootstrap") or {}
    if sb.get("confidence", 0.95) < bb.get("confidence", 0.95):
        problems.append("RATCHET bootstrap confidence lowered")

    def compare(kind: str, key: str, old: dict, new: dict | None, th_old: dict, th_new: dict) -> None:
        if new is None:
            problems.append(f"RATCHET {kind} {key} removed")
            return
        if old.get("blocking") and not new.get("blocking"):
            problems.append(f"RATCHET {kind} {key} no longer blocking")
        for t in ("max", "max_increase"):
            if t in th_old and (t not in th_new or th_new[t] > th_old[t]):
                problems.append(f"RATCHET {kind} {key} {t} loosened {th_old[t]} -> {th_new.get(t)}")
        if "min" in th_old and ("min" not in th_new or th_new["min"] < th_old["min"]):
            problems.append(f"RATCHET {kind} {key} min loosened {th_old['min']} -> {th_new.get('min')}")
        if th_old.get("no_significant_drop") and not th_new.get("no_significant_drop"):
            problems.append(f"RATCHET {kind} {key} dropped no_significant_drop")

    new_metrics = {m["id"]: m for m in spec.get("metrics") or []}
    for m in base_spec.get("metrics") or []:
        n = new_metrics.get(m["id"])
        compare("metric", m["id"], m, n, m.get("threshold") or {}, (n or {}).get("threshold") or {})
    new_slices = {(s["name"], s["metric"]): s for s in spec.get("slices") or []}
    for s in base_spec.get("slices") or []:
        n = new_slices.get((s["name"], s["metric"]))
        compare("slice", f"{s['name']}/{s['metric']}", s, n, s, n or {})
    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
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
            keys = sorted(set(a) & set(b))
            if sl.get("no_significant_drop") and keys:
                d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                                   n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
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
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
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
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
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
