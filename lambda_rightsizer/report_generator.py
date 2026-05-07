"""
report_generator.py
-------------------
Generates all output formats from a list of OptimizationRecord dicts.

Outputs (all written to a timestamped sub-folder under OUTPUT_DIR):
  - Console  : colorized summary table + totals block printed to stdout
  - CSV      : rightsizer_report_<ts>.csv  — flat, spreadsheet-friendly
  - JSON     : rightsizer_report_<ts>.json — full payload including summary

Fields included in every format:
  Function Name, Runtime, Current Memory, Peak Usage, Avg Usage,
  Min Usage, P95 Usage, Recommended Memory, Change (MB), Waste %,
  Utilization %, Estimated Savings (MB), Optimization Status,
  Risk Level, Invocation Count, Data Source, Recommendation

Public API:
  generate_all(records, output_dir?)  -> ReportPaths
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from tabulate import tabulate
from colorama import Fore, Back, Style, init as colorama_init

from lambda_rightsizer.config import Config

colorama_init(autoreset=True)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Generated once per process so all three formats share the same timestamp
_RUN_TS: str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_RUN_TS_HUMAN: str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# Console column widths
_MAX_FN_NAME_LEN: int = 40

# Status -> foreground color
_STATUS_COLOR: dict[str, str] = {
    "over_provisioned":  Fore.YELLOW,
    "under_provisioned": Fore.RED,
    "watch":             Fore.MAGENTA,
    "optimal":           Fore.GREEN,
    "insufficient_data": Fore.CYAN,
    "no_data":           Fore.WHITE,
}

# Risk level -> foreground color
_RISK_COLOR: dict[str, str] = {
    "very_low":  Fore.GREEN,
    "low":       Fore.GREEN,
    "medium":    Fore.YELLOW,
    "high":      Fore.RED,
    "very_high": Fore.RED + Style.BRIGHT,
}

# CSV column order
_CSV_FIELDS: list[str] = [
    "function_name",
    "function_arn",
    "runtime",
    "allocated_mb",
    "max_used_mb",
    "avg_used_mb",
    "min_used_mb",
    "p95_used_mb",
    "utilization_percent",
    "waste_percent",
    "recommended_mb",
    "change_mb",
    "safety_floor_mb",
    "invocation_count",
    "data_source",
    "status",
    "risk_level",
    "risk_score",
    "recommendation",
]


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass
class ReportPaths:
    """Paths to every generated output file."""
    run_timestamp: str
    output_dir: str
    csv: str
    json: str

    def __str__(self) -> str:
        return (
            f"  CSV  : {self.csv}\n"
            f"  JSON : {self.json}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_all(
    records: list[dict],
    output_dir: str = Config.OUTPUT_DIR,
) -> ReportPaths:
    """
    Generate console, CSV, and JSON reports from optimizer output records.

    Args:
        records:    List of dicts as returned by optimizer.analyze().
        output_dir: Root output directory.  A timestamped sub-folder is
                    created inside it for each run.

    Returns:
        ReportPaths with paths to every written file.
    """
    run_dir = os.path.join(output_dir, _RUN_TS)
    os.makedirs(run_dir, exist_ok=True)
    logger.info("Writing reports to %s", run_dir)

    # Console is always printed; it doesn't produce a file path
    _console_report(records)

    csv_path = _csv_report(records, run_dir)
    json_path = _json_report(records, run_dir)

    paths = ReportPaths(
        run_timestamp=_RUN_TS,
        output_dir=run_dir,
        csv=csv_path,
        json=json_path,
    )

    logger.info("Reports written:\n%s", paths)
    return paths


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------

def _console_report(records: list[dict]) -> None:
    summary = _build_summary(records)
    errors = [r for r in records if r.get("status") == "insufficient_data"]
    actionable = [r for r in records if r.get("status") not in ("insufficient_data", "no_data")]

    _print_header()
    _print_main_table(records)
    _print_summary_block(summary)

    if actionable:
        _print_recommendations(actionable)

    if errors:
        _print_error_table(errors)

    _print_footer()


def _print_header() -> None:
    width = 100
    print()
    print(Fore.CYAN + Style.BRIGHT + "=" * width)
    print(f"  {'LAMBDA RIGHTSIZER':^96}")
    print(f"  Generated : {_RUN_TS_HUMAN:<40}  Region : {Config.AWS_REGION}")
    print(f"  Lookback  : {Config.LOOKBACK_DAYS} days{'':<35}  Waste threshold : {Config.WASTE_THRESHOLD_PERCENT:.0f}%")
    print("=" * width + Style.RESET_ALL)


def _print_main_table(records: list[dict]) -> None:
    rows = []
    sorted_records = sorted(
        records,
        key=lambda r: (
            # Sort: over_provisioned first, then by waste% desc, then by name
            0 if r.get("status") == "over_provisioned" else
            1 if r.get("status") == "under_provisioned" else
            2 if r.get("status") == "watch" else
            3 if r.get("status") == "optimal" else 4,
            -(r.get("waste_percent") or 0),
        ),
    )

    for r in sorted_records:
        status = r.get("status", "no_data")
        risk = r.get("risk_level", "")
        sc = _STATUS_COLOR.get(status, "")
        rc = _RISK_COLOR.get(risk, "")

        fn_name = r["function_name"]
        if len(fn_name) > _MAX_FN_NAME_LEN:
            fn_name = "…" + fn_name[-(  _MAX_FN_NAME_LEN - 1):]

        rows.append([
            sc + fn_name + Style.RESET_ALL,
            r.get("runtime", "N/A"),
            _fmt_mb(r.get("allocated_mb")),
            _fmt_mb(r.get("max_used_mb")),
            _fmt_mb(r.get("avg_used_mb")),
            _fmt_mb(r.get("p95_used_mb")),
            _fmt_pct(r.get("utilization_percent")),
            _fmt_pct(r.get("waste_percent")),
            _fmt_mb(r.get("recommended_mb")),
            _fmt_change(r.get("change_mb", 0)),
            rc + risk + Style.RESET_ALL,
            sc + status + Style.RESET_ALL,
        ])

    headers = [
        "Function", "Runtime",
        "Alloc\nMB", "Peak\nMB", "Avg\nMB", "P95\nMB",
        "Util\n%", "Waste\n%",
        "Rec\nMB", "Δ MB",
        "Risk", "Status",
    ]

    print()
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline", stralign="left"))


def _print_summary_block(summary: dict) -> None:
    width = 100
    print()
    print(Fore.CYAN + "-" * width + Style.RESET_ALL)
    print(f"  {'SUMMARY':^96}")
    print(Fore.CYAN + "-" * width + Style.RESET_ALL)

    col1 = [
        f"  Total functions analysed  : {summary['total']}",
        f"  Over-provisioned          : {Fore.YELLOW}{summary['over_provisioned']}{Style.RESET_ALL}",
        f"  Under-provisioned         : {Fore.RED}{summary['under_provisioned']}{Style.RESET_ALL}",
        f"  Watch                     : {Fore.MAGENTA}{summary['watch']}{Style.RESET_ALL}",
    ]
    col2 = [
        f"  Optimal                   : {Fore.GREEN}{summary['optimal']}{Style.RESET_ALL}",
        f"  Insufficient data         : {Fore.CYAN}{summary['insufficient_data']}{Style.RESET_ALL}",
        f"  Total potential savings   : {Fore.YELLOW}{summary['total_savings_mb']} MB{Style.RESET_ALL}",
        f"  Functions with errors     : {Fore.RED}{summary['error_count']}{Style.RESET_ALL}",
    ]

    for l1, l2 in zip(col1, col2):
        print(f"{l1:<60}{l2}")

    print(Fore.CYAN + "-" * width + Style.RESET_ALL)


def _print_recommendations(records: list[dict]) -> None:
    """Print a numbered list of human-readable recommendations."""
    actionable = [
        r for r in records
        if r.get("status") in ("over_provisioned", "under_provisioned", "watch")
    ]
    if not actionable:
        return

    print()
    print(Fore.CYAN + Style.BRIGHT + "  RECOMMENDATIONS" + Style.RESET_ALL)
    print()

    for i, r in enumerate(
        sorted(actionable, key=lambda x: x.get("risk_score", 3)),
        start=1,
    ):
        sc = _STATUS_COLOR.get(r.get("status", ""), "")
        rc = _RISK_COLOR.get(r.get("risk_level", ""), "")
        print(
            f"  {i:>3}. {sc}{r['function_name']}{Style.RESET_ALL}"
            f"  [{rc}{r.get('risk_level','').upper()}{Style.RESET_ALL}]"
        )
        print(f"       {r.get('recommendation', '')}")
        print()


def _print_error_table(errors: list[dict]) -> None:
    """Print functions that could not be analysed due to missing data."""
    print()
    print(Fore.CYAN + "  FUNCTIONS WITH INSUFFICIENT DATA" + Style.RESET_ALL)
    rows = [
        [
            r["function_name"],
            r.get("runtime", "N/A"),
            _fmt_mb(r.get("allocated_mb")),
            r.get("invocation_count", 0),
            r.get("data_source", "N/A"),
        ]
        for r in errors
    ]
    headers = ["Function", "Runtime", "Alloc MB", "Invocations", "Data Source"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


def _print_footer() -> None:
    print()
    print(Fore.CYAN + "=" * 100 + Style.RESET_ALL)
    print()


# ---------------------------------------------------------------------------
# CSV report
# ---------------------------------------------------------------------------

def _csv_report(records: list[dict], run_dir: str) -> str:
    filepath = os.path.join(run_dir, f"rightsizer_report_{_RUN_TS}.csv")

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    logger.info("CSV  → %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------

def _json_report(records: list[dict], run_dir: str) -> str:
    filepath = os.path.join(run_dir, f"rightsizer_report_{_RUN_TS}.json")

    # Strip the raw samples list from JSON output to keep file size manageable;
    # the aggregated stats are sufficient for reporting purposes.
    clean_records = [_strip_samples(r) for r in records]

    payload = {
        "meta": {
            "generated_at": _RUN_TS_HUMAN,
            "generated_at_iso": _RUN_TS,
            "region": Config.AWS_REGION,
            "lookback_days": Config.LOOKBACK_DAYS,
            "waste_threshold_percent": Config.WASTE_THRESHOLD_PERCENT,
            "min_invocations": Config.MIN_INVOCATIONS,
        },
        "summary": _build_summary(records),
        "functions": clean_records,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    logger.info("JSON → %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_summary(records: list[dict]) -> dict:
    actionable = [
        r for r in records
        if r.get("status") == "over_provisioned"
    ]
    return {
        "total": len(records),
        "over_provisioned": sum(1 for r in records if r.get("status") == "over_provisioned"),
        "under_provisioned": sum(1 for r in records if r.get("status") == "under_provisioned"),
        "watch": sum(1 for r in records if r.get("status") == "watch"),
        "optimal": sum(1 for r in records if r.get("status") == "optimal"),
        "insufficient_data": sum(
            1 for r in records if r.get("status") in ("insufficient_data", "no_data")
        ),
        "error_count": sum(1 for r in records if r.get("data_source") == "error"),
        "total_savings_mb": sum(
            max(0, (r.get("allocated_mb") or 0) - (r.get("recommended_mb") or 0))
            for r in actionable
        ),
        "functions_with_recommendations": len(actionable),
    }


def _strip_samples(record: dict) -> dict:
    """Return a copy of the record without the raw samples list."""
    return {k: v for k, v in record.items() if k != "samples"}


def _fmt_mb(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return str(int(round(value)))


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1f}%"


def _fmt_change(change_mb: int) -> str:
    if change_mb == 0:
        return "—"
    sign = "+" if change_mb > 0 else ""
    return f"{sign}{change_mb}"
