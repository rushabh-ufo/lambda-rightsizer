"""
remediation_script_generator.py
--------------------------------
Generates a production-safe bash remediation package from optimizer records.

Three files are written to a timestamped sub-folder:

  remediation_<ts>.sh   — applies recommended memory changes via AWS CLI
                           Supports DRY_RUN, per-function confirmation prompts,
                           and risk-gated auto-approval.

  rollback_<ts>.sh      — restores every function to its original memory value
                           using the backup captured at generation time.

  backup_<ts>.json      — machine-readable snapshot of current config for every
                           targeted function (name, ARN, original memory).

Only functions with status "over_provisioned" or "under_provisioned" are
included.  "watch" and "optimal" functions are explicitly skipped with a
comment explaining why.

Public API:
  generate(records, output_dir?)  -> RemediationPaths
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from lambda_rightsizer.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Statuses that warrant a memory change
_ACTIONABLE_STATUSES: frozenset[str] = frozenset({"over_provisioned", "under_provisioned"})

# Risk scores at or above this threshold require explicit per-function confirmation
# even when running in non-interactive batch mode (unless FORCE=true).
_HIGH_RISK_THRESHOLD: int = 4

_RUN_TS: str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_RUN_TS_HUMAN: str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass
class RemediationPaths:
    """Paths to every file produced by generate()."""
    run_timestamp: str
    output_dir: str
    remediation_script: str
    rollback_script: str
    backup_json: str

    def __str__(self) -> str:
        return (
            f"  Remediation : {self.remediation_script}\n"
            f"  Rollback    : {self.rollback_script}\n"
            f"  Backup      : {self.backup_json}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(
    records: list[dict],
    output_dir: str = Config.OUTPUT_DIR,
) -> RemediationPaths:
    """
    Generate the remediation script, rollback script, and config backup.

    Args:
        records:    List of dicts as returned by optimizer.analyze().
        output_dir: Root output directory.  Files are written to a
                    timestamped sub-folder inside it.

    Returns:
        RemediationPaths with paths to all three generated files.
    """
    run_dir = os.path.join(output_dir, _RUN_TS)
    os.makedirs(run_dir, exist_ok=True)

    actionable = [r for r in records if r.get("status") in _ACTIONABLE_STATUSES]
    skipped = [r for r in records if r.get("status") not in _ACTIONABLE_STATUSES]

    logger.info(
        "Generating remediation package | actionable=%d | skipped=%d | dir=%s",
        len(actionable), len(skipped), run_dir,
    )

    backup_path = _write_backup(actionable, run_dir)
    remediation_path = _write_remediation_script(actionable, skipped, run_dir)
    rollback_path = _write_rollback_script(actionable, run_dir)

    paths = RemediationPaths(
        run_timestamp=_RUN_TS,
        output_dir=run_dir,
        remediation_script=remediation_path,
        rollback_script=rollback_path,
        backup_json=backup_path,
    )

    logger.info("Remediation package written:\n%s", paths)
    return paths


# ---------------------------------------------------------------------------
# Backup JSON
# ---------------------------------------------------------------------------

def _write_backup(actionable: list[dict], run_dir: str) -> str:
    filepath = os.path.join(run_dir, f"backup_{_RUN_TS}.json")

    payload = {
        "generated_at": _RUN_TS_HUMAN,
        "region": Config.AWS_REGION,
        "purpose": "Pre-remediation config snapshot. Used by rollback script.",
        "functions": [
            {
                "function_name": r["function_name"],
                "function_arn":  r["function_arn"],
                "original_memory_mb": r["allocated_mb"],
                "recommended_memory_mb": r["recommended_mb"],
                "status": r["status"],
                "risk_level": r.get("risk_level", "unknown"),
                "risk_score": r.get("risk_score", 0),
            }
            for r in actionable
        ],
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    logger.info("Backup JSON → %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# Remediation script
# ---------------------------------------------------------------------------

def _write_remediation_script(
    actionable: list[dict],
    skipped: list[dict],
    run_dir: str,
) -> str:
    filepath = os.path.join(run_dir, f"remediation_{_RUN_TS}.sh")

    lines: list[str] = []
    lines += _remediation_header(len(actionable), run_dir)
    lines += _shared_functions()
    lines += _preflight_checks()

    if not actionable:
        lines += [
            'log_info "No actionable functions found. Nothing to do."',
            "exit 0",
            "",
        ]
    else:
        lines += _remediation_summary_block(actionable)
        lines += _global_confirmation_block()

        for record in actionable:
            lines += _remediation_function_block(record)

    if skipped:
        lines += _skipped_block(skipped)

    lines += _remediation_footer()

    _write_script(filepath, lines)
    logger.info("Remediation script → %s", filepath)
    return filepath


def _remediation_header(count: int, run_dir: str) -> list[str]:
    backup_filename = f"backup_{_RUN_TS}.json"
    return [
        "#!/usr/bin/env bash",
        "# " + "=" * 77,
        "# Lambda Rightsizer — Remediation Script",
        f"# Generated  : {_RUN_TS_HUMAN}",
        f"# Region     : {Config.AWS_REGION}",
        f"# Targeted   : {count} function(s)",
        "#",
        "# USAGE:",
        "#   bash remediation.sh                  # interactive, confirms each change",
        "#   DRY_RUN=true bash remediation.sh      # preview only, no AWS calls",
        "#   FORCE=true bash remediation.sh        # skip confirmations (use with care)",
        "#   SKIP_HIGH_RISK=true bash remediation.sh  # skip risk>=4 functions",
        "#",
        "# SAFETY:",
        f"#   A config backup was written to: {backup_filename}",
        "#   Run rollback.sh to restore all functions to their original memory.",
        "#   Each AWS CLI call is idempotent — safe to re-run.",
        "# " + "=" * 77,
        "",
        "set -euo pipefail",
        "",
        f'REGION="{Config.AWS_REGION}"',
        f'RUN_DIR="{run_dir}"',
        f'BACKUP_FILE="{os.path.join(run_dir, f"backup_{_RUN_TS}.json")}"',
        "",
        '# Runtime flags (override via environment variables)',
        'DRY_RUN="${DRY_RUN:-false}"',
        'FORCE="${FORCE:-false}"',
        'SKIP_HIGH_RISK="${SKIP_HIGH_RISK:-false}"',
        "",
        '# Counters',
        "UPDATED=0",
        "SKIPPED=0",
        "FAILED=0",
        "",
    ]


def _shared_functions() -> list[str]:
    return [
        "# ---------------------------------------------------------------------------",
        "# Logging helpers",
        "# ---------------------------------------------------------------------------",
        "",
        "RED='\\033[0;31m'",
        "YELLOW='\\033[1;33m'",
        "GREEN='\\033[0;32m'",
        "CYAN='\\033[0;36m'",
        "BOLD='\\033[1m'",
        "RESET='\\033[0m'",
        "",
        'log_info()  { echo -e "${CYAN}[INFO]${RESET}  $*"; }',
        'log_ok()    { echo -e "${GREEN}[OK]${RESET}    $*"; }',
        'log_warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }',
        'log_error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }',
        'log_dry()   { echo -e "${YELLOW}[DRY-RUN]${RESET} $*"; }',
        "",
        "# ---------------------------------------------------------------------------",
        "# confirm <prompt>",
        "#   Prompts the user for y/n.  Returns 0 for yes, 1 for no.",
        "#   Skipped automatically when FORCE=true.",
        "# ---------------------------------------------------------------------------",
        "confirm() {",
        '  local prompt="$1"',
        '  if [ "$FORCE" = "true" ]; then',
        '    log_info "FORCE=true — auto-confirming: $prompt"',
        "    return 0",
        "  fi",
        '  read -r -p "$(echo -e "${BOLD}${prompt} [y/N]: ${RESET}")" response',
        '  case "$response" in',
        "    [yY][eE][sS]|[yY]) return 0 ;;",
        "    *) return 1 ;;",
        "  esac",
        "}",
        "",
        "# ---------------------------------------------------------------------------",
        "# apply_change <function_name> <new_memory_mb> <old_memory_mb> <risk_score>",
        "#   Core update function.  Handles dry-run, confirmation, AWS CLI call,",
        "#   error capture, and counter updates.",
        "# ---------------------------------------------------------------------------",
        "apply_change() {",
        '  local fn_name="$1"',
        '  local new_mb="$2"',
        '  local old_mb="$3"',
        '  local risk_score="$4"',
        "",
        '  # Skip high-risk functions when SKIP_HIGH_RISK=true',
        f'  if [ "$SKIP_HIGH_RISK" = "true" ] && [ "$risk_score" -ge {_HIGH_RISK_THRESHOLD} ]; then',
        '    log_warn "[$fn_name] Skipping — risk score $risk_score >= {threshold} and SKIP_HIGH_RISK=true."'.replace("{threshold}", str(_HIGH_RISK_THRESHOLD)),
        "    (( SKIPPED++ )) || true",
        "    return 0",
        "  fi",
        "",
        '  if [ "$DRY_RUN" = "true" ]; then',
        '    log_dry "[$fn_name] Would change: ${old_mb}MB → ${new_mb}MB (risk=$risk_score)"',
        "    (( SKIPPED++ )) || true",
        "    return 0",
        "  fi",
        "",
        '  # Per-function confirmation for high-risk changes',
        f'  if [ "$risk_score" -ge {_HIGH_RISK_THRESHOLD} ] && [ "$FORCE" != "true" ]; then',
        '    log_warn "[$fn_name] Risk score is $risk_score — requires explicit confirmation."',
        '    if ! confirm "  Apply change to $fn_name (${old_mb}MB → ${new_mb}MB)?"; then',
        '      log_warn "[$fn_name] Skipped by user."',
        "      (( SKIPPED++ )) || true",
        "      return 0",
        "    fi",
        "  fi",
        "",
        '  log_info "[$fn_name] Updating: ${old_mb}MB → ${new_mb}MB ..."',
        "",
        "  # Apply the memory change via AWS CLI",
        "  if aws lambda update-function-configuration \\",
        '    --region "$REGION" \\',
        '    --function-name "$fn_name" \\',
        '    --memory-size "$new_mb" \\',
        "    --output json > /dev/null 2>&1; then",
        '    log_ok "[$fn_name] Updated successfully."',
        "    (( UPDATED++ )) || true",
        "  else",
        '    log_error "[$fn_name] AWS CLI call failed. Function NOT updated."',
        "    (( FAILED++ )) || true",
        "  fi",
        "}",
        "",
    ]


def _preflight_checks() -> list[str]:
    return [
        "# ---------------------------------------------------------------------------",
        "# Pre-flight checks",
        "# ---------------------------------------------------------------------------",
        "",
        "# Verify AWS CLI is available",
        'if ! command -v aws &> /dev/null; then',
        '  log_error "AWS CLI not found. Install it from https://aws.amazon.com/cli/"',
        "  exit 1",
        "fi",
        "",
        "# Verify AWS credentials are configured",
        'if ! aws sts get-caller-identity --region "$REGION" > /dev/null 2>&1; then',
        '  log_error "AWS credentials not configured or invalid for region $REGION."',
        "  exit 1",
        "fi",
        "",
        'log_info "Pre-flight checks passed."',
        'log_info "Region  : $REGION"',
        'log_info "Dry Run : $DRY_RUN"',
        'log_info "Force   : $FORCE"',
        'log_info "Skip High Risk : $SKIP_HIGH_RISK"',
        'echo ""',
        "",
    ]


def _remediation_summary_block(actionable: list[dict]) -> list[str]:
    lines = [
        "# ---------------------------------------------------------------------------",
        "# Change summary",
        "# ---------------------------------------------------------------------------",
        'echo -e "${BOLD}Functions to be modified:${RESET}"',
        'echo ""',
    ]
    for r in actionable:
        direction = "↓ reduce" if r["status"] == "over_provisioned" else "↑ increase"
        lines.append(
            f'echo "  {r["function_name"]:<50} '
            f'{r["allocated_mb"]}MB → {r["recommended_mb"]}MB  '
            f'({direction})  risk={r.get("risk_level","?")}"'
        )
    lines += ['echo ""']
    return lines


def _global_confirmation_block() -> list[str]:
    return [
        "# ---------------------------------------------------------------------------",
        "# Global confirmation (skipped when FORCE=true or DRY_RUN=true)",
        "# ---------------------------------------------------------------------------",
        'if [ "$DRY_RUN" != "true" ] && [ "$FORCE" != "true" ]; then',
        '  if ! confirm "Proceed with the changes listed above?"; then',
        '    log_warn "Aborted by user."',
        "    exit 0",
        "  fi",
        "fi",
        'echo ""',
        "",
    ]


def _remediation_function_block(r: dict) -> list[str]:
    fn = r["function_name"]
    fn_arn = r["function_arn"]
    old_mb = r["allocated_mb"]
    new_mb = r["recommended_mb"]
    status = r["status"]
    risk_score = r.get("risk_score", 1)
    risk_level = r.get("risk_level", "unknown")
    waste_pct = r.get("waste_percent")
    max_used = r.get("max_used_mb", "N/A")
    avg_used = r.get("avg_used_mb")
    p95_used = r.get("p95_used_mb", "N/A")
    invocations = r.get("invocation_count", 0)
    data_source = r.get("data_source", "unknown")
    recommendation = r.get("recommendation", "")
    change_mb = new_mb - old_mb
    direction = "REDUCE" if change_mb < 0 else "INCREASE"

    waste_str = f"{waste_pct:.1f}%" if waste_pct is not None else "N/A"
    avg_str = f"{avg_used:.0f} MB" if avg_used is not None else "N/A"

    return [
        "# " + "-" * 77,
        f"# Function   : {fn}",
        f"# ARN        : {fn_arn}",
        f"# Action     : {direction}  {old_mb}MB → {new_mb}MB  (Δ {change_mb:+d}MB)",
        f"# Status     : {status}",
        f"# Risk       : {risk_level} (score={risk_score})",
        f"# Peak used  : {max_used} MB   Avg: {avg_str}   P95: {p95_used} MB",
        f"# Waste      : {waste_str}",
        f"# Invocations: {invocations}   Data source: {data_source}",
        f"# Recommendation: {recommendation}",
        "# " + "-" * 77,
        f'apply_change "{fn}" {new_mb} {old_mb} {risk_score}',
        "",
    ]


def _skipped_block(skipped: list[dict]) -> list[str]:
    lines = [
        "",
        "# ---------------------------------------------------------------------------",
        "# Functions NOT modified (no action required)",
        "# ---------------------------------------------------------------------------",
        "#",
    ]
    for r in skipped:
        reason = {
            "optimal":           "memory is well-sized",
            "watch":             "utilization approaching threshold — monitor only",
            "insufficient_data": "not enough invocation data",
            "no_data":           "no metrics available",
        }.get(r.get("status", ""), r.get("status", "unknown"))
        lines.append(f'# {r["function_name"]:<50} skipped — {reason}')
    lines.append("#")
    return lines


def _remediation_footer() -> list[str]:
    return [
        "",
        "# ---------------------------------------------------------------------------",
        "# Final summary",
        "# ---------------------------------------------------------------------------",
        'echo ""',
        'echo -e "${BOLD}=== Remediation complete ===${RESET}"',
        'echo -e "  Updated : ${GREEN}$UPDATED${RESET}"',
        'echo -e "  Skipped : ${YELLOW}$SKIPPED${RESET}"',
        'echo -e "  Failed  : ${RED}$FAILED${RESET}"',
        'echo ""',
        'if [ "$FAILED" -gt 0 ]; then',
        '  log_error "Some updates failed. Review the output above."',
        "  exit 1",
        "fi",
    ]


# ---------------------------------------------------------------------------
# Rollback script
# ---------------------------------------------------------------------------

def _write_rollback_script(actionable: list[dict], run_dir: str) -> str:
    filepath = os.path.join(run_dir, f"rollback_{_RUN_TS}.sh")

    lines: list[str] = []
    lines += _rollback_header(len(actionable), run_dir)
    lines += _shared_functions()
    lines += _preflight_checks()

    if not actionable:
        lines += [
            'log_info "Nothing to roll back."',
            "exit 0",
            "",
        ]
    else:
        lines += _rollback_summary_block(actionable)
        lines += _global_confirmation_block()

        for record in actionable:
            lines += _rollback_function_block(record)

    lines += _rollback_footer()

    _write_script(filepath, lines)
    logger.info("Rollback script  → %s", filepath)
    return filepath


def _rollback_header(count: int, run_dir: str) -> list[str]:
    return [
        "#!/usr/bin/env bash",
        "# " + "=" * 77,
        "# Lambda Rightsizer — Rollback Script",
        f"# Generated  : {_RUN_TS_HUMAN}",
        f"# Region     : {Config.AWS_REGION}",
        f"# Restores   : {count} function(s) to their pre-remediation memory",
        "#",
        "# USAGE:",
        "#   bash rollback.sh                  # interactive confirmation",
        "#   DRY_RUN=true bash rollback.sh      # preview only",
        "#   FORCE=true bash rollback.sh        # skip confirmations",
        "#",
        "# This script restores the ORIGINAL memory values captured at analysis time.",
        "# Run this if the remediation caused unexpected behaviour.",
        "# " + "=" * 77,
        "",
        "set -euo pipefail",
        "",
        f'REGION="{Config.AWS_REGION}"',
        "",
        'DRY_RUN="${DRY_RUN:-false}"',
        'FORCE="${FORCE:-false}"',
        "",
        "RESTORED=0",
        "SKIPPED=0",
        "FAILED=0",
        "",
    ]


def _rollback_summary_block(actionable: list[dict]) -> list[str]:
    lines = [
        'echo -e "${BOLD}Functions to be restored:${RESET}"',
        'echo ""',
    ]
    for r in actionable:
        lines.append(
            f'echo "  {r["function_name"]:<50} '
            f'{r["recommended_mb"]}MB → {r["allocated_mb"]}MB  (restore original)"'
        )
    lines += ['echo ""']
    return lines


def _rollback_function_block(r: dict) -> list[str]:
    fn = r["function_name"]
    original_mb = r["allocated_mb"]   # restore TO the original value
    current_mb = r["recommended_mb"]  # what remediation set it to

    return [
        "# " + "-" * 77,
        f"# Restore: {fn}",
        f"#   Remediation set : {current_mb} MB",
        f"#   Restoring to    : {original_mb} MB  (original allocation)",
        "# " + "-" * 77,
        f'apply_change "{fn}" {original_mb} {current_mb} 1',
        "",
    ]


def _rollback_footer() -> list[str]:
    return [
        "",
        'echo ""',
        'echo -e "${BOLD}=== Rollback complete ===${RESET}"',
        'echo -e "  Restored : ${GREEN}$RESTORED${RESET}"',
        'echo -e "  Skipped  : ${YELLOW}$SKIPPED${RESET}"',
        'echo -e "  Failed   : ${RED}$FAILED${RESET}"',
        'echo ""',
        'if [ "$FAILED" -gt 0 ]; then',
        '  log_error "Some restores failed. Review the output above."',
        "  exit 1",
        "fi",
    ]


# ---------------------------------------------------------------------------
# File writer
# ---------------------------------------------------------------------------

def _write_script(filepath: str, lines: list[str]) -> None:
    """Write lines to a bash script and make it executable."""
    with open(filepath, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    # chmod +x (no-op on Windows)
    try:
        mode = os.stat(filepath).st_mode
        os.chmod(filepath, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass
