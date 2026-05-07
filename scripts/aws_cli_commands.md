# AWS CLI Commands — Test Environment Setup

Step-by-step AWS CLI commands to create three sample Lambda functions,
invoke them, verify CloudWatch logs, and run Lambda Rightsizer against them.

All commands use `$REGION` and `$ACCOUNT_ID` as placeholders.
Set them once at the top of your shell session:

```bash
export REGION="us-east-1"
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export PROFILE="default"   # change to your AWS CLI profile name
```

---

## 1. Verify AWS credentials

```bash
aws sts get-caller-identity \
  --profile $PROFILE \
  --region $REGION
```

Expected output:
```json
{
    "UserId": "AIDAXXXXXXXXXXXXXXXXX",
    "Account": "123456789012",
    "Arn": "arn:aws:iam::123456789012:user/your-user"
}
```

---

## 2. Create IAM execution role

Lambda functions need an IAM role to write CloudWatch logs.

### 2a. Write the trust policy

```bash
cat > /tmp/lambda-trust-policy.json <<'EOF'
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
```

### 2b. Create the role

```bash
aws iam create-role \
  --profile $PROFILE \
  --role-name lambda-rightsizer-test-role \
  --assume-role-policy-document file:///tmp/lambda-trust-policy.json \
  --description "Execution role for Lambda Rightsizer test functions"
```

### 2c. Attach the basic execution policy (CloudWatch Logs write access)

```bash
aws iam attach-role-policy \
  --profile $PROFILE \
  --role-name lambda-rightsizer-test-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

### 2d. Wait for IAM propagation

```bash
# IAM changes take a few seconds to propagate globally.
# Lambda CreateFunction will fail with InvalidParameterValueException
# if you proceed too quickly.
sleep 10
```

---

## 3. Create the Lambda deployment package

### 3a. Write the function code

```bash
cat > /tmp/index.py <<'EOF'
"""
Lambda Rightsizer test function.

Uses ~30-50 MB of memory regardless of configured limit.
512 MB and 1024 MB configurations will appear over-provisioned
when analysed by Lambda Rightsizer.
"""
import json
import os
import time


def handler(event, context):
    # Small, predictable workload
    data = list(range(1_000))
    result = sum(x * x for x in data)

    # 50ms sleep produces a non-trivial duration in REPORT lines
    time.sleep(0.05)

    print(
        f"[{context.function_name}] "
        f"request={context.aws_request_id} "
        f"configured={context.memory_limit_in_mb}MB "
        f"result={result}"
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "function":      context.function_name,
            "configured_mb": context.memory_limit_in_mb,
            "result":        result,
        }),
    }
EOF
```

### 3b. Package into a zip file

```bash
cd /tmp && zip function.zip index.py
```

---

## 4. Create the three Lambda functions

### 4a. 128 MB function

```bash
aws lambda create-function \
  --profile $PROFILE \
  --region $REGION \
  --function-name rightsizer-test-128mb \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/lambda-rightsizer-test-role \
  --handler index.handler \
  --zip-file fileb:///tmp/function.zip \
  --memory-size 128 \
  --timeout 30 \
  --description "Lambda Rightsizer test — 128MB (expected: optimal)"
```

### 4b. 512 MB function

```bash
aws lambda create-function \
  --profile $PROFILE \
  --region $REGION \
  --function-name rightsizer-test-512mb \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/lambda-rightsizer-test-role \
  --handler index.handler \
  --zip-file fileb:///tmp/function.zip \
  --memory-size 512 \
  --timeout 30 \
  --description "Lambda Rightsizer test — 512MB (expected: over_provisioned)"
```

### 4c. 1024 MB function

```bash
aws lambda create-function \
  --profile $PROFILE \
  --region $REGION \
  --function-name rightsizer-test-1024mb \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/lambda-rightsizer-test-role \
  --handler index.handler \
  --zip-file fileb:///tmp/function.zip \
  --memory-size 1024 \
  --timeout 30 \
  --description "Lambda Rightsizer test — 1024MB (expected: over_provisioned)"
```

### 4d. Wait for all three functions to become Active

```bash
for FN in rightsizer-test-128mb rightsizer-test-512mb rightsizer-test-1024mb; do
  echo "Waiting for $FN ..."
  aws lambda wait function-active \
    --profile $PROFILE \
    --region $REGION \
    --function-name $FN
  echo "  $FN is Active"
