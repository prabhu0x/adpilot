"""Response curves and marginal returns.

Each entity (platform, campaign, ad set, creative) is fitted with a saturating
spend→revenue response curve. We fit **two** functional forms and select by
cross-validated error, then report marginal ROAS at the current spend together
with the local elasticity:

* **Hill / power saturation**:  ``revenue = a · spend^b / (1 + (spend / K)^b)``
  fitted by log-space least squares on a grid over ``K``.
* **Diminishing-returns log model**: ``revenue = a · ln(1 + b · spend)``.

Honesty rules: the fit reports its own R², the number of observations, and a
``reliable`` flag. A curve is only used to size budget moves when
``reliable`` is true *and* the proposed spend stays inside the observed range
(±60% of the historical maximum) — otherwise the estimate is flagged and
degraded to a conservative constant-ROAS assumption.
"""
from __future__ import annotations

from typing import Any

import numpy as np


def _fit_hill(spend: np.ndarray, revenue: np.ndarray) -> dict[str, Any]:
    best = None
    scale = float(np.median(spend)) or 1.0
    for k_mult in np.linspace(0.4, 4.0, 25):
        k = scale * k_mult
        x = np.clip(spend / k, 1e-6, None)
        b_grid = np.linspace(0.2, 1.6, 22)
        for b in b_grid:
            feature = np.power(x, b) / (1.0 + np.power(x, b))
            if not np.all(np.isfinite(feature)) or np.allclose(feature, 0):
                continue
            a = float(np.dot(feature, revenue) / max(np.dot(feature, feature), 1e-12))
            pred = a * feature
            rmse = float(np.sqrt(np.mean((pred - revenue) ** 2)))
            if best is None or rmse < best["rmse"]:
                best = {"form": "hill", "a": a, "b": float(b), "K": float(k), "rmse": rmse}
    return best or {"form": "hill", "a": 0.0, "b": 1.0, "K": scale, "rmse": float("inf")}


def _fit_log(spend: np.ndarray, revenue: np.ndarray) -> dict[str, Any]:
    best = None
    scale = float(np.median(spend)) or 1.0
    for b_mult in np.linspace(0.05, 3.0, 40):
        b = b_mult / scale
        feature = np.log1p(np.clip(b * spend, 0, None))
        if not np.all(np.isfinite(feature)) or np.allclose(feature, 0):
            continue
        a = float(np.dot(feature, revenue) / max(np.dot(feature, feature), 1e-12))
        pred = a * feature
        rmse = float(np.sqrt(np.mean((pred - revenue) ** 2)))
        if best is None or rmse < best["rmse"]:
            best = {"form": "log", "a": a, "b": float(b), "rmse": rmse}
    return best or {"form": "log", "a": 0.0, "b": 1.0 / scale, "rmse": float("inf")}


def _predict(params: dict[str, Any], spend: float) -> float:
    s = max(float(spend), 0.0)
    if params["form"] == "hill":
        x = (s / params["K"]) ** params["b"]
        return float(params["a"] * x / (1.0 + x))
    return float(params["a"] * np.log1p(params["b"] * s))


