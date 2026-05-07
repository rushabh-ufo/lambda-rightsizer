"""
optimizer.py
------------
Applies optimization logic to Lambda function metrics and produces a
structured recommendation record for each function.

Optimization rules (evaluated in order):
  1. No data / insufficient invocations  -> status: "insufficient_data"
  2. avg utilization < UTIL_REDUCE_THRESHOLD (default 30%)
       -> Reduce memory (status: "over_provisioned")
  3. avg utilization between UTIL_KEEP_LOWER and UTIL_KEEP_UPPER (30-70%)
       -> Keep current allocation (status: "optimal")
  4. avg utilization > UTIL_INCREASE_THRESHOLD (default 80%)
       -> Increase memory (status: "under_provisioned")
  5. avg utilization between 70-80%
       -> Borderline; flag as "watch", no change recommended yet

Hard constraint (always enforced):
  recommended_mb >= ceil(max_used_mb * SAFETY_BUFFER_FACTOR / step) * step
  Guarantees we never recommend below actual peak + 20% headroom.

Risk scoring (1-5):
  Factors that increase risk:
    - Low sample count (< 50 invocations)           +2
    - Data from cw_metrics fallback (less precise)  +1
    - Reduction > 50% of current allocation         +1
    - p95 within 10% of recommended ceiling         +1
  Clamped to 1-5.
    1 = very low risk   5 = high risk

Public API:
  analyze(function_meta, metrics)       -> dict  (OptimizationRecord as dict)
  analyze_batch(functions_with_metrics) -> list[dict]
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, asdict
from typing import Optional

from lambda_rightsizer.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AWS Lambda memory bounds
# ---------------------------------------------------------------------------

LAMBDA_MIN_MB: int = 128
LAMBDA_MAX_MB: int = 10_240

# ---------------------------------------------------------------------------
# Status labels
# ---------------------------------------------------------------------------

STATUS_INSUFFICIENT = "insufficient_data"
STATUS_REDUCE = "over_provisioned"
STATUS_OPTIMAL = "optimal"
STATUS_WATCH = "watch"
STATUS_INCREASE = "under_provisioned"

# Risk level labels keyed by score
_RISK_LABELS: dict[int, str] = {
    1: "very_low",
    2: "low",
    3: "medium",
    4: "high",
    5: "very_high",
}


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass
class OptimizationRecord:
    # Identity
    function_name: str
    function_arn: str
    runtime: str

    # Current configuration
    allocated_mb: int

    # Observed usage
    max_used_mb: Optional[int]
    min_used_mb: Optional[int]
    avg_used_mb: Optional[float]
    p95_used_mb: Optional[int]
    utilization_percent: Optional[float]
    invocation_count: int
    data_source: str

    # Recommendation
    recommended_mb: int
    change_mb: int                  # positive = increase, negative = decrease
    safety_floor_mb: int            # minimum safe value (peak * buffer, rounded)
    waste_percent: Optional[float]  # (allocated - max_used) / allocated * 100
    status: str

    # Risk
    risk_score: int                 # 1-5
    risk_level: str                 # very_low / low / medium / high / very_high

    # Human-readable recommendation
    recommendation: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(function_meta: dict, metrics: dict) -> dict:
    """
    Merge function metadata + metrics into a recommendation record.

    Args:
        function_meta: Dict from lambda_discovery (function_name, function_arn,
                       runtime, memory_mb, ...).
        metrics:       Dict from metrics_analyzer (max_memory_used_mb,
                       avg_memory_used_mb, min_memory_used_mb, p95_memory_used_mb,
                       utilization_percent, invocation_count, data_source, ...).

    Returns:
        OptimizationRecord serialised as a plain dict.
    """
    allocated_mb: int = function_meta["memory_mb"]
    fn_name: str = function_meta["function_name"]

    max_used_mb: Optional[int] = metrics.get("max_memory_used_mb")
    min_used_mb: Optional[int] = metrics.get("min_memory_used_mb")
    avg_used_mb: Optional[float] = metrics.get("avg_memory_used_mb")
    p95_used_mb: Optional[int] = metrics.get("p95_memory_used_mb")
    utilization_pct: Optional[float] = metrics.get("utilization_percent")
    invocations: int = metrics.get("invocation_count", 0)
    data_source: str = metrics.get("data_source", "insufficient_data")

    # --- Insufficient data path ---
    if max_used_mb is None or invocations < Config.MIN_INVOCATIONS:
        logger.debug("%s: skipped — insufficient data (%d invocations).", fn_name, invocations)
        record = OptimizationRecord(
            function_name=fn_name,
            function_arn=function_meta["function_arn"],
            runtime=function_meta["runtime"],
            allocated_mb=allocated_mb,
            max_used_mb=max_used_mb,
            min_used_mb=min_used_mb,
            avg_used_mb=avg_used_mb,
            p95_used_mb=p95_used_mb,
            utilization_percent=utilization_pct,
            invocation_count=invocations,
            data_source=data_source,
            recommended_mb=allocated_mb,
            change_mb=0,
            safety_floor_mb=allocated_mb,
            waste_percent=None,
            status=STATUS_INSUFFICIENT,
            risk_score=3,
            risk_level=_RISK_LABELS[3],
            recommendation=(
                f"Insufficient data — only {invocations} invocation(s) recorded "
                f"(minimum required: {Config.MIN_INVOCATIONS}). "
                "Re-run after more traffic has been observed."
            ),
        )
        return asdict(record)

    # --- Core calculations ---
    safety_floor_mb = _safety_floor(max_used_mb)
    waste_pct = _waste_percent(allocated_mb, max_used_mb)
    status = _determine_status(utilization_pct, avg_used_mb, allocated_mb)
    recommended_mb = _recommend_memory(status, allocated_mb, safety_floor_mb)
    change_mb = recommended_mb - allocated_mb
    risk_score = _score_risk(
        invocations=invocations,
        data_source=data_source,
        allocated_mb=allocated_mb,
        recommended_mb=recommended_mb,
        p95_used_mb=p95_used_mb,
    )

    recommendation = _build_recommendation(
        fn_name=fn_name,
        status=status,
        allocated_mb=allocated_mb,
        recommended_mb=recommended_mb,
        max_used_mb=max_used_mb,
        avg_used_mb=avg_used_mb,
        utilization_pct=utilization_pct,
        waste_pct=waste_pct,
        risk_score=risk_score,
    )

    logger.info(
        "%s: status=%s | allocated=%dMB | max_used=%dMB | recommended=%dMB | risk=%s",
        fn_name, status, allocated_mb, max_used_mb, recommended_mb, _RISK_LABELS[risk_score],
    )

    record = OptimizationRecord(
        function_name=fn_name,
        function_arn=function_meta["function_arn"],
        runtime=function_meta["runtime"],
        allocated_mb=allocated_mb,
        max_used_mb=max_used_mb,
        min_used_mb=min_used_mb,
        avg_used_mb=avg_used_mb,
        p95_used_mb=p95_used_mb,
        utilization_percent=utilization_pct,
        invocation_count=invocations,
        data_source=data_source,
        recommended_mb=recommended_mb,
        change_mb=change_mb,
        safety_floor_mb=safety_floor_mb,
        waste_percent=round(waste_pct, 1),
        status=status,
        risk_score=risk_score,
        risk_level=_RISK_LABELS[risk_score],
        recommendation=recommendation,
    )
    return asdict(record)


def analyze_batch(functions_with_metrics: list[tuple[dict, dict]]) -> list[dict]:
    """
    Convenience wrapper for processing multiple functions.

    Args:
        functions_with_metrics: List of (function_meta, metrics) tuples.

    Returns:
        List of OptimizationRecord dicts in the same order.
    """
    return [analyze(fn_meta, metrics) for fn_meta, metrics in functions_with_metrics]


# ---------------------------------------------------------------------------
# Optimization logic
# ---------------------------------------------------------------------------

def _determine_status(
    utilization_pct: Optional[float],
    avg_used_mb: Optional[float],
    allocated_mb: int,
) -> str:
    """Map utilization percentage to a status label."""
    if utilization_pct is None:
        # Fall back to a rough estimate if utilization wasn't computed
        if avg_used_mb is None:
            return STATUS_INSUFFICIENT
        utilization_pct = avg_used_mb / allocated_mb * 100

    if utilization_pct < Config.UTIL_REDUCE_THRESHOLD:
        return STATUS_REDUCE
    if utilization_pct > Config.UTIL_INCREASE_THRESHOLD:
        return STATUS_INCREASE
    if utilization_pct > Config.UTIL_KEEP_UPPER:
        return STATUS_WATCH
    return STATUS_OPTIMAL


def _safety_floor(max_used_mb: int) -> int:
    """Minimum safe memory: peak + 20% buffer, rounded up to step."""
    buffered = max_used_mb * Config.SAFETY_BUFFER_FACTOR
    step = Config.MEMORY_STEP_MB
    floored = math.ceil(buffered / step) * step
    return int(max(LAMBDA_MIN_MB, min(LAMBDA_MAX_MB, floored)))


def _recommend_memory(status: str, allocated_mb: int, safety_floor_mb: int) -> int:
    """
    Derive the recommended memory value.
    - REDUCE:   safety_floor_mb (always respects the hard constraint)
    - INCREASE: next step above safety_floor_mb
    - WATCH:    no change
    - OPTIMAL:  no change
    """
    if status == STATUS_REDUCE:
        return safety_floor_mb
    if status == STATUS_INCREASE:
        step = Config.MEMORY_STEP_MB
        above = math.ceil(safety_floor_mb / step) * step
        return int(min(LAMBDA_MAX_MB, above))
    return allocated_mb  # optimal / watch / insufficient


def _waste_percent(allocated_mb: int, max_used_mb: int) -> float:
    if allocated_mb == 0:
        return 0.0
    return (allocated_mb - max_used_mb) / allocated_mb * 100


# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------

def _score_risk(
    invocations: int,
    data_source: str,
    allocated_mb: int,
    recommended_mb: int,
    p95_used_mb: Optional[int],
) -> int:
    score = 1

    if invocations < 50:
        score += 2
    elif invocations < Config.MIN_INVOCATIONS * 5:
        score += 1

    if data_source == "cw_metrics":
        score += 1

    if allocated_mb > 0:
        reduction_pct = (allocated_mb - recommended_mb) / allocated_mb * 100
        if reduction_pct > 50:
            score += 1

    if p95_used_mb is not None and recommended_mb > 0:
        headroom_pct = (recommended_mb - p95_used_mb) / recommended_mb * 100
        if headroom_pct < 10:
            score += 1

    return max(1, min(5, score))


# ---------------------------------------------------------------------------
# Human-readable recommendation text
# ---------------------------------------------------------------------------

def _build_recommendation(
    fn_name: str,
    status: str,
    allocated_mb: int,
    recommended_mb: int,
    max_used_mb: int,
    avg_used_mb: Optional[float],
    utilization_pct: Optional[float],
    waste_pct: float,
    risk_score: int,
) -> str:
    util_str = f"{utilization_pct:.1f}%" if utilization_pct is not None else "N/A"
    avg_str = f"{avg_used_mb:.0f} MB" if avg_used_mb is not None else "N/A"
    savings_mb = allocated_mb - recommended_mb

    if status == STATUS_REDUCE:
        return (
            f"Reduce memory from {allocated_mb} MB to {recommended_mb} MB. "
            f"Peak usage was {max_used_mb} MB (avg {avg_str}), "
            f"utilization {util_str} — {waste_pct:.1f}% waste. "
            f"Estimated saving: {savings_mb} MB per invocation. "
            f"Risk: {_RISK_LABELS[risk_score]}."
        )

    if status == STATUS_INCREASE:
        return (
            f"Increase memory from {allocated_mb} MB to {recommended_mb} MB. "
            f"Peak usage {max_used_mb} MB exceeds safe headroom. "
            f"Utilization: {util_str}. Risk: {_RISK_LABELS[risk_score]}."
        )

    if status == STATUS_WATCH:
        return (
            f"No change recommended yet. Utilization {util_str} is approaching the "
            f"upper threshold ({Config.UTIL_KEEP_UPPER}%). Monitor over the next cycle."
        )

    if status == STATUS_OPTIMAL:
        return (
            f"Memory allocation is well-sized. Utilization {util_str}, "
            f"peak {max_used_mb} MB against {allocated_mb} MB allocated."
        )

    return "Insufficient data to make a recommendation."
