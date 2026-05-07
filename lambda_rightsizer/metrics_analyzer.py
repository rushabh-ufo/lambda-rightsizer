"""
metrics_analyzer.py
-------------------
Retrieves and analyses actual Lambda memory usage from CloudWatch.

Strategy (in order):
  1. CloudWatch Logs Insights  — aggregates REPORT lines server-side; fast and cheap.
  2. CloudWatch Logs filter    — fetches raw REPORT log events and parses them locally.
     Used when Logs Insights returns fewer than MIN_INVOCATIONS samples (e.g. new
     functions, short retention windows, or Insights quota exhaustion).
  3. CloudWatch Metrics        — last resort; uses the aws/lambda MaxMemoryUsed metric.
     Less granular (daily aggregates) but always available when logs are absent.

Each strategy is tried in sequence; the first one that meets the MIN_INVOCATIONS
threshold wins.  If none do, the result is returned with data_source="insufficient_data"
and all numeric fields set to None.

Public API:
  analyze_function(session, function_name, start_time?, end_time?)  -> MetricsResult
  analyze_functions(session, function_names, ...)                   -> list[MetricsResult]
  to_json(result)                                                   -> str
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from lambda_rightsizer.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# REPORT line format (Lambda runtime emits this for every invocation):
# REPORT RequestId: <id>  Duration: X ms  Billed Duration: X ms
#         Memory Size: X MB  Max Memory Used: X MB  Init Duration: X ms
_REPORT_PATTERN = re.compile(
    r"Max Memory Used:\s*(?P<max_used>\d+)\s*MB",
    re.IGNORECASE,
)

_THROTTLE_CODES: frozenset[str] = frozenset(
    {"TooManyRequestsException", "Throttling", "ThrottlingException", "LimitExceededException"}
)
_PERMISSION_CODES: frozenset[str] = frozenset(
    {"AccessDeniedException", "UnauthorizedException", "AuthorizationError"}
)
_MISSING_LOG_GROUP_CODES: frozenset[str] = frozenset(
    {"ResourceNotFoundException"}
)

_MAX_RETRIES: int = 4
_BASE_BACKOFF: float = 1.0
_QUERY_POLL_INTERVAL: float = 1.0
_QUERY_TIMEOUT: int = 15  # seconds — sufficient for any window up to 90 days

# CloudWatch Logs Insights hard limit per query
_INSIGHTS_MAX_LIMIT: int = 10_000

# How many raw log events to fetch per filter_log_events page
_FILTER_PAGE_SIZE: int = 100


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class MemorySample:
    """A single observed Max Memory Used value from one invocation."""
    memory_mb: int
    timestamp: str  # ISO-8601 UTC


@dataclass
class MetricsResult:
    """
    Complete memory analysis for one Lambda function over the analysis window.

    Numeric fields are None when data is unavailable.
    """
    function_name: str
    analysis_start: str                     # ISO-8601 UTC
    analysis_end: str                       # ISO-8601 UTC
    lookback_days: int

    # Aggregates
    max_memory_used_mb: Optional[int]
    min_memory_used_mb: Optional[int]
    avg_memory_used_mb: Optional[float]
    p95_memory_used_mb: Optional[int]       # 95th-percentile
    utilization_percent: Optional[float]    # avg_used / allocated * 100
    allocated_memory_mb: Optional[int]      # from function config (passed in)

    invocation_count: int
    data_source: str                        # "logs_insights" | "logs_filter" | "cw_metrics" | "insufficient_data"
    samples: list[MemorySample] = field(default_factory=list)  # raw samples (logs strategies only)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_function(
    session: boto3.Session,
    function_name: str,
    allocated_memory_mb: Optional[int] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> MetricsResult:
    """
    Analyse memory usage for a single Lambda function.

    Args:
        session:              Authenticated boto3 Session.
        function_name:        Lambda function name (not ARN).
        allocated_memory_mb:  Configured memory from discovery; used to compute
                              utilization_percent.  Pass None if unavailable.
        start_time:           Analysis window start (UTC).  Defaults to
                              Config.LOOKBACK_DAYS ago.
        end_time:             Analysis window end (UTC).  Defaults to now.

    Returns:
        MetricsResult with all computed statistics.
    """
    end_time = end_time or datetime.now(timezone.utc)
    start_time = start_time or (end_time - timedelta(days=Config.LOOKBACK_DAYS))

    logger.info(
        "Analysing %s | window=%s → %s",
        function_name,
        start_time.strftime("%Y-%m-%d"),
        end_time.strftime("%Y-%m-%d"),
    )

    _t0 = time.monotonic()

    result = _try_logs_insights(session, function_name, allocated_memory_mb, start_time, end_time)

    if result.invocation_count < Config.MIN_INVOCATIONS:
        logger.debug(
            "%s: Logs Insights returned %d sample(s) (need %d). Trying log filter ...",
            function_name, result.invocation_count, Config.MIN_INVOCATIONS,
        )
        result = _try_logs_filter(session, function_name, allocated_memory_mb, start_time, end_time)

    if result.invocation_count < Config.MIN_INVOCATIONS:
        logger.debug(
            "%s: Log filter returned %d sample(s). Falling back to CW Metrics ...",
            function_name, result.invocation_count,
        )
        result = _try_cw_metrics(session, function_name, allocated_memory_mb, start_time, end_time)

    if result.invocation_count < Config.MIN_INVOCATIONS:
        msg = (
            f"Only {result.invocation_count} invocation(s) found "
            f"(minimum required: {Config.MIN_INVOCATIONS}). "
            "Results may not be statistically significant."
        )
        logger.warning("%s: %s", function_name, msg)
        result.warnings.append(msg)

    logger.info(
        "%s: done in %.1fs | source=%s | invocations=%d | max=%s MB | avg=%s MB | utilization=%s%%",
        function_name,
        time.monotonic() - _t0,
        result.data_source,
        result.invocation_count,
        result.max_memory_used_mb,
        f"{result.avg_memory_used_mb:.1f}" if result.avg_memory_used_mb is not None else "N/A",
        f"{result.utilization_percent:.1f}" if result.utilization_percent is not None else "N/A",
    )
    return result


def analyze_functions(
    session: boto3.Session,
    functions: list[dict],
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> list[MetricsResult]:
    """
    Convenience wrapper to analyse a list of function dicts as returned by
    lambda_discovery (keys: function_name, memory_mb).

    Args:
        session:    Authenticated boto3 Session.
        functions:  List of dicts with at least 'function_name' and 'memory_mb'.
        start_time: Optional analysis window start.
        end_time:   Optional analysis window end.

    Returns:
        List of MetricsResult, one per function, in the same order.
    """
    results = []
    for fn in functions:
        result = analyze_function(
            session=session,
            function_name=fn["function_name"],
            allocated_memory_mb=fn.get("memory_mb"),
            start_time=start_time,
            end_time=end_time,
        )
        results.append(result)
    return results


def to_json(result: MetricsResult, indent: int = 2) -> str:
    """Serialise a MetricsResult to a JSON string."""
    return json.dumps(asdict(result), indent=indent, default=str)


# ---------------------------------------------------------------------------
# Strategy 1: CloudWatch Logs Insights
# ---------------------------------------------------------------------------

# Insights query — pulls max_used per invocation so we can compute all stats
_INSIGHTS_QUERY = """\
filter @type = "REPORT"
| parse @message "Max Memory Used: * MB" as max_used_mb
| stats
    max(max_used_mb)   as peak_mb,
    min(max_used_mb)   as min_mb,
    avg(max_used_mb)   as avg_mb,
    count(*)           as invocations
  by bin(1d)
