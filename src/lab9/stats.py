"""Statistics students implement (M2 judge calibration, M4 paired bootstrap).

Rules: a judge is a classifier -- measure it like one. A higher mean is not
"better" until the paired CI of the difference excludes 0.
Reference values to self-check are in tests/test_lab9_stats.py.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Sequence
from fractions import Fraction

POS = "FAIL"   # the judge's job is to FIND failures, so FAIL is the positive class
NEG = "PASS"


def _binary_label(v: object, who: str, i: int) -> bool:
    """True for FAIL, False for PASS (case/whitespace-insensitive). Anything else is an error:
    a blank or "N/A" cell silently counted as PASS would inflate precision and hide judge misses."""
    s = v.strip().upper() if isinstance(v, str) else None
    if s not in (POS, NEG):
        raise ValueError(f"{who} label #{i} is {v!r}: expected 'PASS' or 'FAIL'")
    return s == POS


def confusion(human: Sequence[str], judge: Sequence[str]) -> dict[str, int]:
    """tp/fp/fn/tn with FAIL as positive."""
    if isinstance(human, str) or isinstance(judge, str):
        raise ValueError("confusion needs two sequences of labels, not a string")
    human, judge = list(human), list(judge)
    if len(human) != len(judge):
        raise ValueError(f"length mismatch: {len(human)} human vs {len(judge)} judge labels")
    cm = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for i, (h, j) in enumerate(zip(human, judge)):
        h_pos = _binary_label(h, "human", i)
        j_pos = _binary_label(j, "judge", i)
        cm[("t" if h_pos == j_pos else "f") + ("p" if j_pos else "n")] += 1
    return cm


def _unit_interval(name: str, v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, numbers.Real) or not math.isfinite(float(v)):
        raise ValueError(f"{name} must be a finite number, got {v!r}")
    if not 0.0 <= float(v) <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {v!r}")
    return float(v)


def precision_recall(human: Sequence[str], judge: Sequence[str]) -> tuple[float, float]:
    cm = confusion(human, judge)
    precision = cm["tp"] / (cm["tp"] + cm["fp"]) if cm["tp"] + cm["fp"] else 0.0
    recall = cm["tp"] / (cm["tp"] + cm["fn"]) if cm["tp"] + cm["fn"] else 0.0
    return precision, recall


def cohen_kappa(human: Sequence[str], judge: Sequence[str]) -> float:
    """kappa = (p_o - p_e) / (1 - p_e). Raise ValueError when undefined (p_e == 1):
    a constant judge has no signal and must FAIL calibration, not pass on a technicality."""
    cm = confusion(human, judge)
    n = sum(cm.values())
    if n == 0:
        raise ValueError("kappa undefined: no labels")
    p_o = (cm["tp"] + cm["tn"]) / n
    human_pos, judge_pos = (cm["tp"] + cm["fn"]) / n, (cm["tp"] + cm["fp"]) / n
    p_e = human_pos * judge_pos + (1 - human_pos) * (1 - judge_pos)
    if math.isclose(p_e, 1.0):
        raise ValueError("kappa undefined: expected agreement is 1 (constant labels)")
    return (p_o - p_e) / (1 - p_e)


def rogan_gladen(observed_rate: float, sensitivity: float, specificity: float) -> float:
    """Correct an observed failure rate for judge error; clip to [0, 1]."""
    observed_rate = _unit_interval("observed_rate", observed_rate)
    sensitivity = _unit_interval("sensitivity", sensitivity)
    specificity = _unit_interval("specificity", specificity)
    denom = sensitivity + specificity - 1
    if denom <= 0:
        raise ValueError("Rogan-Gladen undefined: judge is no better than chance (sens + spec <= 1)")
    return min(1.0, max(0.0, (observed_rate + specificity - 1) / denom))


def paired_bootstrap(a: Sequence[float], b: Sequence[float], *, n_resamples: int = 1000,
                     rng: int = 2027, confidence: float = 0.95) -> tuple[float, float, float]:
    """a[i], b[i] = per-item mean over seeds, SAME items, SAME order.
    Return (mean(a)-mean(b), lo, hi) with scipy.stats.bootstrap(paired=True, method="percentile",
    rng=numpy.random.default_rng(rng)). If a == b everywhere return (0.0, 0.0, 0.0)."""
    import numpy as np
    from scipy.stats import bootstrap

    if isinstance(n_resamples, bool) or not isinstance(n_resamples, numbers.Integral) or n_resamples < 1:
        raise ValueError(f"n_resamples must be a positive int, got {n_resamples!r}")
    if isinstance(rng, bool) or not isinstance(rng, numbers.Integral):
        raise ValueError(f"rng must be an int seed, got {rng!r}")
    if isinstance(confidence, bool) or not isinstance(confidence, numbers.Real) \
            or not math.isfinite(float(confidence)) or not 0.0 < float(confidence) < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence!r}")
    if isinstance(a, (str, bytes)) or isinstance(b, (str, bytes)):
        raise ValueError("paired bootstrap needs numeric vectors, not strings")
    try:
        x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"paired bootstrap needs numeric vectors: {exc}") from None
    if x.shape != y.shape or x.ndim != 1 or x.size == 0:
        raise ValueError(f"paired bootstrap needs two equal-length, non-empty vectors, got {x.shape} and {y.shape}")
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y))):
        raise ValueError("paired bootstrap got NaN/inf")
    diff = x - y
    d = float(diff.mean())
    if np.all(diff == diff[0]):
        # Degenerate: every resample gives the same mean; scipy would warn and return NaN.
        return (d, d, d) if d else (0.0, 0.0, 0.0)

    def mean_diff(u, v, axis=-1):
        return np.mean(u, axis=axis) - np.mean(v, axis=axis)

    res = bootstrap((x, y), mean_diff, paired=True, vectorized=True, n_resamples=int(n_resamples),
                    confidence_level=float(confidence), method="percentile", rng=np.random.default_rng(int(rng)))
    return d, float(res.confidence_interval.low), float(res.confidence_interval.high)


def pass_at_k(n: int, c: int, k: int) -> float:
    """Probability that AT LEAST ONE of k trials drawn from n (c of them passing) passes.
    Unbiased estimator (Chen et al. 2021): 1 - C(n-c, k) / C(n, k). Raise ValueError if k > n."""
    _check_nck(n, c, k)
    n, c, k = int(n), int(c), int(k)
    return float(1 - Fraction(math.comb(n - c, k), math.comb(n, k)))


def pass_hat_k(n: int, c: int, k: int) -> float:
    """pass^k (tau-bench, Yao et al. 2024): probability that ALL k trials drawn from n pass.
    Unbiased estimator: C(c, k) / C(n, k). Raise ValueError if k > n.
    pass^k <= pass@1 <= pass@k; a reliable agent keeps pass^k close to pass@1."""
    _check_nck(n, c, k)
    n, c, k = int(n), int(c), int(k)
    return float(Fraction(math.comb(c, k), math.comb(n, k)))


def _check_nck(n: int, c: int, k: int) -> None:
    for name, v in (("n", n), ("c", c), ("k", k)):
        if isinstance(v, bool) or not isinstance(v, numbers.Integral):
            raise ValueError(f"{name} must be an int, got {v!r}")
    if n < 1 or k < 1:
        raise ValueError(f"need n >= 1 and k >= 1, got n={n}, k={k}")
    if not 0 <= c <= n:
        raise ValueError(f"need 0 <= c <= n, got c={c}, n={n}")
    if k > n:
        raise ValueError(f"k={k} > n={n}: cannot draw k trials from n")
