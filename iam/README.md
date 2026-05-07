# IAM Policies

Two policies are provided following the principle of least privilege.
Attach only what each identity actually needs.

---

## Files

| File | Purpose |
|---|---|
| `lambda-rightsizer-read-only.json` | Analysis only — discovery, metrics, log queries |
| `lambda-rightsizer-remediation.json` | Additive — apply memory changes via AWS CLI |

Attach **read-only** to the identity that runs the tool.
Attach **remediation** only to the identity that executes the generated bash script,
and only when you are ready to apply changes.

---

## Read-Only Policy — Permission Breakdown

### `sts:GetCallerIdentity`
**Resource:** `*` (STS does not support resource-level restrictions for this action)

Called in `main.py` and `lambda_discovery.py` at startup to validate that
credentials are working and to resolve the AWS account ID for logging.
Without this the tool fails immediately with a clear auth error rather than
a confusing downstream failure.

---

### `lambda:ListFunctions`
**Resource:** `*` (Lambda list operations do not support resource-level ARN scoping)

Called by `lambda_discovery.py` via the boto3 paginator.
This is the core discovery action — it returns every function's name, ARN,
runtime, configured memory, timeout, and layer list.
Read-only; does not modify any function.

---

### `logs:StartQuery`
**Resource:** `arn:aws:logs:*:*:log-group:/aws/lambda/*`

Called by `metrics_analyzer.py` (strategy 1 — Logs Insights).
Submits a CloudWatch Logs Insights query against `/aws/lambda/<function-name>`
to extract `Max Memory Used` values from REPORT log lines.
Scoped to Lambda log groups only — cannot query any other log group.

---

### `logs:GetQueryResults`
**Resource:** `arn:aws:logs:*:*:log-group:/aws/lambda/*`

Called by `metrics_analyzer.py` to poll the Logs Insights query started
by `StartQuery` until it reaches `Complete` status.
Must be paired with `StartQuery` — one without the other is useless.
Scoped to Lambda log groups only.

---

### `logs:StopQuery`
**Resource:** `arn:aws:logs:*:*:log-group:/aws/lambda/*`

Allows cancellation of a running Logs Insights query if the tool is
interrupted mid-run. Prevents orphaned queries from consuming Logs Insights
quota. Scoped to Lambda log groups only.

---

### `logs:FilterLogEvents`
**Resource:**
- `arn:aws:logs:*:*:log-group:/aws/lambda/*`
- `arn:aws:logs:*:*:log-group:/aws/lambda/*:log-stream:*`

Called by `metrics_analyzer.py` (strategy 2 — log filter fallback).
Used when Logs Insights returns fewer samples than `MIN_INVOCATIONS`.
Fetches raw REPORT log events and parses `Max Memory Used` locally via regex.
Both the log group and log stream ARN patterns are required because
`FilterLogEvents` checks permissions at both levels.
Scoped to Lambda log groups and their streams only.

---

### `logs:DescribeLogGroups`
**Resource:** `arn:aws:logs:*:*:log-group:/aws/lambda/*`

Used to check whether a log group exists before attempting queries,
avoiding noisy `ResourceNotFoundException` errors for functions that
have never been invoked or have log retention expired.
Scoped to Lambda log groups only.

---

### `cloudwatch:GetMetricStatistics`
**Resource:** `*` (CloudWatch metrics do not support resource-level restrictions)

Called by `metrics_analyzer.py` (strategy 3 — CloudWatch Metrics fallback).
Retrieves the `MaxMemoryUsed` metric from the `AWS/Lambda` namespace when
both log strategies return insufficient data. Returns daily aggregated
Maximum/Average/SampleCount datapoints.
Read-only; does not modify any metric or alarm.

---

### `cloudwatch:ListMetrics`
**Resource:** `*` (CloudWatch metrics do not support resource-level restrictions)

Used to verify that the `MaxMemoryUsed` metric exists for a given function
before calling `GetMetricStatistics`, avoiding unnecessary API calls for
functions with no metric history.
Read-only.

---

## Remediation Policy — Permission Breakdown

### `lambda:UpdateFunctionConfiguration`
**Resource:** `arn:aws:lambda:*:*:function:*`

Called by the generated `remediation_<ts>.sh` bash script via the AWS CLI.
Updates the `MemorySize` field on a Lambda function to the recommended value.

**This is the only write permission in the entire project.**

It is intentionally in a separate policy so it can be:
- Attached to a different, more restricted IAM role
- Granted only at remediation time and revoked afterwards
- Scoped further to specific functions by replacing `*` with explicit ARNs:

```json
"Resource": [
  "arn:aws:lambda:us-east-1:123456789012:function:my-function-a",
  "arn:aws:lambda:us-east-1:123456789012:function:my-function-b"
]
```

`UpdateFunctionConfiguration` cannot deploy code, change IAM roles,
add environment variables, or modify VPC settings when called with
only `--memory-size`. It is the narrowest possible write action for
this use case.

---

## Recommended Attachment Strategy

```
Analysis identity (CI runner, developer role, EC2 instance profile)
  └── lambda-rightsizer-read-only   [always attached]

Remediation identity (separate role, assumed only when applying changes)
  └── lambda-rightsizer-read-only   [needed to re-verify before applying]
  └── lambda-rightsizer-remediation [attached only at remediation time]
```

For maximum safety, scope the remediation policy to a specific region
and account by replacing the wildcard ARN:

```json
"Resource": "arn:aws:lambda:us-east-1:123456789012:function:*"
```

---

## Permissions NOT Required

The following are explicitly excluded and should never be granted:

| Permission | Why excluded |
|---|---|
| `lambda:GetFunction` | Not called — `ListFunctions` returns all needed metadata |
| `lambda:InvokeFunction` | Never invoked — analysis is passive |
| `logs:CreateLogGroup` / `PutLogEvents` | Tool does not write to CloudWatch Logs |
| `cloudwatch:PutMetricData` | Tool does not publish metrics |
| `iam:*` | No IAM operations performed |
| `s3:*` | Output is written locally, not to S3 |
