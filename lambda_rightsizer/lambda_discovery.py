"""
lambda_discovery.py
-------------------
Discovers all AWS Lambda functions in the configured account and region.

Responsibilities:
  - Paginate through Lambda list_functions API
  - Capture: name, ARN, runtime, memory, timeout, last_modified, description, layers
  - Exponential backoff on throttling (TooManyRequestsException / Throttling)
  - Graceful handling of permission errors (AccessDeniedException / UnauthorizedException)
  - Return structured, JSON-serialisable output

Public API:
  discover_functions(session)  -> DiscoveryResult
  to_json(result)              -> str
"""

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Iterator, Optional

import boto3
from botocore.exceptions import ClientError

from lambda_rightsizer.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_RETRIES: int = 5
_BASE_BACKOFF_SECONDS: float = 1.0
_THROTTLE_ERROR_CODES: frozenset[str] = frozenset(
    {"TooManyRequestsException", "Throttling", "ThrottlingException"}
)
_PERMISSION_ERROR_CODES: frozenset[str] = frozenset(
    {"AccessDeniedException", "UnauthorizedException", "AuthorizationError"}
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FunctionRecord:
    """Normalised representation of a single Lambda function."""

    function_name: str
    function_arn: str
    runtime: str
    memory_mb: int
    timeout_seconds: int
    last_modified: str
    description: str
    layers: list[str]
    package_type: str
    architectures: list[str]


@dataclass
class DiscoveryResult:
    """Top-level result returned by discover_functions()."""

    region: str
    account_id: str
    scanned_at: str
    total_discovered: int
    functions: list[FunctionRecord] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_functions(session: boto3.Session) -> DiscoveryResult:
    """
    Enumerate all Lambda functions visible to the supplied boto3 session.

    Args:
        session: An authenticated boto3.Session.

    Returns:
        DiscoveryResult containing all discovered FunctionRecords plus any
        non-fatal errors encountered during the scan.

    Raises:
        PermissionError: If the caller lacks lambda:ListFunctions entirely.
    """
    region = Config.AWS_REGION
    account_id = _resolve_account_id(session)
    scanned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(
        "Starting Lambda discovery | account=%s | region=%s",
        account_id,
        region,
    )

    client = _build_lambda_client(session)
    functions: list[FunctionRecord] = []
    errors: list[dict] = []

    for raw_fn in _iter_functions(client, errors):
        record = _normalise(raw_fn)
        functions.append(record)
        logger.debug("Discovered function: %s (%s)", record.function_name, record.runtime)

    result = DiscoveryResult(
        region=region,
        account_id=account_id,
        scanned_at=scanned_at,
        total_discovered=len(functions),
        functions=functions,
        errors=errors,
    )

    logger.info(
        "Discovery complete | total=%d | errors=%d",
        result.total_discovered,
        len(result.errors),
    )
    return result


def to_json(result: DiscoveryResult, indent: int = 2) -> str:
    """Serialise a DiscoveryResult to a JSON string."""
    return json.dumps(asdict(result), indent=indent, default=str)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_lambda_client(session: boto3.Session):
    """Return a Lambda client pinned to the configured region."""
    return session.client("lambda", region_name=Config.AWS_REGION)


def _resolve_account_id(session: boto3.Session) -> str:
    """
    Retrieve the AWS account ID via STS.
    Returns 'unknown' if STS is unreachable rather than aborting the scan.
    """
    try:
        sts = session.client("sts", region_name=Config.AWS_REGION)
        return sts.get_caller_identity()["Account"]
    except ClientError as exc:
        logger.warning("Could not resolve account ID via STS: %s", exc)
        return "unknown"


def _iter_functions(client, errors: list[dict]) -> Iterator[dict]:
    """
    Yield raw Lambda function dicts one at a time, handling pagination,
    throttling (exponential backoff), and permission errors gracefully.

    Iterates the boto3 paginator with a plain for-loop — the correct pattern.

    The previous implementation used next(iter(page_iterator)) inside a while
    loop, which called iter() on the paginator object on every iteration.
    iter() on a boto3 paginator returns a brand-new page iterator starting at
    page 1 each time, so the loop never advanced past the first page and ran
    forever.
    """
    retries: int = 0

    while True:
        try:
            paginator = client.get_paginator("list_functions")
            page_number: int = 0

            for page in paginator.paginate():
                page_number += 1
                functions_on_page = page.get("Functions", [])
                logger.info(
                    "Page %d: received %d function(s).", page_number, len(functions_on_page)
                )
                for fn in functions_on_page:
                    yield fn

            # for-loop completed — all pages consumed, exit the retry loop
            break

        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_msg = exc.response["Error"]["Message"]

            if error_code in _THROTTLE_ERROR_CODES:
                if retries >= _MAX_RETRIES:
                    logger.error(
                        "Exceeded max retries (%d) due to throttling. Aborting.",
                        _MAX_RETRIES,
                    )
                    errors.append(_build_error(error_code, error_msg, fatal=True))
                    break

                wait = _backoff_seconds(retries)
                logger.warning(
                    "Throttled by Lambda API (attempt %d/%d). Retrying in %.1fs ...",
                    retries + 1, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
                retries += 1
                # Loop back to top — a fresh paginator.paginate() restarts
                # from page 1, which is safe for list_functions.

            elif error_code in _PERMISSION_ERROR_CODES:
                logger.error(
                    "Permission denied listing Lambda functions (%s): %s",
                    error_code, error_msg,
                )
                raise PermissionError(
                    f"Insufficient permissions to list Lambda functions: {error_msg}"
                ) from exc

            else:
                logger.error(
                    "Unexpected API error during pagination: %s — %s", error_code, error_msg
                )
                errors.append(_build_error(error_code, error_msg, fatal=True))
                break


def _normalise(raw: dict) -> FunctionRecord:
    """
    Map a raw Lambda API response dict to a typed FunctionRecord.
    All fields use safe .get() access with sensible defaults.
    """
    layers = [
        layer.get("Arn", "")
        for layer in raw.get("Layers", [])
        if layer.get("Arn")
    ]

    return FunctionRecord(
        function_name=raw["FunctionName"],
        function_arn=raw["FunctionArn"],
        runtime=raw.get("Runtime", "provided"),
        memory_mb=raw.get("MemorySize", 128),
        timeout_seconds=raw.get("Timeout", 3),
        last_modified=raw.get("LastModified", ""),
        description=raw.get("Description", ""),
        layers=layers,
        package_type=raw.get("PackageType", "Zip"),
        architectures=raw.get("Architectures", ["x86_64"]),
    )


def _backoff_seconds(attempt: int) -> float:
    """Full-jitter exponential backoff capped at 30 seconds."""
    import random
    cap = 30.0
    return min(cap, _BASE_BACKOFF_SECONDS * (2 ** attempt)) * random.random()


def _build_error(code: str, message: str, *, fatal: bool = False) -> dict:
    return {
        "error_code": code,
        "message": message,
        "fatal": fatal,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