"""

# Second query to get individual samples for percentile calculation
_INSIGHTS_SAMPLES_QUERY = """\
filter @type = "REPORT"
| parse @message "Max Memory Used: * MB" as max_used_mb
| fields @timestamp, max_used_mb
| sort @timestamp desc
| limit {limit}
"""


def _try_logs_insights(
    session: boto3.Session,
    function_name: str,
    allocated_mb: Optional[int],
    start_time: datetime,
    end_time: datetime,
) -> MetricsResult:
    client = session.client("logs", region_name=Config.AWS_REGION)
    log_group = f"/aws/lambda/{function_name}"
    base = _base_result(function_name, allocated_mb, start_time, end_time)

    # --- Fetch individual samples (for min/p95 and sample list) ---
    sample_limit = max(Config.MIN_INVOCATIONS * 10, 1000)
    sample_limit = min(sample_limit, _INSIGHTS_MAX_LIMIT)
    samples_query = _INSIGHTS_SAMPLES_QUERY.format(limit=sample_limit)

    try:
        query_id = _start_insights_query(client, log_group, samples_query, start_time, end_time)
        if query_id is None:
            return base

        raw_rows = _poll_insights_query(client, query_id, function_name)
        if not raw_rows:
            return base

        samples = _parse_insights_samples(raw_rows)
        if not samples:
            return base

        result = _compute_stats(base, samples, "logs_insights")
        logger.debug("%s: Logs Insights returned %d sample(s).", function_name, len(samples))
        return result

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in _MISSING_LOG_GROUP_CODES:
            logger.debug("%s: Log group %s does not exist.", function_name, log_group)
        elif code in _PERMISSION_CODES:
            logger.warning("%s: No permission to query Logs Insights (%s).", function_name, code)
            base.warnings.append(f"Logs Insights access denied: {code}")
        else:
            logger.warning("%s: Logs Insights error (%s): %s", function_name, code, exc)
        return base


def _start_insights_query(
    client,
    log_group: str,
    query: str,
    start_time: datetime,
    end_time: datetime,
) -> Optional[str]:
    """Start a Logs Insights query with retry on throttle. Returns queryId or None."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.start_query(
                logGroupName=log_group,
                startTime=int(start_time.timestamp()),
                endTime=int(end_time.timestamp()),
                queryString=query,
            )
            return response["queryId"]
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in _THROTTLE_CODES and attempt < _MAX_RETRIES:
                wait = _jitter_backoff(attempt)
                logger.warning("Throttled starting Insights query. Retrying in %.1fs ...", wait)
                time.sleep(wait)
            else:
                raise
    return None


