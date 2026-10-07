"""Root-cause decomposition primitives.

Two families are implemented:

* **Metric (factor) decomposition** — multiplicative identities such as
  ``ROAS = CTR × CVR × AOV / CPC`` and ``revenue = impressions × CTR × CVR × AOV``.
  Contributions are attributed on the *log* scale, which makes
  ``Σ contributions = total log change`` exactly (a Shapley-consistent,
  order-independent split for multiplicative models).
* **Mix / rate decomposition (Oaxaca-style)** — splits an aggregate change into
  the part caused by shifting *mix* between segments and the part caused by
  within-segment *rate* changes.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Metric decompositions
# --------------------------------------------------------------------------- #
FORMULAS: dict[str, dict[str, Any]] = {
    "roas": {
        "formula": "ROAS = CTR × CVR × AOV ÷ CPC",
        "factors": ["ctr", "cvr", "aov", "cpc"],
        "signs": {"ctr": 1, "cvr": 1, "aov": 1, "cpc": -1},
        "unit": "x",
    },
    "revenue": {
        "formula": "Revenue = impressions × CTR × CVR × AOV",
        "factors": ["impressions", "ctr", "cvr", "aov"],
        "signs": {"impressions": 1, "ctr": 1, "cvr": 1, "aov": 1},
        "unit": "usd",
    },
    "cpa": {
        # CPA = CPC ÷ CVR
        "formula": "CPA = CPC ÷ CVR",
        "factors": ["cpc", "cvr"],
        "signs": {"cpc": 1, "cvr": -1},
        "unit": "usd",
    },
    "cpm": {
        "formula": "CPM = spend ÷ impressions × 1000",
        "factors": ["spend", "impressions"],
        "signs": {"spend": 1, "impressions": -1},
        "unit": "usd",
    },
    "contribution_margin": {
        "formula": "Contribution margin = revenue × CM% ÷ spend",
        "factors": ["revenue", "cm_pct", "spend"],
        "signs": {"revenue": 1, "cm_pct": 1, "spend": -1},
        "unit": "usd",
    },
}


def metric_decomposition(
    metric: str,
    before: dict[str, float],
    after: dict[str, float],
) -> dict[str, Any]:
    """Log-scale factor attribution of a multiplicative metric change.

    ``before`` / ``after`` are dictionaries of factor values for the baseline and
    comparison windows (e.g. ``{"ctr": 0.014, "cvr": 0.028, ...}``).
    """
    spec = FORMULAS.get(metric)
    if not spec:
        return {"available": False, "reason": f"no decomposition defined for '{metric}'"}

    eps = 1e-12
    contributions: list[dict[str, Any]] = []
    total_log_change = 0.0
    for factor in spec["factors"]:
        b = float(before.get(factor, 0.0))
        a = float(after.get(factor, 0.0))
        if b <= 0 or a <= 0:
            continue
        log_change = math.log(a / b)
        signed = log_change * spec["signs"][factor]
        total_log_change += signed
        contributions.append({
            "factor": factor,
            "before": round(b, 6),
            "after": round(a, 6),
            "change_pct": round((a / b - 1) * 100, 2),
            "log_contribution": round(signed, 6),
        })

    model_change_pct = (math.exp(total_log_change) - 1) * 100 if total_log_change else 0.0
    for c in contributions:
        c["contribution_pct"] = (
            round(c["log_contribution"] / total_log_change * 100, 2) if abs(total_log_change) > eps else 0.0
        )
    contributions.sort(key=lambda c: abs(c["contribution_pct"]), reverse=True)
    return {
        "available": True,
        "metric": metric,
        "formula": spec["formula"],
        "unit": spec["unit"],
        "factors": contributions,
        "modelled_change_pct": round(model_change_pct, 2),
        "log_change_total": round(total_log_change, 6),
    }


def derive_factors(agg: dict[str, float]) -> dict[str, float]:
    """Compute the multiplicative factors from raw aggregates."""
    spend = float(agg.get("spend", 0.0))
    impressions = float(agg.get("impressions", 0.0))
    clicks = float(agg.get("clicks", 0.0))
    revenue = float(agg.get("attributed_revenue", 0.0))
    orders = float(agg.get("attributed_orders", 0.0))
    cm = float(agg.get("contribution_margin", 0.0))
    return {
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "revenue": revenue,
        "ctr": clicks / impressions if impressions else 0.0,
        "cvr": orders / clicks if clicks else 0.0,
        "aov": revenue / orders if orders else 0.0,
        "cpc": spend / clicks if clicks else 0.0,
        "cpm": spend / impressions * 1000 if impressions else 0.0,
        "roas": revenue / spend if spend else 0.0,
        "cm_pct": cm / revenue * 100 if revenue else 0.0,
    }


# --------------------------------------------------------------------------- #
# Mix / rate (Oaxaca-style) decomposition
# --------------------------------------------------------------------------- #
def mix_rate_decomposition(
    frame: pd.DataFrame,
    segment_col: str,
    value_col: str,
    weight_col: str,
    before_mask: pd.Series,
    after_mask: pd.Series,
) -> dict[str, Any]:
    """Decompose an aggregate rate change into mix vs rate effects.

    ``aggregate = Σ w_i × r_i`` where ``r_i = value/weight`` inside each segment
    and ``w_i`` is the segment's share of total weight. The classic Oaxaca split
    gives::

        Δaggregate = Σ (w_i^b − w_i^a)/2 × (r_i^a − r_i^b)   [mix]
                   + Σ (r_i^a − r_i^b)/2 × (w_i^b + w_i^a)   [rate, exact]

    The symmetric formulation is used so the two effects sum exactly to the
    observed change.
    """
    before = frame[before_mask].groupby(segment_col).agg(v=(value_col, "sum"), w=(weight_col, "sum"))
    after = frame[after_mask].groupby(segment_col).agg(v=(value_col, "sum"), w=(weight_col, "sum"))
    idx = before.index.union(after.index)
    before = before.reindex(idx).fillna(0.0)
    after = after.reindex(idx).fillna(0.0)

    wb_total, wa_total = before["w"].sum(), after["w"].sum()
    if wb_total <= 0 or wa_total <= 0:
        return {"available": False, "reason": "insufficient weight in one of the windows"}

    wb = before["w"] / wb_total
    wa = after["w"] / wa_total
    rb = np.where(before["w"] > 0, before["v"] / before["w"].replace(0, np.nan), 0.0)
    ra = np.where(after["w"] > 0, after["v"] / after["w"].replace(0, np.nan), 0.0)
    rb = np.nan_to_num(rb)
    ra = np.nan_to_num(ra)

    agg_before = float(before["v"].sum() / wb_total)
    agg_after = float(after["v"].sum() / wa_total)
    delta = agg_after - agg_before

    mix = (wb - wa) * (ra - rb)
    rate = (ra - rb) * (wb + wa)
    rows = []
    for i, seg in enumerate(idx):
        rows.append({
            "segment": str(seg),
            "share_before": round(float(wb.iloc[i]) * 100, 2),
            "share_after": round(float(wa.iloc[i]) * 100, 2),
            "rate_before": round(float(rb[i]), 4),
            "rate_after": round(float(ra[i]), 4),
            "mix_effect": round(float(mix[i]), 6),
            "rate_effect": round(float(rate[i]), 6),
        })
    rows.sort(key=lambda r: abs(r["mix_effect"]) + abs(r["rate_effect"]), reverse=True)
    return {
        "available": True,
        "segment": segment_col,
        "value": value_col,
        "aggregate_before": round(agg_before, 4),
        "aggregate_after": round(agg_after, 4),
        "change": round(delta, 6),
        "change_pct": round((agg_after / agg_before - 1) * 100, 2) if agg_before else 0.0,
        "total_mix_effect": round(float(mix.sum()), 6),
        "total_rate_effect": round(float(rate.sum()), 6),
        "segments": rows[:15],
    }


def waterfall_series(
    frame: pd.DataFrame,
    date_col: str,
    value_col: str,
    before_mask: pd.Series,
    after_mask: pd.Series,
) -> list[dict[str, Any]]:
    """Weekly-aggregated series used to render before/after waterfall charts."""
    grouped = (
        frame.groupby(pd.Grouper(key=date_col, freq="W"))[value_col]
        .sum()
        .reset_index()
        .rename(columns={date_col: "week", value_col: "value"})
    )
    return [
        {"week": str(r["week"].date()), "value": round(float(r["value"]), 2)}
        for _, r in grouped.iterrows()
    ]


def top_contributors(
    frame: pd.DataFrame,
    segment_col: str,
    metric_col: str,
    before_mask: pd.Series,
    after_mask: pd.Series,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Segments contributing most to an absolute change in a metric total."""
    before = frame[before_mask].groupby(segment_col)[metric_col].sum()
    after = frame[after_mask].groupby(segment_col)[metric_col].sum()
    idx = before.index.union(after.index)
    before = before.reindex(idx).fillna(0.0)
    after = after.reindex(idx).fillna(0.0)
    total_delta = float(after.sum() - before.sum())
    rows = []
    for seg in idx:
        b, a = float(before[seg]), float(after[seg])
        delta = a - b
        rows.append({
            "segment": str(seg),
            "before": round(b, 2),
            "after": round(a, 2),
            "delta": round(delta, 2),
            "delta_pct": round((a / b - 1) * 100, 2) if b else 0.0,
            "share_of_change_pct": round(delta / total_delta * 100, 2) if abs(total_delta) > 1e-9 else 0.0,
        })
    rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return rows[:limit]


def safe_pct_change(before: float, after: float) -> float:
    if not before:
        return 0.0
    return round((after / before - 1) * 100, 2)


def window_masks(frame: pd.DataFrame, date_col: str, start, end) -> tuple[pd.Series, ...]:
    dates = pd.to_datetime(frame[date_col]).dt.date
    return dates < start, (dates >= start) & (dates <= end)


def summarise(values: Iterable[float]) -> dict[str, float]:
    arr = np.asarray(list(values), dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return {"mean": 0.0, "median": 0.0, "std": 0.0, "n": 0}
    return {
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "std": float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
        "n": int(arr.size),
    }