done
```

### 4e. Verify all three functions exist

```bash
aws lambda list-functions \
  --profile $PROFILE \
  --region $REGION \
  --query "Functions[?starts_with(FunctionName,'rightsizer-test')].[FunctionName,MemorySize,Runtime]" \
  --output table
```

Expected output:
```
--------------------------------------------------------------
|                       ListFunctions                        |
+---------------------------+--------+--------------------+
|  rightsizer-test-128mb    |  128   |  python3.12        |
|  rightsizer-test-512mb    |  512   |  python3.12        |
|  rightsizer-test-1024mb   |  1024  |  python3.12        |
+---------------------------+--------+--------------------+
```

---

## 5. Invoke each function 10 times

### 5a. Invoke rightsizer-test-128mb × 10

```bash
for i in $(seq 1 10); do
  aws lambda invoke \
    --profile $PROFILE \
    --region $REGION \
    --function-name rightsizer-test-128mb \
    --invocation-type RequestResponse \
    --payload '{"source":"rightsizer-test"}' \
    --cli-binary-format raw-in-base64-out \
    --log-type None \
    /tmp/response-128.json \
    --query 'StatusCode' \
    --output text
  sleep 0.2
done
```

### 5b. Invoke rightsizer-test-512mb × 10

```bash
for i in $(seq 1 10); do
  aws lambda invoke \
    --profile $PROFILE \
    --region $REGION \
    --function-name rightsizer-test-512mb \
    --invocation-type RequestResponse \
    --payload '{"source":"rightsizer-test"}' \
    --cli-binary-format raw-in-base64-out \
    --log-type None \
    /tmp/response-512.json \
    --query 'StatusCode' \
    --output text
  sleep 0.2
done
```

### 5c. Invoke rightsizer-test-1024mb × 10

```bash
for i in $(seq 1 10); do
  aws lambda invoke \
    --profile $PROFILE \
    --region $REGION \
    --function-name rightsizer-test-1024mb \
    --invocation-type RequestResponse \
    --payload '{"source":"rightsizer-test"}' \
    --cli-binary-format raw-in-base64-out \
    --log-type None \
    /tmp/response-1024.json \
    --query 'StatusCode' \
    --output text
  sleep 0.2
done
```

Each invocation prints `200` on success.

---

## 6. Verify CloudWatch logs

Wait ~15 seconds after the last invocation for logs to propagate.

```bash
sleep 15
```

### 6a. Confirm log groups exist

```bash
aws logs describe-log-groups \
  --profile $PROFILE \
  --region $REGION \
  --log-group-name-prefix "/aws/lambda/rightsizer-test" \
  --query "logGroups[].logGroupName" \
  --output table
```

Expected output:
```
----------------------------------------------
|           DescribeLogGroups                |
+--------------------------------------------+
|  /aws/lambda/rightsizer-test-128mb         |
|  /aws/lambda/rightsizer-test-512mb         |
|  /aws/lambda/rightsizer-test-1024mb        |
+--------------------------------------------+
```

### 6b. Count REPORT lines per function

```bash
for FN in rightsizer-test-128mb rightsizer-test-512mb rightsizer-test-1024mb; do
  COUNT=$(aws logs filter-log-events \
    --profile $PROFILE \
    --region $REGION \
    --log-group-name "/aws/lambda/$FN" \
    --filter-pattern "REPORT RequestId" \
    --query "length(events)" \
    --output text 2>/dev/null || echo "0")
  echo "$FN — $COUNT REPORT line(s)"
done
```

Expected output:
```
rightsizer-test-128mb  — 10 REPORT line(s)
rightsizer-test-512mb  — 10 REPORT line(s)
rightsizer-test-1024mb — 10 REPORT line(s)
```

### 6c. Inspect a sample REPORT line (shows Max Memory Used)

```bash
aws logs filter-log-events \
  --profile $PROFILE \
  --region $REGION \
  --log-group-name "/aws/lambda/rightsizer-test-1024mb" \
  --filter-pattern "REPORT RequestId" \
  --max-items 1 \
  --query "events[0].message" \
  --output text
