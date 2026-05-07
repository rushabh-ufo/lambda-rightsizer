# Lambda Rightsizer

> Automated AWS Lambda memory optimization — discover waste, quantify it, and fix it safely.

Lambda Rightsizer scans every Lambda function in an AWS account, pulls real memory
usage from CloudWatch, calculates over- and under-provisioning, and produces
actionable reports alongside a ready-to-run remediation script with a built-in
rollback path.

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Features](#features)
3. [Architecture](#architecture)
4. [Project Structure](#project-structure)
5. [AWS Prerequisites](#aws-prerequisites)
6. [IAM Setup](#iam-setup)
7. [Installation](#installation)
8. [Configuration](#configuration)
9. [Usage](#usage)
10. [Sample Output](#sample-output)
11. [Output Files](#output-files)
12. [Safety Considerations](#safety-considerations)
13. [Rollback Process](#rollback-process)
14. [Future Enhancements](#future-enhancements)

---

## Problem Statement

AWS Lambda pricing is based on two dimensions: **number of invocations** and
**GB-seconds** (memory × duration). Memory is the only dimension you control
directly — and it is routinely over-provisioned.

Common patterns that lead to waste:

- Functions provisioned at 512 MB or 1024 MB "just to be safe" that consistently
  use fewer than 100 MB
- Default memory settings (128 MB) left in place on functions that have grown
  and now run close to their limit
- No systematic process to review allocations after initial deployment

Without tooling, identifying these functions across a large account requires
manually querying CloudWatch for each function — impractical at scale.

Lambda Rightsizer automates the entire process: discovery, analysis, recommendation,
and remediation, with safety controls at every step.

---

## Features

### Analysis
- Scans all Lambda functions in a region via paginated `ListFunctions`
- Three-strategy memory data collection, tried in order:
  1. **CloudWatch Logs Insights** — server-side aggregation of REPORT log lines (fastest)
  2. **CloudWatch Logs filter** — raw REPORT line parsing when Insights has insufficient samples
  3. **CloudWatch Metrics** — `MaxMemoryUsed` metric as a last resort
- Computes per-function: peak, average, minimum, and P95 memory usage
- Calculates utilization percentage (avg used / allocated × 100)
- Configurable lookback window (default: 14 days)

### Optimization
- Four-band utilization model:

  | Utilization | Status | Action |
  |---|---|---|
  | < 30% | `over_provisioned` | Reduce to safety floor |
  | 30–70% | `optimal` | No change |
  | 70–80% | `watch` | Monitor — approaching threshold |
  | > 80% | `under_provisioned` | Increase above safety floor |

- Safety floor: `ceil(peak_mb × 1.20 / 64) × 64` — always 20% headroom above observed peak, rounded to the nearest 64 MB AWS step
- Risk scoring (1–5) based on sample count, data source quality, reduction magnitude, and P95 headroom

### Reporting
- **Console** — colorized table with all metrics, sorted by severity; numbered recommendations; insufficient-data table
- **CSV** — flat export for spreadsheet analysis
- **JSON** — structured payload with metadata, summary, and per-function records
- All file outputs written to a timestamped sub-folder so runs never overwrite each other

### Remediation
- Generated bash script applies `aws lambda update-function-configuration` for each targeted function
- `DRY_RUN=true` mode previews all changes without making any AWS calls
- `FORCE=true` mode skips interactive confirmation prompts for CI/CD pipelines
- `SKIP_HIGH_RISK=true` mode automatically skips functions with risk score ≥ 4
- Per-function comment block in the script: ARN, peak/avg/P95 usage, waste %, invocation count, data source, full recommendation text
- Pre-flight checks: verifies AWS CLI is installed and credentials are valid before touching anything

### Rollback
- Companion rollback script generated alongside remediation script
- Restores every targeted function to its exact pre-remediation memory value
- Backed by a `backup_<ts>.json` snapshot of original configurations
- Same `DRY_RUN` / `FORCE` flags as the remediation script

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        main.py  (CLI)                           │
│  argparse → Config override → boto3 session → workflow          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
          ┌────────────────▼────────────────┐
          │      lambda_discovery.py        │
          │  ListFunctions (paginated)      │
          │  → list[FunctionRecord]         │
          └────────────────┬────────────────┘
                           │  parallel (ThreadPoolExecutor)
          ┌────────────────▼────────────────┐
          │      metrics_analyzer.py        │
          │                                 │
          │  1. Logs Insights (StartQuery)  │
          │     ↓ if < MIN_INVOCATIONS      │
          │  2. FilterLogEvents + regex     │
          │     ↓ if < MIN_INVOCATIONS      │
          │  3. GetMetricStatistics         │
          │                                 │
          │  → MetricsResult (peak, avg,    │
          │    min, p95, utilization)       │
          └────────────────┬────────────────┘
                           │
          ┌────────────────▼────────────────┐
          │          optimizer.py           │
          │  utilization bands → status     │
          │  safety floor calculation       │
          │  risk scoring (1–5)             │
          │  → OptimizationRecord           │
          └────────────────┬────────────────┘
                           │
               ┌───────────┴───────────┐
               │                       │
  ┌────────────▼──────────┐  ┌─────────▼──────────────────┐
  │   report_generator.py │  │ remediation_script_         │
  │                       │  │ generator.py               │
  │  Console (colorized)  │  │                            │
  │  CSV report           │  │  remediation_<ts>.sh       │
  │  JSON report          │  │  rollback_<ts>.sh          │
  │                       │  │  backup_<ts>.json          │
  └───────────────────────┘  └────────────────────────────┘
```

### Data flow summary

1. `main.py` parses CLI args and merges them with `.env` / environment config
2. `lambda_discovery` paginates `ListFunctions` and returns typed `FunctionRecord` objects
3. `metrics_analyzer` fetches CloudWatch data for each function in parallel (configurable worker count)
4. `optimizer` applies utilization band logic and risk scoring to produce `OptimizationRecord` dicts
5. `report_generator` writes console output, CSV, and JSON to a timestamped output folder
6. `remediation_script_generator` writes the bash remediation/rollback package to the same folder

---

## Project Structure

```
lambda-rightsizer/
├── lambda_rightsizer/
│   ├── __init__.py
│   ├── config.py                        # Env-driven config, single source of truth
│   ├── lambda_discovery.py              # Lambda function enumeration
│   ├── metrics_analyzer.py              # CloudWatch memory data collection
│   ├── optimizer.py                     # Waste calculation + recommendations
│   ├── report_generator.py              # Console / CSV / JSON output
│   ├── remediation_script_generator.py  # Bash remediation + rollback scripts
│   └── main.py                          # CLI entry point
├── iam/
│   ├── lambda-rightsizer-read-only.json   # Analysis IAM policy
│   ├── lambda-rightsizer-remediation.json # Remediation IAM policy (write)
│   └── README.md                          # Per-permission explanation
├── output/                              # Generated reports (git-ignored)
├── .env.example
├── requirements.txt
└── README.md
```

---

## AWS Prerequisites

| Requirement | Notes |
|---|---|
| AWS account | Any account type |
| AWS CLI v2 | Required to execute the generated remediation script. [Install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| Python 3.11+ | Earlier versions may work but are untested |
| CloudWatch Logs enabled | Lambda functions must have logging enabled for Logs Insights / filter strategies. The CloudWatch Metrics fallback works without logs. |
| Log retention | Logs must cover the configured `LOOKBACK_DAYS` window. Functions with expired or absent logs fall back to CloudWatch Metrics. |

### Supported regions

Any AWS region where Lambda and CloudWatch are available. Set `AWS_REGION` in
`.env` or pass `--region` at runtime.

---

## IAM Setup

Two policies are provided in `iam/`. Attach them to the appropriate identities.

### Policy 1 — Read-only (required to run the tool)

`iam/lambda-rightsizer-read-only.json`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "STSValidateIdentity",         "Action": ["sts:GetCallerIdentity"],                                          "Resource": "*" },
    { "Sid": "LambdaDiscoverFunctions",     "Action": ["lambda:ListFunctions"],                                           "Resource": "*" },
    { "Sid": "CloudWatchLogsInsightsQuery", "Action": ["logs:StartQuery","logs:GetQueryResults","logs:StopQuery"],        "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/*" },
    { "Sid": "CloudWatchLogsFilterEvents",  "Action": ["logs:FilterLogEvents"],                                           "Resource": ["arn:aws:logs:*:*:log-group:/aws/lambda/*","arn:aws:logs:*:*:log-group:/aws/lambda/*:log-stream:*"] },
    { "Sid": "CloudWatchLogsDescribeGroups","Action": ["logs:DescribeLogGroups"],                                         "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/*" },
    { "Sid": "CloudWatchMetricsRead",       "Action": ["cloudwatch:GetMetricStatistics","cloudwatch:ListMetrics"],        "Resource": "*" }
  ]
}
```

### Policy 2 — Remediation (required only to apply changes)

`iam/lambda-rightsizer-remediation.json`

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "LambdaApplyRecommendations", "Action": ["lambda:UpdateFunctionConfiguration"], "Resource": "arn:aws:lambda:*:*:function:*" }
  ]
}
```

### Recommended attachment strategy

```
Analysis identity  (CI runner / developer role / EC2 instance profile)
  └── lambda-rightsizer-read-only        ← always attached

Remediation identity  (separate role, assumed only when applying changes)
  └── lambda-rightsizer-read-only
  └── lambda-rightsizer-remediation      ← attached only at remediation time
```

To scope remediation to a specific region and account, replace the wildcard resource:

```json
"Resource": "arn:aws:lambda:us-east-1:123456789012:function:*"
```

To scope to specific functions only:

```json
"Resource": [
  "arn:aws:lambda:us-east-1:123456789012:function:payment-processor",
  "arn:aws:lambda:us-east-1:123456789012:function:order-handler"
]
```

See `iam/README.md` for a full per-permission explanation.

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-org/lambda-rightsizer.git
cd lambda-rightsizer

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and edit the environment config
cp .env.example .env
```

### Dependencies

```
boto3==1.34.69       # AWS SDK
botocore==1.34.69    # boto3 core
python-dotenv==1.0.1 # .env file loading
tabulate==0.9.0      # Console table formatting
colorama==0.4.6      # Cross-platform terminal colors
```

---

## Configuration

All settings are read from environment variables or a `.env` file in the project root.
CLI flags override both.

| Variable | Default | Description |
|---|---|---|
| `AWS_PROFILE` | `default` | AWS CLI named profile |
| `AWS_REGION` | `us-east-1` | Target AWS region |
| `LOOKBACK_DAYS` | `14` | Analysis window in days |
| `WASTE_THRESHOLD_PERCENT` | `40` | Minimum waste % to flag (legacy threshold, utilization bands take precedence) |
| `MIN_INVOCATIONS` | `10` | Skip functions with fewer invocations than this |
| `UTIL_REDUCE_THRESHOLD` | `30` | Avg utilization below this → reduce memory |
| `UTIL_KEEP_LOWER` | `30` | Lower bound of the "optimal" band |
| `UTIL_KEEP_UPPER` | `70` | Upper bound of the "optimal" band |
| `UTIL_INCREASE_THRESHOLD` | `80` | Avg utilization above this → increase memory |
| `SAFETY_BUFFER_FACTOR` | `1.20` | Headroom multiplier above observed peak (20%) |
| `MEMORY_STEP_MB` | `64` | Rounding step for recommendations (AWS requirement) |
| `OUTPUT_DIR` | `./output` | Root directory for all generated files |
| `LOG_LEVEL` | `INFO` | Logging verbosity: DEBUG / INFO / WARNING / ERROR |
| `LAMBDA_PRICE_PER_GB_SECOND` | `0.0000166667` | Lambda price per GB-second (us-east-1) |

---

## Usage

### Basic scan

```bash
python -m lambda_rightsizer.main
```

### Common options

```bash
# Scan a specific region with a 30-day lookback
python -m lambda_rightsizer.main --region eu-west-1 --days 30

# Preview analysis without writing any files
python -m lambda_rightsizer.main --dry-run

# Scan only functions whose names contain "payment" or "order"
python -m lambda_rightsizer.main --filter payment,order

# Use a non-default AWS profile
python -m lambda_rightsizer.main --profile prod-readonly

# Write output to a custom directory with more parallel workers
python -m lambda_rightsizer.main --output /tmp/rightsizer-reports --workers 10

# Generate analysis reports but skip remediation script generation
python -m lambda_rightsizer.main --no-remediation

# Verbose logging for debugging
python -m lambda_rightsizer.main --log-level DEBUG
```

### All CLI flags

```
--region REGION       AWS region to scan
--profile PROFILE     AWS CLI profile name
--days N              Lookback window in days
--output DIR          Output directory for reports and scripts
--workers N           Parallel workers for metrics fetching (default: 5)
--dry-run             Analyse only — do not write any output files
--no-remediation      Skip remediation and rollback script generation
--log-level LEVEL     DEBUG | INFO | WARNING | ERROR
--filter TERMS        Comma-separated function name substrings (case-insensitive)
```

### Applying recommendations

```bash
# Navigate to the timestamped output folder
cd output/20240315T142301Z/

# Preview changes without applying (recommended first step)
DRY_RUN=true bash remediation_20240315T142301Z.sh

# Apply changes interactively (prompts for confirmation)
bash remediation_20240315T142301Z.sh

# Apply without prompts (CI/CD pipelines)
FORCE=true bash remediation_20240315T142301Z.sh

# Skip high-risk functions (risk score >= 4)
SKIP_HIGH_RISK=true bash remediation_20240315T142301Z.sh
```

---

## Sample Output

### Console report (truncated)

```
════════════════════════════════════════════════════════════════════════════════════════════════════════
                                       LAMBDA RIGHTSIZER
  Generated : 2024-03-15 14:23:01 UTC                    Region : us-east-1
  Lookback  : 14 days                                    Waste threshold : 40%
════════════════════════════════════════════════════════════════════════════════════════════════════════

╭──────────────────────────────────────┬──────────┬───────┬──────┬─────┬─────┬───────┬────────┬─────┬──────┬────────┬──────────────────╮
│ Function                             │ Runtime  │ Alloc │ Peak │ Avg │ P95 │ Util  │ Waste  │ Rec │ Δ MB │ Risk   │ Status           │
│                                      │          │ MB    │ MB   │ MB  │ MB  │ %     │ %      │ MB  │      │        │                  │
├──────────────────────────────────────┼──────────┼───────┼──────┼─────┼─────┼───────┼────────┼─────┼──────┼────────┼──────────────────┤
│ payment-processor                    │ python3.11│ 1024 │  87  │ 72  │ 85  │  7.0% │ 91.5%  │ 128 │ -896 │ low    │ over_provisioned │
│ order-handler                        │ nodejs18 │  512  │ 201  │ 178 │ 198 │ 34.8% │ 60.7%  │ 256 │ -256 │ low    │ over_provisioned │
│ image-resizer                        │ python3.11│  256 │ 231  │ 198 │ 228 │ 77.3% │  9.8%  │ 256 │   0  │ medium │ watch            │
│ auth-validator                       │ nodejs18 │  128  │  98  │ 81  │ 96  │ 63.3% │ 23.4%  │ 128 │   0  │ low    │ optimal          │
│ legacy-report-gen                    │ python3.9 │  128 │   -  │  -  │  -  │   N/A │    N/A │ 128 │   —  │ medium │ insufficient_data│
╰──────────────────────────────────────┴──────────┴───────┴──────┴─────┴─────┴───────┴────────┴─────┴──────┴────────┴──────────────────╯

────────────────────────────────────────────────────────────────────────────────────────────────────────
                                            SUMMARY
────────────────────────────────────────────────────────────────────────────────────────────────────────
  Total functions analysed  : 5          Optimal                   : 1
  Over-provisioned          : 2          Insufficient data         : 1
  Under-provisioned         : 0          Total potential savings   : 1152 MB
  Watch                     : 1          Functions with errors     : 0
────────────────────────────────────────────────────────────────────────────────────────────────────────

  RECOMMENDATIONS

    1. payment-processor  [LOW]
       Reduce memory from 1024 MB to 128 MB. Peak usage was 87 MB (avg 72 MB),
       utilization 7.0% — 91.5% waste. Estimated saving: 896 MB per invocation. Risk: low.

    2. order-handler  [LOW]
       Reduce memory from 512 MB to 256 MB. Peak usage was 201 MB (avg 178 MB),
       utilization 34.8% — 60.7% waste. Estimated saving: 256 MB per invocation. Risk: low.
```

### Final dashboard

```
╔══════════════════════════════════════════════════════════╗
║          LAMBDA RIGHTSIZER — RUN COMPLETE                ║
╠══════════════════════════════════════════════════════════╣
║  Region                    us-east-1                     ║
║  Lookback                  14 days                       ║
║  Functions scanned         5                             ║
║  Elapsed                   12.4s                         ║
╠══════════════════════════════════════════════════════════╣
║  Over-provisioned          2                             ║
║  Under-provisioned         0                             ║
║  Watch                     1                             ║
║  Optimal                   1                             ║
║  Insufficient data         1                             ║
║  Metric errors             0                             ║
╠══════════════════════════════════════════════════════════╣
║  Potential savings         1152 MB                       ║
╠══════════════════════════════════════════════════════════╣
║  CSV report    output/20240315T142301Z/rightsizer_...csv ║
║  JSON report   output/20240315T142301Z/rightsizer_...json║
╠══════════════════════════════════════════════════════════╣
║  Remediation   output/20240315T142301Z/remediation_...sh ║
║  Rollback      output/20240315T142301Z/rollback_...sh    ║
║  Backup        output/20240315T142301Z/backup_...json    ║
╚══════════════════════════════════════════════════════════╝
```

### Generated remediation script (excerpt)

```bash
# -------------------------------------------------------------------------------
# Function   : payment-processor
# ARN        : arn:aws:lambda:us-east-1:123456789012:function:payment-processor
# Action     : REDUCE  1024MB → 128MB  (Δ -896MB)
# Status     : over_provisioned
# Risk       : low (score=2)
# Peak used  : 87 MB   Avg: 72 MB   P95: 85 MB
# Waste      : 91.5%
# Invocations: 4821   Data source: logs_insights
# Recommendation: Reduce memory from 1024 MB to 128 MB. Peak usage was 87 MB ...
# -------------------------------------------------------------------------------
apply_change "payment-processor" 128 1024 2
```

---

## Output Files

Every run creates a timestamped sub-folder under `OUTPUT_DIR`:

```
output/
└── 20240315T142301Z/
    ├── rightsizer_report_20240315T142301Z.csv    # Flat CSV, all fields
    ├── rightsizer_report_20240315T142301Z.json   # Structured JSON with summary
    ├── remediation_20240315T142301Z.sh           # Apply recommendations
    ├── rollback_20240315T142301Z.sh              # Restore original values
    └── backup_20240315T142301Z.json              # Pre-change config snapshot
```

Runs never overwrite each other. Keep the output folder in `.gitignore` if
the repository is shared — it may contain account-specific function names and ARNs.

### JSON report structure

```json
{
  "meta": {
    "generated_at": "2024-03-15 14:23:01 UTC",
    "region": "us-east-1",
    "lookback_days": 14,
    "waste_threshold_percent": 40.0,
    "min_invocations": 10
  },
  "summary": {
    "total": 5,
    "over_provisioned": 2,
    "under_provisioned": 0,
    "watch": 1,
    "optimal": 1,
    "insufficient_data": 1,
    "total_savings_mb": 1152,
    "functions_with_recommendations": 2
  },
  "functions": [ ... ]
}
```

---

## Safety Considerations

### The tool is read-only by default

Running `python -m lambda_rightsizer.main` makes no changes to any Lambda function.
It only reads data from CloudWatch and Lambda APIs.

### The remediation script requires explicit action

The generated bash script does nothing until you run it. Even then:

- It prints a full change summary and asks for confirmation before proceeding
- High-risk functions (score ≥ 4) require a second, per-function confirmation
- `DRY_RUN=true` lets you see exactly what would happen without any AWS calls

### The 20% safety buffer

Recommended memory is always calculated as:

```
recommended_mb = ceil(peak_observed_mb × 1.20 / 64) × 64
```

The tool will never recommend memory below this floor, regardless of how low
average utilization is. This protects against cold-start spikes and statistical
outliers in the observation window.

### Risk scoring

Each recommendation carries a risk score from 1 (very low) to 5 (very high).
Factors that increase risk:

| Factor | Score increase |
|---|---|
| Fewer than 50 invocations in the window | +2 |
| Fewer than 5× MIN_INVOCATIONS | +1 |
| Data from CloudWatch Metrics fallback (less granular) | +1 |
| Reduction > 50% of current allocation | +1 |
| P95 usage within 10% of recommended ceiling | +1 |

Functions with risk score ≥ 4 require explicit per-function confirmation
and can be excluded entirely with `SKIP_HIGH_RISK=true`.

### Validate in non-production first

Before applying changes to production functions, apply the same analysis to
a staging or development environment. Confirm that function behaviour and
error rates are unchanged after the memory reduction.

### `UpdateFunctionConfiguration` is non-destructive

Changing memory size does not redeploy code, modify environment variables,
change IAM roles, or affect VPC settings. It takes effect on the next
cold start. Warm instances continue running with the previous memory allocation
until they are recycled.

---

## Rollback Process

If a memory change causes unexpected behaviour (increased timeouts, out-of-memory
errors, latency regression), restore the original configuration immediately:

```bash
# Navigate to the run folder that was applied
cd output/20240315T142301Z/

# Preview what will be restored
DRY_RUN=true bash rollback_20240315T142301Z.sh

# Restore all functions to their original memory values
bash rollback_20240315T142301Z.sh
```

The rollback script restores every function to the exact `allocated_mb` value
captured at analysis time. It uses the same `apply_change` function as the
remediation script, with the same pre-flight checks, confirmation prompts,
and error handling.

The `backup_20240315T142301Z.json` file in the same folder is a machine-readable
record of original values and can be used to construct manual `aws lambda`
commands if the rollback script itself is unavailable:

```bash
aws lambda update-function-configuration \
  --region us-east-1 \
  --function-name payment-processor \
  --memory-size 1024
```

---

## Future Enhancements

### Short-term

- **Duration-based cost modelling** — incorporate average invocation duration
  from CloudWatch to produce a monthly USD savings estimate alongside the MB figure
- **Multi-region scan** — run analysis across all enabled regions in a single
  invocation and produce a consolidated cross-region report
- **Function name prefix / tag filtering** — filter by resource tags
  (`aws:cloudformation:stack-name`, custom cost-allocation tags) in addition to
  name substrings
- **Slack / Teams notification** — post the summary dashboard to a webhook
  after each run

### Medium-term

- **Scheduled execution** — CloudFormation / CDK template to deploy the tool
  as a scheduled Lambda function or ECS Fargate task with results written to S3
- **Trend analysis** — compare successive runs to detect functions whose memory
  usage is growing over time and flag them before they become under-provisioned
- **Provisioned Concurrency analysis** — extend the optimizer to evaluate
  whether Provisioned Concurrency is cost-effective given observed cold-start rates
- **ARM64 / Graviton recommendation** — flag x86_64 functions that are candidates
  for migration to `arm64` architecture (typically 20% cheaper per GB-second)

### Long-term

- **Web dashboard** — React front-end backed by the JSON report API for
  interactive exploration of recommendations across accounts and regions
- **CI/CD integration** — GitHub Actions / GitLab CI workflow that runs the
  analysis on pull requests and comments recommendations on infrastructure changes
- **Auto-remediation with guardrails** — fully automated apply pipeline with
  configurable guardrails (max reduction per run, mandatory staging validation,
  automatic rollback on elevated error rate)
- **Cross-account support** — assume roles across an AWS Organization to
  produce account-level and organization-level savings reports

---

## Contributing

1. Fork the repository and create a feature branch
2. Follow the existing module structure — one responsibility per file
3. Add or update tests for any changed logic
4. Ensure `getDiagnostics` passes with no errors before opening a PR
5. Keep PRs focused — one feature or fix per PR

## License

MIT — see `LICENSE` for details.
