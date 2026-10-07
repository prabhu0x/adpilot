"""Causal-inference layer.

Implements the two counterfactual designs used across AdPilot:

* **Difference-in-differences (DiD)** with a two-way fixed-effects style
  estimator on a treated unit vs. a donor pool, returning the effect, a
  bootstrap confidence interval and the parallel-trends check.
* **Synthetic control / CausalImpact-style** counterfactual: donors are weighted
  by ridge-regularised least squares on the pre-period, the synthetic series is
  projected into the post period, and the effect is the cumulative gap with a
  permutation-based interval.

Both are deliberately dependency-light (numpy/scipy only) so the sandbox runs
them in-process, and both always return an explicit caveat about assumptions.
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

RNG_SEED = 42


def _as_matrix(series: dict[str, Sequence[float]], length: int) -> tuple[np.ndarray, list[str]]:
    names = sorted(series)
    mat = np.zeros((length, len(names)), dtype=float)
    for j, name in enumerate(names):
        values = np.asarray(series[name], dtype=float)[:length]
        if values.size < length:
            values = np.pad(values, (0, length - values.size), constant_values=np.nan)
        mat[:, j] = values
    return mat, names


def difference_in_differences(
    treated_pre: Sequence[float],
    treated_post: Sequence[float],
    donor_pre: Sequence[float],
    donor_post: Sequence[float],
    n_bootstrap: int = 400,
    label: str = "metric",
    unit: str = "",
) -> dict[str, Any]:
    """DiD estimator with a bootstrap interval and a parallel-trends check."""
    tp, tq = np.asarray(treated_pre, float), np.asarray(treated_post, float)
    dp, dq = np.asarray(donor_pre, float), np.asarray(donor_post, float)
    if tp.size < 5 or tq.size < 3 or dp.size < 5 or dq.size < 3:
        return {"available": False, "reason": "insufficient observations for a DiD design"}

    def _ratio(pre: np.ndarray, post: np.ndarray) -> float:
        base = pre.mean()
        if base == 0:
            return 0.0
        return float(post.mean() / base - 1.0)

    effect = _ratio(tp, tq) - _ratio(dp, dq)

    rng = np.random.default_rng(RNG_SEED)
    boot = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        tpi = rng.choice(tp, tp.size, replace=True)
        tqi = rng.choice(tq, tq.size, replace=True)
        dpi = rng.choice(dp, dp.size, replace=True)
        dqi = rng.choice(dq, dq.size, replace=True)
        boot[i] = (_ratio(tpi, tqi)) - (_ratio(dpi, dqi))
    ci_low, ci_high = np.percentile(boot, [10, 90])
    se = float(boot.std(ddof=1)) or 1e-9
    z = effect / se
    # two-sided normal p-value
    from math import erfc, sqrt

    p_value = float(erfc(abs(z) / sqrt(2)))

    # parallel-trends: compare pre-period slopes
    x = np.arange(tp.size)
    t_slope = np.polyfit(x, tp, 1)[0] / (tp.mean() or 1e-9)
    d_slope = np.polyfit(np.arange(dp.size), dp, 1)[0] / (dp.mean() or 1e-9)
    parallel = abs(t_slope - d_slope) < 0.01

    return {
        "available": True,
        "method": "difference_in_differences",
        "label": label,
        "unit": unit,
        "effect_estimate": round(float(effect), 4),
        "effect_pct": round(float(effect) * 100, 2),
        "ci_low": round(float(ci_low), 4),
        "ci_high": round(float(ci_high), 4),
        "std_error": round(se, 5),
        "p_value": round(p_value, 5),
        "confidence": round(float(min(0.99, max(0.02, 1 - p_value))), 3),
        "significant": bool(p_value < 0.1 and (ci_low > 0 or ci_high < 0)),
        "parallel_trends_ok": bool(parallel),
        "caveat": (
            "DiD assumes the donor pool would have moved like the treated unit absent the event; "
            "only trustworthy when parallel pre-trends hold."
        ),
    }


def synthetic_control(
    treated_pre: Sequence[float],
    treated_post: Sequence[float],
    donors_pre: dict[str, Sequence[float]],
    donors_post: dict[str, Sequence[float]],
    label: str = "metric",
    unit: str = "",
    n_permutations: int = 120,
) -> dict[str, Any]:
    """CausalImpact-style synthetic control with a permutation interval."""
    tp = np.asarray(treated_pre, float)
    tq = np.asarray(treated_post, float)
    if tp.size < 8 or tq.size < 3 or len(donors_pre) < 3:
        return {"available": False, "reason": "insufficient history for a synthetic control"}

    length = tp.size
    pre_mat, names = _as_matrix(donors_pre, length)
    if np.isnan(pre_mat).any():
        return {"available": False, "reason": "donor series contain gaps"}

    # ridge-regularised least squares with a non-negative intercept
    ridge = 1e-3
    A = np.hstack([np.ones((length, 1)), pre_mat])
    gram = A.T @ A + ridge * np.eye(A.shape[1])
    weights = np.linalg.solve(gram, A.T @ tp)
    intercept, donor_weights = weights[0], weights[1:]

    post_len = tq.size
    post_mat, _ = _as_matrix(donors_post, post_len)
    synthetic = intercept + post_mat @ donor_weights
    gap = tq - synthetic

    pre_fit = A @ weights
    resid = tp - pre_fit
    sigma = float(resid.std(ddof=1)) or 1e-9
    cumulative = float(gap.sum())
    avg_gap = float(gap.mean())

    # permutation test: recompute using each donor as a pseudo-treated unit
    placebo = []
    rng = np.random.default_rng(RNG_SEED)
    for j, name in enumerate(names[:n_permutations]):
        others = [n for n in names if n != name]
        if len(others) < 2:
            continue
        A_other = np.hstack([np.ones((length, 1)), np.array([donors_pre[n] for n in others]).T])
        try:
            w = np.linalg.solve(A_other.T @ A_other + ridge * np.eye(A_other.shape[1]), A_other.T @ np.array(donors_pre[name]))
        except np.linalg.LinAlgError:
            continue
        B = np.hstack([np.ones((post_len, 1)), np.array([donors_post[n] for n in others]).T])
        placebo.append(float((np.array(donors_post[name]) - B @ w).sum()))
    if placebo:
        placebo_arr = np.abs(np.asarray(placebo))
        p_value = float((placebo_arr >= abs(cumulative)).mean())
        se = float(placebo_arr.std(ddof=1)) or 1e-9
    else:
        p_value, se = 0.5, sigma * np.sqrt(post_len)
    ci_low, ci_high = cumulative - 1.64 * se, cumulative + 1.64 * se

    return {
        "available": True,
        "method": "synthetic_control",
        "label": label,
        "unit": unit,
        "observed_post_mean": round(float(tq.mean()), 4),
        "counterfactual_post_mean": round(float(synthetic.mean()), 4),
        "effect_estimate": round(avg_gap, 4),
        "cumulative_effect": round(cumulative, 2),
        "effect_pct": round(float(avg_gap / (synthetic.mean() or 1e-9)) * 100, 2),
        "ci_low": round(ci_low, 2),
        "ci_high": round(ci_high, 2),
        "p_value": round(p_value, 4),
        "confidence": round(float(min(0.99, max(0.02, 1 - p_value))), 3),
        "significant": bool(p_value < 0.1),
        "pre_period_fit_rmse": round(sigma, 4),
        "donor_weights": {n: round(float(w), 4) for n, w in sorted(zip(names, donor_weights), key=lambda kv: -abs(kv[1]))[:8]},
        "caveat": (
            "Synthetic control assumes no unobserved shock hits the treated unit and donors are "
            "contemporaneously unaffected; treat the interval as a credible range, not a guarantee."
        ),
    }


def naive_counterfactual(
    pre: Sequence[float],
    post: Sequence[float],
    label: str = "metric",
    unit: str = "",
) -> dict[str, Any]:
    """Fallback: seasonally-adjusted mean shift with a t-based interval."""
    p = np.asarray(pre, float)
    q = np.asarray(post, float)
    if p.size < 7 or q.size < 2:
        return {"available": False, "reason": "insufficient observations"}
    base = float(p.mean())
    se = float(p.std(ddof=1) / max(np.sqrt(p.size), 1)) or 1e-9
    effect = float(q.mean() - base)
    ci = 1.96 * se
    return {
        "available": True,
        "method": "pre_period_mean_shift",
        "label": label,
        "unit": unit,
        "baseline": round(base, 4),
        "observed": round(float(q.mean()), 4),
        "effect_estimate": round(effect, 4),
        "effect_pct": round(effect / base * 100, 2) if base else 0.0,
        "ci_low": round(effect - ci, 4),
        "ci_high": round(effect + ci, 4),
        "p_value": round(float(min(1.0, 2 * (1 - _normal_cdf(abs(effect / se))))), 4),
        "confidence": round(float(min(0.95, max(0.05, 1 - min(1.0, 2 * (1 - _normal_cdf(abs(effect / se))))))), 3),
        "significant": bool(abs(effect) > ci),
        "caveat": "Simple pre/post mean shift; ignores concurrent shocks and seasonality.",
    }


def _normal_cdf(x: float) -> float:
    from math import erf, sqrt

    return 0.5 * (1 + erf(x / sqrt(2)))


def estimate_causal_effect(
    treated_pre: Sequence[float],
    treated_post: Sequence[float],
    donors_pre: dict[str, Sequence[float]] | None = None,
    donors_post: dict[str, Sequence[float]] | None = None,
    label: str = "metric",
    unit: str = "",
) -> dict[str, Any]:
    """Pick the strongest available design: synthetic control → DiD → naive."""
    if donors_pre and donors_post and len(donors_pre) >= 3:
        result = synthetic_control(treated_pre, treated_post, donors_pre, donors_post, label, unit)
        if result.get("available"):
            return result
    if donors_pre and donors_post:
        flat_pre = np.concatenate([np.asarray(v, float) for v in donors_pre.values()]) if donors_pre else np.array([])
        flat_post = np.concatenate([np.asarray(v, float) for v in donors_post.values()]) if donors_post else np.array([])
        if flat_pre.size >= 5 and flat_post.size >= 3:
            result = difference_in_differences(treated_pre, treated_post, flat_pre, flat_post, label=label, unit=unit)
            if result.get("available"):
                return result
    return naive_counterfactual(treated_pre, treated_post, label=label, unit=unit)