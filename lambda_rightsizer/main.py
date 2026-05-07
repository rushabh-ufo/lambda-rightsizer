"""
main.py
-------
Entry point and CLI for Lambda Rightsizer.

Full workflow:
  1. Parse CLI arguments / merge with env config
  2. Validate AWS credentials
  3. Discover all Lambda functions          (lambda_discovery)
  4. Fetch CloudWatch memory metrics        (metrics_analyzer)  [parallel]
  5. Calculate waste + recommendations      (optimizer)
  6. Generate console / CSV / JSON reports  (report_generator)
  7. Generate remediation + rollback scripts (remediation_script_generator)
  8. Print final summary dashboard

CLI usage:
  python -m lambda_rightsizer.main [options]

  --region      AWS region (overrides AWS_REGION env var)
  --profile     AWS CLI profile (overrides AWS_PROFILE env var)
  --days        Lookback window in days (overrides LOOKBACK_DAYS)
  --output      Output directory (overrides OUTPUT_DIR)
  --workers     Parallel workers for metrics fetching (default: 5)
  --dry-run     Skip report/script generation; print analysis only
  --no-remediation  Skip remediation script generation
  --log-level   Logging verbosity: DEBUG | INFO | WARNING | ERROR
  --filter      Comma-separated substring filter on function names
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
from colorama import Fore, Style, init as colorama_init

from lambda_rightsizer.config import Config
from lambda_rightsizer import (
    lambda_discovery,
    metrics_analyzer,
    optimizer,
    report_generator,
    remediation_script_generator,
)

colorama_init(autoreset=True)

# ---------------------------------------------------------------------------
# Logging — configured before anything else so early errors are captured
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=Config.get_log_level(),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stderr,   # keep stderr clean from report output on stdout
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_WORKERS: int = 5
_EXIT_OK: int = 0
_EXIT_ERR: int = 1
_EXIT_NO_FUNCTIONS: int = 2


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lambda-rightsizer",
        description=(
            "Scan AWS Lambda functions, analyse CloudWatch memory usage, "
            "and generate right-sizing recommendations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables (all overridable via CLI flags):\n"
            "  AWS_REGION, AWS_PROFILE, LOOKBACK_DAYS, OUTPUT_DIR,\n"
            "  WASTE_THRESHOLD_PERCENT, MIN_INVOCATIONS, LOG_LEVEL\n\n"
            "Examples:\n"
            "  python -m lambda_rightsizer.main --region eu-west-1 --days 30\n"
            "  python -m lambda_rightsizer.main --dry-run --filter payment,auth\n"
            "  python -m lambda_rightsizer.main --output ./reports --workers 10\n"
        ),
    )

    parser.add_argument(
        "--region",
        default=None,
        metavar="REGION",
        help=f"AWS region to scan (default: {Config.AWS_REGION})",
    )
    parser.add_argument(
        "--profile",
        default=None,
        metavar="PROFILE",
        help=f"AWS CLI profile name (default: {Config.AWS_PROFILE})",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help=f"Lookback window in days (default: {Config.LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="DIR",
        help=f"Output directory for reports and scripts (default: {Config.OUTPUT_DIR})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=_DEFAULT_WORKERS,
        metavar="N",
        help=f"Parallel workers for metrics fetching (default: {_DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Analyse only — do not write any output files",
    )
    parser.add_argument(
        "--no-remediation",
        action="store_true",
        default=False,
        help="Skip remediation and rollback script generation",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help=f"Logging verbosity (default: {Config.LOG_LEVEL})",
    )
    parser.add_argument(
        "--filter",
        default=None,
        metavar="TERMS",
        help=(
            "Comma-separated substrings to filter function names "
            "(e.g. --filter payment,auth). Case-insensitive."
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Config override — apply CLI args on top of env/Config defaults
# ---------------------------------------------------------------------------

def _apply_cli_overrides(args: argparse.Namespace) -> None:
    """Mutate Config class attributes in-place based on parsed CLI args."""
    if args.region:
        Config.AWS_REGION = args.region
    if args.profile:
        Config.AWS_PROFILE = args.profile
    if args.days is not None:
        if args.days < 1:
            _die("--days must be >= 1")
        Config.LOOKBACK_DAYS = args.days
    if args.output:
        Config.OUTPUT_DIR = args.output
    if args.log_level:
        Config.LOG_LEVEL = args.log_level
        logging.getLogger().setLevel(Config.get_log_level())


# ---------------------------------------------------------------------------
# AWS session
# ---------------------------------------------------------------------------

def _build_session() -> boto3.Session:
    """
    Create and validate a boto3 Session.
    Exits with a clear error message on credential/profile failures.
    """
    from botocore.config import Config as BotocoreConfig
    try:
        session = boto3.Session(
            profile_name=Config.AWS_PROFILE,
            region_name=Config.AWS_REGION,
        )
        # Short timeout so a misconfigured profile or unreachable endpoint
        # fails fast rather than hanging for the default 60s connect timeout.
        sts_config = BotocoreConfig(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})
        identity = session.client(
            "sts",
            region_name=Config.AWS_REGION,
            config=sts_config,
        ).get_caller_identity()
        logger.info(
            "Authenticated | account=%s | arn=%s",
            identity["Account"],
            identity["Arn"],
        )
        return session
    except ProfileNotFound:
        _die(f"AWS profile '{Config.AWS_PROFILE}' not found. "
             "Check ~/.aws/credentials or set AWS_PROFILE.")
    except NoCredentialsError:
        _die("No AWS credentials found. Configure via profile, env vars, or IAM role.")
    except ClientError as exc:
        _die(f"AWS authentication failed: {exc}")


# ---------------------------------------------------------------------------
# Metrics fetching (thread-safe worker)
# ---------------------------------------------------------------------------

def _fetch_metrics(session: boto3.Session, fn_meta: dict) -> dict:
    """
    Fetch CloudWatch memory metrics for one function.
    Returns a plain dict (MetricsResult serialised via asdict).
    Catches all exceptions so a single failure never aborts the thread pool.
    """
    function_name = fn_meta["function_name"]
    try:
        result = metrics_analyzer.analyze_function(
            session=session,
            function_name=function_name,
            allocated_memory_mb=fn_meta.get("memory_mb"),
        )
        return asdict(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Metrics fetch failed for %s: %s", function_name, exc)
        return {
            "function_name": function_name,
            "max_memory_used_mb": None,
            "min_memory_used_mb": None,
            "avg_memory_used_mb": None,
            "p95_memory_used_mb": None,
            "utilization_percent": None,
            "invocation_count": 0,
            "data_source": "error",
            "warnings": [str(exc)],
        }


# ---------------------------------------------------------------------------
# Progress indicator
# ---------------------------------------------------------------------------

class _Progress:
    """
    Minimal inline progress counter that writes to stderr so it doesn't
    interfere with stdout report output.
    """

    def __init__(self, total: int, label: str) -> None:
        self._total = total
        self._done = 0
        self._label = label
        self._start = time.monotonic()

    def tick(self, name: str = "") -> None:
        self._done += 1
        pct = self._done / self._total * 100 if self._total else 100
        bar_len = 30
        filled = int(bar_len * self._done / max(self._total, 1))
        bar = "█" * filled + "░" * (bar_len - filled)
        elapsed = time.monotonic() - self._start
        suffix = f" {name[:35]:<35}" if name else ""
        print(
            f"\r  {self._label}  [{bar}] {self._done}/{self._total} ({pct:.0f}%)"
            f"  {elapsed:.1f}s{suffix}",
            end="",
            flush=True,
            file=sys.stderr,
        )

    def done(self) -> None:
        elapsed = time.monotonic() - self._start
        print(
            f"\r  {self._label}  [{'█' * 30}] {self._total}/{self._total} (100%)"
            f"  {elapsed:.1f}s  complete{' ' * 40}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Summary dashboard
# ---------------------------------------------------------------------------

def _print_dashboard(
    records: list[dict],
    report_paths: Optional[report_generator.ReportPaths],
    remediation_paths: Optional[remediation_script_generator.RemediationPaths],
    elapsed: float,
    dry_run: bool,
) -> None:
    """Print the final summary dashboard after all steps complete."""
    total = len(records)
    over = sum(1 for r in records if r.get("status") == "over_provisioned")
    under = sum(1 for r in records if r.get("status") == "under_provisioned")
    watch = sum(1 for r in records if r.get("status") == "watch")
    optimal = sum(1 for r in records if r.get("status") == "optimal")
    no_data = sum(1 for r in records if r.get("status") in ("insufficient_data", "no_data"))
    errors = sum(1 for r in records if r.get("data_source") == "error")

    savings_mb = sum(
        max(0, (r.get("allocated_mb") or 0) - (r.get("recommended_mb") or 0))
        for r in records
        if r.get("status") == "over_provisioned"
    )

    w = 60
    c = Fore.CYAN + Style.BRIGHT
    r = Style.RESET_ALL

    print()
    print(c + "╔" + "═" * (w - 2) + "╗" + r)
    print(c + "║" + r + f"{'  LAMBDA RIGHTSIZER — RUN COMPLETE':^{w - 2}}" + c + "║" + r)
    print(c + "╠" + "═" * (w - 2) + "╣" + r)

    def row(label: str, value: str, color: str = "") -> None:
        val_str = color + value + r if color else value
        print(c + "║" + r + f"  {label:<28}{val_str}" + c + "║" + r)

    row("Region",          Config.AWS_REGION)
    row("Lookback",        f"{Config.LOOKBACK_DAYS} days")
    row("Functions scanned", str(total))
    row("Elapsed",         f"{elapsed:.1f}s")
    print(c + "╠" + "═" * (w - 2) + "╣" + r)
    row("Over-provisioned",  str(over),    Fore.YELLOW)
    row("Under-provisioned", str(under),   Fore.RED)
    row("Watch",             str(watch),   Fore.MAGENTA)
    row("Optimal",           str(optimal), Fore.GREEN)
    row("Insufficient data", str(no_data), Fore.CYAN)
    row("Metric errors",     str(errors),  Fore.RED if errors else Fore.GREEN)
    print(c + "╠" + "═" * (w - 2) + "╣" + r)
    row("Potential savings", f"{savings_mb} MB", Fore.YELLOW if savings_mb else Fore.GREEN)

    if dry_run:
        print(c + "╠" + "═" * (w - 2) + "╣" + r)
        row("Mode", "DRY RUN — no files written", Fore.YELLOW)
    else:
        if report_paths:
            print(c + "╠" + "═" * (w - 2) + "╣" + r)
            row("CSV report",  report_paths.csv)
            row("JSON report", report_paths.json)
        if remediation_paths:
            print(c + "╠" + "═" * (w - 2) + "╣" + r)
            row("Remediation", remediation_paths.remediation_script)
            row("Rollback",    remediation_paths.rollback_script)
            row("Backup",      remediation_paths.backup_json)

    print(c + "╚" + "═" * (w - 2) + "╝" + r)
    print()


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run(argv: Optional[list[str]] = None) -> int:
    """
    Execute the full Lambda Rightsizer workflow.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 = success, 1 = error, 2 = no functions found).
    """
    start_time = time.monotonic()

    # --- Parse CLI ---
    parser = _build_parser()
    args = parser.parse_args(argv)
    _apply_cli_overrides(args)

    logger.info(
        "Lambda Rightsizer starting | region=%s | lookback=%d days | workers=%d",
        Config.AWS_REGION,
        Config.LOOKBACK_DAYS,
        args.workers,
    )

    if args.dry_run:
        logger.info("DRY RUN mode — no output files will be written.")

    # --- AWS session ---
    session = _build_session()

    # -------------------------------------------------------------------------
    # Step 1: Discover Lambda functions
    # -------------------------------------------------------------------------
    _print_step(1, "Discovering Lambda functions ...")

    try:
        discovery = lambda_discovery.discover_functions(session)
    except PermissionError as exc:
        _die(str(exc))

    if discovery.errors:
        for err in discovery.errors:
            logger.warning("Discovery error: [%s] %s", err["error_code"], err["message"])

    functions = [asdict(fn) for fn in discovery.functions]

    # Apply name filter if provided
    if args.filter:
        terms = [t.strip().lower() for t in args.filter.split(",") if t.strip()]
        before = len(functions)
        functions = [
            fn for fn in functions
            if any(t in fn["function_name"].lower() for t in terms)
        ]
        logger.info(
            "Filter '%s' applied: %d → %d function(s).",
            args.filter, before, len(functions),
        )

    if not functions:
        logger.warning("No Lambda functions found in region %s.", Config.AWS_REGION)
        return _EXIT_NO_FUNCTIONS

    _print_step_done(f"{len(functions)} function(s) discovered")

    # -------------------------------------------------------------------------
    # Step 2: Fetch CloudWatch metrics (parallel)
    # -------------------------------------------------------------------------
    _print_step(2, f"Fetching CloudWatch metrics ({args.workers} workers) ...")

    metrics_map: dict[str, dict] = {}
    progress = _Progress(len(functions), "Metrics")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_fn = {
            pool.submit(_fetch_metrics, session, fn): fn
            for fn in functions
        }
        for future in as_completed(future_to_fn):
            fn_meta = future_to_fn[future]
            result = future.result()
            metrics_map[fn_meta["function_name"]] = result
            progress.tick(fn_meta["function_name"])

    progress.done()
    _print_step_done("Metrics fetched")

    # -------------------------------------------------------------------------
    # Step 3: Optimize — calculate waste and recommendations
    # -------------------------------------------------------------------------
    _print_step(3, "Calculating recommendations ...")

    records: list[dict] = []
    for fn_meta in functions:
        metrics = metrics_map.get(fn_meta["function_name"], {})
        records.append(optimizer.analyze(fn_meta, metrics))

    actionable = sum(
        1 for r in records
        if r.get("status") in ("over_provisioned", "under_provisioned")
    )
    _print_step_done(f"{actionable} function(s) need attention")

    # -------------------------------------------------------------------------
    # Step 4: Generate reports
    # -------------------------------------------------------------------------
    _print_step(4, "Generating reports ...")

    report_paths: Optional[report_generator.ReportPaths] = None

    if args.dry_run:
        # In dry-run mode still print the console report but skip file writes
        report_generator._console_report(records)  # noqa: SLF001
        _print_step_done("Console report printed (dry-run — no files written)")
    else:
        report_paths = report_generator.generate_all(records, Config.OUTPUT_DIR)
        _print_step_done(f"Reports written to {report_paths.output_dir}")

    # -------------------------------------------------------------------------
    # Step 5: Generate remediation scripts
    # -------------------------------------------------------------------------
    remediation_paths: Optional[remediation_script_generator.RemediationPaths] = None

    if args.no_remediation or args.dry_run:
        _print_step(5, "Remediation script generation skipped.")
        _print_step_done("(--dry-run or --no-remediation set)")
    else:
        _print_step(5, "Generating remediation scripts ...")
        remediation_paths = remediation_script_generator.generate(records, Config.OUTPUT_DIR)
        _print_step_done(f"Scripts written to {remediation_paths.output_dir}")

    # -------------------------------------------------------------------------
    # Step 6: Final summary dashboard
    # -------------------------------------------------------------------------
    elapsed = time.monotonic() - start_time
    _print_dashboard(records, report_paths, remediation_paths, elapsed, args.dry_run)

    return _EXIT_OK


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_step(n: int, message: str) -> None:
    print(
        f"\n{Fore.CYAN}[{n}/5]{Style.RESET_ALL}  {message}",
        file=sys.stderr,
    )


def _print_step_done(detail: str = "") -> None:
    suffix = f"  {Fore.CYAN}{detail}{Style.RESET_ALL}" if detail else ""
    print(
        f"       {Fore.GREEN}✓{Style.RESET_ALL}{suffix}",
        file=sys.stderr,
    )


def _die(message: str) -> None:
    """Print a fatal error and exit."""
    print(f"\n{Fore.RED}[FATAL]{Style.RESET_ALL}  {message}\n", file=sys.stderr)
    sys.exit(_EXIT_ERR)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(run())
