#!/usr/bin/env bash
# =============================================================================
# Lambda Rightsizer — Remediation Script
# Generated  : 2026-05-05 14:20:57 UTC
# Region     : us-east-1
# Targeted   : 3 function(s)
#
# USAGE:
#   bash remediation.sh                  # interactive, confirms each change
#   DRY_RUN=true bash remediation.sh      # preview only, no AWS calls
#   FORCE=true bash remediation.sh        # skip confirmations (use with care)
#   SKIP_HIGH_RISK=true bash remediation.sh  # skip risk>=4 functions
#
# SAFETY:
#   A config backup was written to: backup_20260505T142057Z.json
#   Run rollback.sh to restore all functions to their original memory.
#   Each AWS CLI call is idempotent — safe to re-run.
# =============================================================================

set -euo pipefail

REGION="us-east-1"
RUN_DIR="./output/20260505T142057Z"
BACKUP_FILE="./output/20260505T142057Z/backup_20260505T142057Z.json"

# Runtime flags (override via environment variables)
DRY_RUN="${DRY_RUN:-false}"
FORCE="${FORCE:-false}"
SKIP_HIGH_RISK="${SKIP_HIGH_RISK:-false}"

# Counters
UPDATED=0
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

# ---------------------------------------------------------------------------
# Change summary
# ---------------------------------------------------------------------------
echo -e "${BOLD}Functions to be modified:${RESET}"
echo ""
echo "  rightsizer-test-128mb                              128MB → 128MB  (↓ reduce)  risk=medium"
echo "  rightsizer-test-512mb                              512MB → 128MB  (↓ reduce)  risk=high"
echo "  rightsizer-test-1024mb                             1024MB → 128MB  (↓ reduce)  risk=high"
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
# Function   : rightsizer-test-128mb
# ARN        : arn:aws:lambda:us-east-1:975050196195:function:rightsizer-test-128mb
# Action     : INCREASE  128MB → 128MB  (Δ +0MB)
# Status     : over_provisioned
# Risk       : medium (score=3)
# Peak used  : 36 MB   Avg: 36 MB   P95: 36 MB
# Waste      : 71.9%
# Invocations: 10   Data source: logs_filter
# Recommendation: Reduce memory from 128 MB to 128 MB. Peak usage was 36 MB (avg 36 MB), utilization 28.1% — 71.9% waste. Estimated saving: 0 MB per invocation. Risk: medium.
# -----------------------------------------------------------------------------
apply_change "rightsizer-test-128mb" 128 128 3

# -----------------------------------------------------------------------------
# Function   : rightsizer-test-512mb
# ARN        : arn:aws:lambda:us-east-1:975050196195:function:rightsizer-test-512mb
# Action     : REDUCE  512MB → 128MB  (Δ -384MB)
# Status     : over_provisioned
# Risk       : high (score=4)
# Peak used  : 36 MB   Avg: 36 MB   P95: 36 MB
# Waste      : 93.0%
# Invocations: 10   Data source: logs_filter
# Recommendation: Reduce memory from 512 MB to 128 MB. Peak usage was 36 MB (avg 36 MB), utilization 7.0% — 93.0% waste. Estimated saving: 384 MB per invocation. Risk: high.
# -----------------------------------------------------------------------------
apply_change "rightsizer-test-512mb" 128 512 4

# -----------------------------------------------------------------------------
# Function   : rightsizer-test-1024mb
# ARN        : arn:aws:lambda:us-east-1:975050196195:function:rightsizer-test-1024mb
# Action     : REDUCE  1024MB → 128MB  (Δ -896MB)
# Status     : over_provisioned
# Risk       : high (score=4)
# Peak used  : 36 MB   Avg: 36 MB   P95: 36 MB
# Waste      : 96.5%
# Invocations: 10   Data source: logs_filter
# Recommendation: Reduce memory from 1024 MB to 128 MB. Peak usage was 36 MB (avg 36 MB), utilization 3.5% — 96.5% waste. Estimated saving: 896 MB per invocation. Risk: high.
# -----------------------------------------------------------------------------
apply_change "rightsizer-test-1024mb" 128 1024 4


# ---------------------------------------------------------------------------
# Functions NOT modified (no action required)
# ---------------------------------------------------------------------------
#
# api-key-usage-demo-put-item-rsoft                  skipped — not enough invocation data
# serverless-gdpr-agent-GDPRAgentFunction-BkNr8YU50Jn4 skipped — not enough invocation data
# api-key-usage-demo-get-by-id-rsoft                 skipped — not enough invocation data
# rag-retrieval-lambda                               skipped — not enough invocation data
# llmops-text-extractor                              skipped — not enough invocation data
# api-key-usage-demo-get-all-items-rsoft             skipped — not enough invocation data
#

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}=== Remediation complete ===${RESET}"
echo -e "  Updated : ${GREEN}$UPDATED${RESET}"
echo -e "  Skipped : ${YELLOW}$SKIPPED${RESET}"
echo -e "  Failed  : ${RED}$FAILED${RESET}"
echo ""
if [ "$FAILED" -gt 0 ]; then
  log_error "Some updates failed. Review the output above."
  exit 1
fi
