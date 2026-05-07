#!/usr/bin/env bash
# =============================================================================
# Lambda Rightsizer — Rollback Script
# Generated  : 2026-05-05 14:20:57 UTC
# Region     : us-east-1
# Restores   : 3 function(s) to their pre-remediation memory
#
# USAGE:
#   bash rollback.sh                  # interactive confirmation
#   DRY_RUN=true bash rollback.sh      # preview only
#   FORCE=true bash rollback.sh        # skip confirmations
#
# This script restores the ORIGINAL memory values captured at analysis time.
# Run this if the remediation caused unexpected behaviour.
# =============================================================================

set -euo pipefail

REGION="us-east-1"

DRY_RUN="${DRY_RUN:-false}"
FORCE="${FORCE:-false}"

RESTORED=0
SKIPPED=0
FAILED=0

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${RESET}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${RESET}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
log_error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
log_dry()   { echo -e "${YELLOW}[DRY-RUN]${RESET} $*"; }

# ---------------------------------------------------------------------------
# confirm <prompt>
#   Prompts the user for y/n.  Returns 0 for yes, 1 for no.
#   Skipped automatically when FORCE=true.
# ---------------------------------------------------------------------------
confirm() {
  local prompt="$1"
  if [ "$FORCE" = "true" ]; then
    log_info "FORCE=true — auto-confirming: $prompt"
    return 0
  fi
  read -r -p "$(echo -e "${BOLD}${prompt} [y/N]: ${RESET}")" response
  case "$response" in
    [yY][eE][sS]|[yY]) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# apply_change <function_name> <new_memory_mb> <old_memory_mb> <risk_score>
#   Core update function.  Handles dry-run, confirmation, AWS CLI call,
#   error capture, and counter updates.
# ---------------------------------------------------------------------------
apply_change() {
  local fn_name="$1"
  local new_mb="$2"
  local old_mb="$3"
  local risk_score="$4"

  # Skip high-risk functions when SKIP_HIGH_RISK=true
  if [ "$SKIP_HIGH_RISK" = "true" ] && [ "$risk_score" -ge 4 ]; then
    log_warn "[$fn_name] Skipping — risk score $risk_score >= 4 and SKIP_HIGH_RISK=true."
    (( SKIPPED++ )) || true
    return 0
  fi

  if [ "$DRY_RUN" = "true" ]; then
    log_dry "[$fn_name] Would change: ${old_mb}MB → ${new_mb}MB (risk=$risk_score)"
    (( SKIPPED++ )) || true
    return 0
  fi

  # Per-function confirmation for high-risk changes
  if [ "$risk_score" -ge 4 ] && [ "$FORCE" != "true" ]; then
    log_warn "[$fn_name] Risk score is $risk_score — requires explicit confirmation."
    if ! confirm "  Apply change to $fn_name (${old_mb}MB → ${new_mb}MB)?"; then
      log_warn "[$fn_name] Skipped by user."
      (( SKIPPED++ )) || true
      return 0
    fi
  fi

  log_info "[$fn_name] Updating: ${old_mb}MB → ${new_mb}MB ..."

  # Apply the memory change via AWS CLI
  if aws lambda update-function-configuration \
    --region "$REGION" \
    --function-name "$fn_name" \
    --memory-size "$new_mb" \
    --output json > /dev/null 2>&1; then
    log_ok "[$fn_name] Updated successfully."
    (( UPDATED++ )) || true
  else
    log_error "[$fn_name] AWS CLI call failed. Function NOT updated."
    (( FAILED++ )) || true
  fi
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

# Verify AWS CLI is available
if ! command -v aws &> /dev/null; then
  log_error "AWS CLI not found. Install it from https://aws.amazon.com/cli/"
  exit 1
fi

# Verify AWS credentials are configured
if ! aws sts get-caller-identity --region "$REGION" > /dev/null 2>&1; then
  log_error "AWS credentials not configured or invalid for region $REGION."
  exit 1
fi

log_info "Pre-flight checks passed."
log_info "Region  : $REGION"
log_info "Dry Run : $DRY_RUN"
log_info "Force   : $FORCE"
log_info "Skip High Risk : $SKIP_HIGH_RISK"
echo ""

echo -e "${BOLD}Functions to be restored:${RESET}"
echo ""
echo "  rightsizer-test-128mb                              128MB → 128MB  (restore original)"
echo "  rightsizer-test-512mb                              128MB → 512MB  (restore original)"
echo "  rightsizer-test-1024mb                             128MB → 1024MB  (restore original)"
echo ""
# ---------------------------------------------------------------------------
# Global confirmation (skipped when FORCE=true or DRY_RUN=true)
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" != "true" ] && [ "$FORCE" != "true" ]; then
  if ! confirm "Proceed with the changes listed above?"; then
    log_warn "Aborted by user."
    exit 0
  fi
fi
echo ""

# -----------------------------------------------------------------------------
# Restore: rightsizer-test-128mb
#   Remediation set : 128 MB
#   Restoring to    : 128 MB  (original allocation)
# -----------------------------------------------------------------------------
apply_change "rightsizer-test-128mb" 128 128 1

# -----------------------------------------------------------------------------
# Restore: rightsizer-test-512mb
#   Remediation set : 128 MB
#   Restoring to    : 512 MB  (original allocation)
# -----------------------------------------------------------------------------
apply_change "rightsizer-test-512mb" 512 128 1

# -----------------------------------------------------------------------------
# Restore: rightsizer-test-1024mb
#   Remediation set : 128 MB
#   Restoring to    : 1024 MB  (original allocation)
# -----------------------------------------------------------------------------
apply_change "rightsizer-test-1024mb" 1024 128 1


echo ""
echo -e "${BOLD}=== Rollback complete ===${RESET}"
echo -e "  Restored : ${GREEN}$RESTORED${RESET}"
echo -e "  Skipped  : ${YELLOW}$SKIPPED${RESET}"
echo -e "  Failed   : ${RED}$FAILED${RESET}"
echo ""
if [ "$FAILED" -gt 0 ]; then
  log_error "Some restores failed. Review the output above."
  exit 1
fi