def _poll_insights_query(client, query_id: str, function_name: str) -> list[list[dict]]:
    """Poll until the query completes or times out. Returns raw results rows."""
    deadline = time.monotonic() + _QUERY_TIMEOUT
    elapsed = 0.0
    while time.monotonic() < deadline:
        try:
            response = client.get_query_results(queryId=query_id)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in _THROTTLE_CODES:
                time.sleep(_jitter_backoff(0))
                continue
            raise

        status = response["status"]
        logger.debug("%s: Insights query status=%s (%.0fs elapsed)", function_name, status, elapsed)

        if status == "Complete":
            return response.get("results", [])
        if status in ("Failed", "Cancelled", "Timeout"):
            logger.info("%s: Insights query ended with status=%s — falling back.", function_name, status)
            return []

        time.sleep(_QUERY_POLL_INTERVAL)
        elapsed += _QUERY_POLL_INTERVAL

    logger.info(
        "%s: Insights query timed out after %ds — falling back to log filter.",
        function_name, _QUERY_TIMEOUT,
    )
    return []


def _parse_insights_samples(rows: list[list[dict]]) -> list[MemorySample]:
    """Convert raw Insights result rows into MemorySample objects."""
    samples: list[MemorySample] = []
    for row in rows:
        fields = {item["field"]: item["value"] for item in row}
        raw_mb = fields.get("max_used_mb")
        raw_ts = fields.get("@timestamp", "")
        if raw_mb is None:
            continue
        try:
            samples.append(MemorySample(memory_mb=int(float(raw_mb)), timestamp=raw_ts))
        except (ValueError, TypeError):
            continue
    return samples


# ---------------------------------------------------------------------------
# Strategy 2: CloudWatch Logs filter (raw REPORT line parsing)
# ---------------------------------------------------------------------------

def _try_logs_filter(
    session: boto3.Session,
    function_name: str,
    allocated_mb: Optional[int],
    start_time: datetime,
    end_time: datetime,
) -> MetricsResult:
    client = session.client("logs", region_name=Config.AWS_REGION)
    log_group = f"/aws/lambda/{function_name}"
    base = _base_result(function_name, allocated_mb, start_time, end_time)

    target = max(Config.MIN_INVOCATIONS * 10, 200)
    samples: list[MemorySample] = []

    try:
        kwargs: dict = {
            "logGroupName": log_group,
            "startTime": int(start_time.timestamp() * 1000),
            "endTime": int(end_time.timestamp() * 1000),
            "filterPattern": '"Max Memory Used"',
            "limit": _FILTER_PAGE_SIZE,
        }

        pages_fetched = 0
        while len(samples) < target:
            response = _filter_with_retry(client, kwargs, function_name)
            if response is None:
                break

            events = response.get("events", [])
            for event in events:
                sample = _parse_report_line(event.get("message", ""), event.get("timestamp"))
                if sample:
                    samples.append(sample)

            pages_fetched += 1
            next_token = response.get("nextToken")
            if not next_token or not events:
                break
            kwargs["nextToken"] = next_token

        logger.debug(
            "%s: Log filter fetched %d sample(s) across %d page(s).",
            function_name, len(samples), pages_fetched,
        )

        if not samples:
            return base

        return _compute_stats(base, samples, "logs_filter")

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in _MISSING_LOG_GROUP_CODES:
            logger.debug("%s: Log group %s does not exist.", function_name, log_group)
        elif code in _PERMISSION_CODES:
            logger.warning("%s: No permission to filter logs (%s).", function_name, code)
            base.warnings.append(f"Log filter access denied: {code}")
        else:
            logger.warning("%s: Log filter error (%s): %s", function_name, code, exc)
        return base


def _filter_with_retry(client, kwargs: dict, function_name: str) -> Optional[dict]:
    """Call filter_log_events with exponential backoff on throttle."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return client.filter_log_events(**kwargs)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in _THROTTLE_CODES and attempt < _MAX_RETRIES:
                wait = _jitter_backoff(attempt)
                logger.warning(
                    "%s: Throttled on filter_log_events (attempt %d). Retrying in %.1fs ...",
                    function_name, attempt + 1, wait,
                )
                time.sleep(wait)
            else:
                raise
    return None


def _parse_report_line(message: str, timestamp_ms: Optional[int]) -> Optional[MemorySample]:
    """
    Extract Max Memory Used from a Lambda REPORT log line.

    Example line:
      REPORT RequestId: abc  Duration: 123 ms  Billed Duration: 200 ms
      Memory Size: 512 MB  Max Memory Used: 87 MB  Init Duration: 456 ms
    """
    match = _REPORT_PATTERN.search(message)
    if not match:
        return None
    try:
        mb = int(match.group("max_used"))
        ts = (
            datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if timestamp_ms
            else ""
        )
        return MemorySample(memory_mb=mb, timestamp=ts)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Strategy 3: CloudWatch Metrics (last resort)
# ---------------------------------------------------------------------------

def _try_cw_metrics(
    session: boto3.Session,
    function_name: str,
    allocated_mb: Optional[int],
    start_time: datetime,
    end_time: datetime,
) -> MetricsResult:
    client = session.client("cloudwatch", region_name=Config.AWS_REGION)
    base = _base_result(function_name, allocated_mb, start_time, end_time)

    # Use hourly granularity to get more data points within the window
    period_seconds = 3_600
    total_seconds = int((end_time - start_time).total_seconds())
    # CloudWatch max 1440 datapoints per request — widen period if needed
    if total_seconds // period_seconds > 1440:
        period_seconds = math.ceil(total_seconds / 1440 / 3600) * 3600

    try:
        response = _cw_get_metric_with_retry(
            client,
            function_name=function_name,
            start_time=start_time,
            end_time=end_time,
            period=period_seconds,
        )
        datapoints = response.get("Datapoints", [])
        if not datapoints:
            logger.debug("%s: No CloudWatch metric datapoints found.", function_name)
            return base

        # Build synthetic samples from daily Maximum datapoints
        samples: list[MemorySample] = []
        for dp in sorted(datapoints, key=lambda d: d["Timestamp"]):
            ts = dp["Timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(dp["Timestamp"], datetime) else str(dp["Timestamp"])
            samples.append(MemorySample(memory_mb=int(dp["Maximum"]), timestamp=ts))

        logger.debug("%s: CW Metrics returned %d datapoint(s).", function_name, len(samples))
        result = _compute_stats(base, samples, "cw_metrics")
        result.warnings.append(
            "Statistics derived from CloudWatch daily Maximum metric — "
            "individual invocation data unavailable."
        )
        return result

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in _PERMISSION_CODES:
            logger.warning("%s: No permission to read CW Metrics (%s).", function_name, code)
            base.warnings.append(f"CloudWatch Metrics access denied: {code}")
        else:
            logger.warning("%s: CW Metrics error (%s): %s", function_name, code, exc)
        return base


def _cw_get_metric_with_retry(client, *, function_name: str, start_time: datetime, end_time: datetime, period: int) -> dict:
    """Call get_metric_statistics with retry on throttle."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return client.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="MaxMemoryUsed",
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=start_time,
                EndTime=end_time,
                Period=period,
                Statistics=["Maximum", "Average", "SampleCount"],
                Unit="Megabytes",
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in _THROTTLE_CODES and attempt < _MAX_RETRIES:
                wait = _jitter_backoff(attempt)
                logger.warning("Throttled on get_metric_statistics. Retrying in %.1fs ...", wait)
                time.sleep(wait)
            else:
                raise
    return {}