```

Example output:
```
REPORT RequestId: abc-123  Duration: 55.21 ms  Billed Duration: 56 ms
Memory Size: 1024 MB  Max Memory Used: 38 MB  Init Duration: 142.30 ms
```

The `Max Memory Used: 38 MB` against `Memory Size: 1024 MB` is exactly the
over-provisioning signal Lambda Rightsizer is designed to detect.

### 6d. Run a Logs Insights query manually (optional)

```bash
# Start the query
QUERY_ID=$(aws logs start-query \
  --profile $PROFILE \
  --region $REGION \
  --log-group-name "/aws/lambda/rightsizer-test-1024mb" \
  --start-time $(date -d '1 hour ago' +%s 2>/dev/null || date -v-1H +%s) \
  --end-time $(date +%s) \
  --query-string 'filter @type = "REPORT" | parse @message "Max Memory Used: * MB" as max_used_mb | stats max(max_used_mb) as peak_mb, avg(max_used_mb) as avg_mb, count(*) as invocations' \
  --query 'queryId' \
  --output text)

echo "Query ID: $QUERY_ID"

# Wait a few seconds then fetch results
sleep 5

aws logs get-query-results \
  --profile $PROFILE \
  --region $REGION \
  --query-id "$QUERY_ID"
```

---

## 7. Run Lambda Rightsizer

```bash
cd lambda-rightsizer

# Targeted scan — only the three test functions, 1-day lookback
python -m lambda_rightsizer.main \
  --region $REGION \
  --profile $PROFILE \
  --days 1 \
  --filter rightsizer-test

# Dry-run mode — analysis only, no files written
python -m lambda_rightsizer.main \
  --region $REGION \
  --profile $PROFILE \
  --days 1 \
  --filter rightsizer-test \
  --dry-run
```

### Expected results

| Function | Configured | Expected Max Used | Expected Status |
|---|---|---|---|
| `rightsizer-test-128mb` | 128 MB | ~38 MB | `optimal` or `over_provisioned` |
| `rightsizer-test-512mb` | 512 MB | ~38 MB | `over_provisioned` |
| `rightsizer-test-1024mb` | 1024 MB | ~38 MB | `over_provisioned` |

The 512 MB and 1024 MB functions will show ~96% waste and be flagged for reduction.

---

## 8. Teardown

Remove all resources created by this guide.

### Delete Lambda functions

```bash
for FN in rightsizer-test-128mb rightsizer-test-512mb rightsizer-test-1024mb; do
  aws lambda delete-function \
    --profile $PROFILE \
    --region $REGION \
    --function-name $FN
  echo "Deleted $FN"
done
```

### Delete IAM role

```bash
# Detach policy first (required before role deletion)
aws iam detach-role-policy \
  --profile $PROFILE \
  --role-name lambda-rightsizer-test-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam delete-role \
  --profile $PROFILE \
  --role-name lambda-rightsizer-test-role

echo "Deleted role lambda-rightsizer-test-role"
```

### Delete CloudWatch log groups (optional)

Lambda deletes log groups automatically when functions are deleted,
but if they persist:

```bash
for FN in rightsizer-test-128mb rightsizer-test-512mb rightsizer-test-1024mb; do
  aws logs delete-log-group \
    --profile $PROFILE \
    --region $REGION \
    --log-group-name "/aws/lambda/$FN" 2>/dev/null \
    && echo "Deleted /aws/lambda/$FN" \
    || echo "/aws/lambda/$FN already gone"
done
```

---

## Permissions required to run this guide

| Action | Required for |
|---|---|
| `sts:GetCallerIdentity` | Credential validation |
| `iam:CreateRole` | Step 2b |
| `iam:AttachRolePolicy` | Step 2c |
| `iam:GetRole` | Idempotency check |
| `iam:DetachRolePolicy` | Teardown |
| `iam:DeleteRole` | Teardown |
| `iam:PassRole` | Lambda CreateFunction |
| `lambda:CreateFunction` | Step 4 |
| `lambda:UpdateFunctionCode` | Step 4 (if re-running) |
| `lambda:UpdateFunctionConfiguration` | Step 4 (if re-running) |
| `lambda:GetFunction` | Idempotency check |
| `lambda:ListFunctions` | Step 4e verification |
| `lambda:InvokeFunction` | Step 5 |
| `lambda:DeleteFunction` | Teardown |
| `logs:DescribeLogGroups` | Step 6a |
| `logs:FilterLogEvents` | Step 6b, 6c |
| `logs:StartQuery` | Step 6d |
| `logs:GetQueryResults` | Step 6d |
| `logs:DeleteLogGroup` | Teardown (optional) |
