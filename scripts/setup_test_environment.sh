#!/usr/bin/env bash
# =============================================================================
# setup_test_environment.sh
#
# Creates three sample Lambda functions with different memory configurations,
# invokes each 10 times, and verifies CloudWatch logs are populated —
# preparing the account for a Lambda Rightsizer test run.
#
# USAGE:
#   bash scripts/setup_test_environment.sh
#   AWS_REGION=eu-west-1 bash scripts/setup_test_environment.sh
#   SKIP_TEARDOWN=true bash scripts/setup_test_environment.sh
#
# TEARDOWN (removes everything created by this script):
#   bash scripts/setup_test_environment.sh --teardown
#
# REQUIREMENTS:
#   - AWS CLI v2 installed and configured
#   - Permissions: iam:CreateRole, iam:AttachRolePolicy, iam:PassRole,
#                  lambda:CreateFunction, lambda:InvokeFunction,
#                  lambda:DeleteFunction, lambda:GetFunction,
#                  logs:DescribeLogGroups, logs:FilterLogEvents
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-personal}"
RUNTIME="python3.12"
HANDLER="index.handler"

# IAM role name created for the test functions
ROLE_NAME="lambda-rightsizer-test-role"

# Function names — intentionally descriptive so --filter works in the tool
FN_128="rightsizer-test-128mb"
FN_512="rightsizer-test-512mb"
FN_1024="rightsizer-test-1024mb"

# How many times to invoke each function
INVOCATION_COUNT=10

# Seconds to wait after invocations before checking logs
LOG_WAIT_SECONDS=15

# Temp directory for generated artefacts (cleaned up on exit)
WORK_DIR="$(mktemp -d)"

# ---------------------------------------------------------------------------
# Colours
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
log_step()  { echo -e "\n${BOLD}${CYAN}==> $*${RESET}"; }

# ---------------------------------------------------------------------------
# Cleanup on exit
# ---------------------------------------------------------------------------

cleanup_workdir() {
  rm -rf "$WORK_DIR"
}
trap cleanup_workdir EXIT

# ---------------------------------------------------------------------------
# Teardown mode — remove everything this script created
# ---------------------------------------------------------------------------

teardown() {
  log_step "Tearing down test environment"

  for FN in "$FN_128" "$FN_512" "$FN_1024"; do
    log_info "Deleting function: $FN"
    aws lambda delete-function \
      --region "$REGION" \
      --profile "$PROFILE" \
      --function-name "$FN" 2>/dev/null \
      && log_ok "Deleted $FN" \
      || log_warn "$FN not found — skipping"
  done

  # Detach managed policy before deleting role
  log_info "Detaching policy from role: $ROLE_NAME"
  aws iam detach-role-policy \
    --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" \
    2>/dev/null || true

  log_info "Deleting IAM role: $ROLE_NAME"
  aws iam delete-role \
    --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    2>/dev/null \
    && log_ok "Deleted role $ROLE_NAME" \
    || log_warn "Role $ROLE_NAME not found — skipping"

  log_ok "Teardown complete."
  exit 0
}

if [[ "${1:-}" == "--teardown" ]]; then
  teardown
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

log_step "Pre-flight checks"

if ! command -v aws &>/dev/null; then
  log_error "AWS CLI not found. Install from https://aws.amazon.com/cli/"
  exit 1
fi

if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
  log_error "Python not found. Required to create the Lambda deployment package."
  exit 1
fi

if ! aws sts get-caller-identity \
     --region "$REGION" \
     --profile "$PROFILE" \
     --output text > /dev/null 2>&1; then
  log_error "AWS credentials not valid for profile '$PROFILE' in region '$REGION'."
  exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query Account \
  --output text)

log_ok "Authenticated | account=$ACCOUNT_ID | region=$REGION | profile=$PROFILE"

# ---------------------------------------------------------------------------
# Step 1 — Create IAM execution role
# ---------------------------------------------------------------------------

log_step "Step 1/5 — IAM execution role"

# Trust policy allowing Lambda to assume this role
cat > "$WORK_DIR/trust-policy.json" <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# Check if role already exists
if aws iam get-role \
     --profile "$PROFILE" \
     --role-name "$ROLE_NAME" \
     --output text > /dev/null 2>&1; then
  log_warn "Role '$ROLE_NAME' already exists — reusing."
else
  log_info "Creating IAM role: $ROLE_NAME"
  aws iam create-role \
    --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$WORK_DIR/trust-policy.json" \
    --description "Execution role for Lambda Rightsizer test functions" \
    --output text > /dev/null

  # Attach the AWS-managed basic execution policy (CloudWatch Logs write access)
  log_info "Attaching AWSLambdaBasicExecutionRole policy"
  aws iam attach-role-policy \
    --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

  # IAM role propagation — Lambda CreateFunction will fail if the role
  # isn't fully consistent yet. 10 seconds is sufficient in most regions.
  log_info "Waiting 10s for IAM role to propagate ..."
  sleep 10