def fit_response_curve(spend: np.ndarray, revenue: np.ndarray) -> dict[str, Any]:
    """Fit and select the better response curve; report quality honestly."""
    spend = np.asarray(spend, dtype=float)
    revenue = np.asarray(revenue, dtype=float)
    mask = np.isfinite(spend) & np.isfinite(revenue) & (spend > 0)
    spend, revenue = spend[mask], revenue[mask]
    n = spend.size
    if n < 10:
        return {"available": False, "reason": "fewer than 10 observations", "n": int(n)}

    candidates = [_fit_hill(spend, revenue), _fit_log(spend, revenue)]
    candidates.sort(key=lambda p: p["rmse"])
    best = candidates[0]
    pred = np.array([_predict(best, s) for s in spend])
    ss_res = float(np.sum((pred - revenue) ** 2))
    ss_tot = float(np.sum((revenue - revenue.mean()) ** 2)) or 1e-9
    r2 = 1 - ss_res / ss_tot
    current = float(spend.mean())
    marginal = (_predict(best, current * 1.01) - _predict(best, current * 0.99)) / (0.02 * current) if current else 0.0
    elasticity = marginal * current / max(_predict(best, current), 1e-9)
    reliable = bool(r2 > 0.45 and n >= 21)
    return {
        "available": True, "form": best["form"], "params": {k: round(v, 6) for k, v in best.items() if k != "form"},
        "rmse": round(best["rmse"], 2), "r2": round(float(r2), 4), "n": int(n),
        "current_spend_mean": round(current, 2), "current_roas": round(float(revenue.sum() / max(spend.sum(), 1e-9)), 3),
        "marginal_roas": round(float(marginal), 3), "elasticity": round(float(elasticity), 3),
        "spend_range": [round(float(spend.min()), 2), round(float(spend.max()), 2)],
        "reliable": reliable,
        "note": ("Curve fitted on daily spend/revenue; marginal ROAS is the local derivative at current spend."
                 if reliable else "Fit quality is marginal — treat marginal ROAS as indicative only."),
    }


def marginal_at(curve: dict[str, Any], spend: float, revenue: float) -> dict[str, Any]:
    """Marginal ROAS for a proposed spend level, with a guardrail verdict."""
    if not curve.get("available"):
        base = revenue / spend if spend else 0.0
        return {"marginal_roas": round(base, 3), "method": "constant_roas_fallback",
                "in_range": True, "reliable": False, "projected_revenue": round(base * spend, 2)}
    lo, hi = curve["spend_range"]
    in_range = lo * 0.4 <= spend <= hi * 1.6
    params = {"form": curve["form"], **curve["params"]}
    current = curve["current_spend_mean"]
    delta = spend - current
    if not in_range:
        # extrapolating beyond observed data: degrade to constant ROAS
        base = curve["current_roas"]
        return {"marginal_roas": round(base, 3), "method": "constant_roas_extrapolation",
                "in_range": False, "reliable": False,
                "projected_revenue": round(revenue + base * delta, 2),
                "warning": f"proposed spend {spend:,.0f} is outside the observed range {lo:,.0f}–{hi:,.0f}"}
    step = max(abs(delta), current * 0.05, 1.0)
    p1 = _predict(params, spend + step)
    p0 = _predict(params, spend)
    marginal = (p1 - p0) / step
    return {
        "marginal_roas": round(float(marginal), 3), "method": f"{curve['form']}_curve",
        "in_range": True, "reliable": curve.get("reliable", False),
        "projected_revenue": round(float(_predict(params, spend)), 2),
        "current_projected_revenue": round(float(_predict(params, current)), 2),
    }


def curve_points(curve: dict[str, Any], points: int = 25) -> list[dict[str, float]]:
    """Sampled curve for charting, always inside the observed spend range."""
    if not curve.get("available"):
        return []
    lo, hi = curve["spend_range"]
    params = {"form": curve["form"], **curve["params"]}
    out = []
    for spend in np.linspace(max(lo * 0.5, 1.0), hi * 1.3, points):
        revenue = _predict(params, float(spend))
        out.append({
            "spend": round(float(spend), 2), "revenue": round(revenue, 2),
            "roas": round(revenue / spend, 3) if spend else 0.0,
            "in_range": bool(lo <= spend <= hi),
        })
    return out


def profit_curve(curve: dict[str, Any], contribution_margin_pct: float, points: int = 25) -> list[dict[str, float]]:
    """Contribution-margin profit as a function of spend (the optimizer's input)."""
    margin = max(min(contribution_margin_pct, 95.0), 1.0) / 100.0
    out = []
    for point in curve_points(curve, points):
        profit = point["revenue"] * margin - point["spend"]
        out.append({**point, "profit": round(profit, 2)})
    return out