# ---------------------------------------------------------------------------
# Statistics computation
# ---------------------------------------------------------------------------

def _compute_stats(
    base: MetricsResult,
    samples: list[MemorySample],
    source: str,
) -> MetricsResult:
    """Populate all aggregate fields from a list of MemorySamples."""
    if not samples:
        base.data_source = source
        return base

    values = [s.memory_mb for s in samples]
    n = len(values)

    max_mb = max(values)
    min_mb = min(values)
    avg_mb = sum(values) / n
    p95_mb = _percentile(values, 95)

    utilization: Optional[float] = None
    if base.allocated_memory_mb and base.allocated_memory_mb > 0:
        utilization = round(avg_mb / base.allocated_memory_mb * 100, 2)

    base.max_memory_used_mb = max_mb
    base.min_memory_used_mb = min_mb
    base.avg_memory_used_mb = round(avg_mb, 2)
    base.p95_memory_used_mb = p95_mb
    base.utilization_percent = utilization
    base.invocation_count = n
    base.data_source = source
    base.samples = samples

    return base


def _percentile(values: list[int], pct: int) -> int:
    """
    Nearest-rank percentile (no interpolation).
    Handles single-element lists correctly.
    """
    if not values:
        return 0
    sorted_vals = sorted(values)
    rank = math.ceil(pct / 100 * len(sorted_vals))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_result(
    function_name: str,
    allocated_mb: Optional[int],
    start_time: datetime,
    end_time: datetime,
) -> MetricsResult:
    """Return an empty MetricsResult skeleton."""
    return MetricsResult(
        function_name=function_name,
        analysis_start=start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        analysis_end=end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        lookback_days=Config.LOOKBACK_DAYS,
        max_memory_used_mb=None,
        min_memory_used_mb=None,
        avg_memory_used_mb=None,
        p95_memory_used_mb=None,
        utilization_percent=None,
        allocated_memory_mb=allocated_mb,
        invocation_count=0,
        data_source="insufficient_data",
        samples=[],
        warnings=[],
    )


def _jitter_backoff(attempt: int) -> float:
    """Full-jitter exponential backoff capped at 30 seconds."""
    import random
    return min(30.0, _BASE_BACKOFF * (2 ** attempt)) * random.random()