fi

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
log_ok "Role ARN: $ROLE_ARN"

# ---------------------------------------------------------------------------
# Step 2 — Build Lambda deployment package
# ---------------------------------------------------------------------------

log_step "Step 2/5 — Build deployment package"

# The function intentionally allocates a small amount of memory (a list of
# 1000 integers) so that actual memory usage stays well below the configured
# limit — making the 512 MB and 1024 MB functions clearly over-provisioned
# and ideal test subjects for Lambda Rightsizer.

cat > "$WORK_DIR/index.py" <<'PYEOF'
"""
Lambda Rightsizer — test function.

Designed to use a predictable, low amount of memory (~30-50 MB) regardless
of the configured memory limit, so that 512 MB and 1024 MB configurations
are clearly over-provisioned when analysed by Lambda Rightsizer.
"""

import json
import os
import time


def handler(event, context):
    # Simulate a small, realistic workload
    data = list(range(1_000))
    result = sum(x * x for x in data)

    # Short sleep to produce a non-trivial duration in REPORT lines
    time.sleep(0.05)

    configured_mb = context.memory_limit_in_mb
    fn_name       = context.function_name
    request_id    = context.aws_request_id

    print(f"[{fn_name}] request={request_id} configured={configured_mb}MB result={result}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "function":      fn_name,
            "configured_mb": configured_mb,
            "result":        result,
        }),
    }
PYEOF

# Package into a zip file
ZIP_PATH="$WORK_DIR/function.zip"

if command -v zip &>/dev/null; then
  (cd "$WORK_DIR" && zip -q "$ZIP_PATH" index.py)
else
  # Fallback: use Python's zipfile module (available everywhere Python is)
  python3 -c "
import zipfile, os
with zipfile.ZipFile('$ZIP_PATH', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write('$WORK_DIR/index.py', 'index.py')
"
fi

log_ok "Deployment package: $ZIP_PATH ($(du -h "$ZIP_PATH" | cut -f1))"

# ---------------------------------------------------------------------------
# Step 3 — Create Lambda functions
# ---------------------------------------------------------------------------

log_step "Step 3/5 — Create Lambda functions"

create_function() {
  local fn_name="$1"
  local memory_mb="$2"

  # Check if function already exists
  if aws lambda get-function \
       --region "$REGION" \
       --profile "$PROFILE" \
       --function-name "$fn_name" \
       --output text > /dev/null 2>&1; then
    log_warn "Function '$fn_name' already exists — updating code and config."

    aws lambda update-function-code \
      --region "$REGION" \
      --profile "$PROFILE" \
      --function-name "$fn_name" \
      --zip-file "fileb://$ZIP_PATH" \
      --output text > /dev/null

    aws lambda update-function-configuration \
      --region "$REGION" \
      --profile "$PROFILE" \
      --function-name "$fn_name" \
      --memory-size "$memory_mb" \
      --output text > /dev/null
  else
    log_info "Creating function: $fn_name (${memory_mb}MB)"

    aws lambda create-function \
      --region "$REGION" \
      --profile "$PROFILE" \
      --function-name "$fn_name" \
      --runtime "$RUNTIME" \
      --role "$ROLE_ARN" \
      --handler "$HANDLER" \
      --zip-file "fileb://$ZIP_PATH" \
      --memory-size "$memory_mb" \
      --timeout 30 \
      --description "Lambda Rightsizer test function — ${memory_mb}MB configuration" \
      --output text > /dev/null
  fi

  # Wait until the function is Active before proceeding
  log_info "Waiting for $fn_name to become Active ..."
  aws lambda wait function-active \
    --region "$REGION" \
    --profile "$PROFILE" \
    --function-name "$fn_name"

  log_ok "Created: $fn_name | memory=${memory_mb}MB | runtime=$RUNTIME"
}

create_function "$FN_128"  128
create_function "$FN_512"  512
create_function "$FN_1024" 1024

# ---------------------------------------------------------------------------
# Step 4 — Invoke each function 10 times
# ---------------------------------------------------------------------------

log_step "Step 4/5 — Invoke functions ($INVOCATION_COUNT times each)"

invoke_function() {
  local fn_name="$1"
  local count="$2"
  local response_file="$WORK_DIR/response.json"

  log_info "Invoking $fn_name × $count ..."

  local success=0
  local failure=0

  for i in $(seq 1 "$count"); do
    # Synchronous invocation — captures the response payload
    HTTP_STATUS=$(aws lambda invoke \
      --region "$REGION" \
      --profile "$PROFILE" \
      --function-name "$fn_name" \
      --invocation-type RequestResponse \
      --payload '{"source":"rightsizer-test"}' \
      --cli-binary-format raw-in-base64-out \
      --log-type None \
      --output json \
      "$response_file" \
      --query 'StatusCode' \
      --output text 2>/dev/null)

    if [[ "$HTTP_STATUS" == "200" ]]; then
      (( success++ )) || true
    else
      (( failure++ )) || true
      log_warn "  Invocation $i returned HTTP $HTTP_STATUS"
    fi

    # Small delay between invocations to spread log timestamps
    sleep 0.2
  done

  log_ok "$fn_name — success=$success failure=$failure"
}

invoke_function "$FN_128"  "$INVOCATION_COUNT"
invoke_function "$FN_512"  "$INVOCATION_COUNT"
invoke_function "$FN_1024" "$INVOCATION_COUNT"

# ---------------------------------------------------------------------------
# Step 5 — Verify CloudWatch logs
# ---------------------------------------------------------------------------

log_step "Step 5/5 — Verify CloudWatch logs"

log_info "Waiting ${LOG_WAIT_SECONDS}s for logs to propagate to CloudWatch ..."
sleep "$LOG_WAIT_SECONDS"

verify_logs() {
  local fn_name="$1"
  local log_group="/aws/lambda/$fn_name"

  # Check the log group exists
  if ! aws logs describe-log-groups \
       --region "$REGION" \
       --profile "$PROFILE" \
       --log-group-name-prefix "$log_group" \
       --query "logGroups[?logGroupName=='$log_group'].logGroupName" \
       --output text | grep -q "$fn_name"; then
    log_warn "$fn_name — log group '$log_group' not found yet. Logs may still be propagating."
    return
  fi

  # Count REPORT lines (one per invocation)
  REPORT_COUNT=$(aws logs filter-log-events \
    --region "$REGION" \
    --profile "$PROFILE" \
    --log-group-name "$log_group" \
    --filter-pattern "REPORT RequestId" \
    --query "length(events)" \
    --output text 2>/dev/null || echo "0")

  # Fetch one sample REPORT line to show Max Memory Used
  SAMPLE_REPORT=$(aws logs filter-log-events \
    --region "$REGION" \
    --profile "$PROFILE" \
    --log-group-name "$log_group" \
    --filter-pattern "REPORT RequestId" \
    --max-items 1 \
    --query "events[0].message" \
    --output text 2>/dev/null || echo "")

  if [[ "$REPORT_COUNT" -ge 1 ]]; then
    log_ok "$fn_name — $REPORT_COUNT REPORT line(s) found in CloudWatch"
    if [[ -n "$SAMPLE_REPORT" && "$SAMPLE_REPORT" != "None" ]]; then
      echo "         Sample: $SAMPLE_REPORT" | tr -d '\n'
      echo ""
    fi
  else
    log_warn "$fn_name — no REPORT lines found yet (logs may still be propagating)"
  fi
}

verify_logs "$FN_128"
verify_logs "$FN_512"
verify_logs "$FN_1024"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${BOLD}${CYAN}============================================================${RESET}"
echo -e "${BOLD}  Test environment ready${RESET}"
echo -e "${CYAN}============================================================${RESET}"
echo ""
echo -e "  Region   : $REGION"
echo -e "  Account  : $ACCOUNT_ID"
echo ""
echo -e "  Functions created:"
echo -e "    ${YELLOW}$FN_128${RESET}   — 128 MB  (expected: optimal / watch)"
echo -e "    ${YELLOW}$FN_512${RESET}   — 512 MB  (expected: over_provisioned)"
echo -e "    ${YELLOW}$FN_1024${RESET}  — 1024 MB (expected: over_provisioned)"
echo ""
echo -e "  Each function was invoked ${INVOCATION_COUNT} times."
echo -e "  CloudWatch REPORT lines should be visible in:"
echo -e "    /aws/lambda/$FN_128"
echo -e "    /aws/lambda/$FN_512"
echo -e "    /aws/lambda/$FN_1024"
echo ""
echo -e "${BOLD}  Run Lambda Rightsizer:${RESET}"
echo -e "    cd lambda-rightsizer"
echo -e "    python -m lambda_rightsizer.main \\"
echo -e "      --region $REGION \\"
echo -e "      --profile $PROFILE \\"
echo -e "      --days 1 \\"
echo -e "      --filter rightsizer-test"
echo ""
echo -e "${BOLD}  Teardown when done:${RESET}"
echo -e "    bash scripts/setup_test_environment.sh --teardown"
echo ""
echo -e "${CYAN}============================================================${RESET}"
