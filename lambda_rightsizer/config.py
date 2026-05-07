"""
config.py - Centralized configuration loader.
Reads from environment variables / .env file with sensible defaults.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()


class Config:
    # AWS
    AWS_PROFILE: str = os.getenv("AWS_PROFILE", "personal")
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")

    # Analysis window
    LOOKBACK_DAYS: int = int(os.getenv("LOOKBACK_DAYS", "2"))

    # Optimization thresholds
    WASTE_THRESHOLD_PERCENT: float = float(os.getenv("WASTE_THRESHOLD_PERCENT", "40"))
    MIN_INVOCATIONS: int = int(os.getenv("MIN_INVOCATIONS", "10"))

    # Utilization bands (percent of allocated memory)
    UTIL_REDUCE_THRESHOLD: float = float(os.getenv("UTIL_REDUCE_THRESHOLD", "30"))   # below → reduce
    UTIL_KEEP_LOWER: float = float(os.getenv("UTIL_KEEP_LOWER", "30"))               # 30–70 → keep
    UTIL_KEEP_UPPER: float = float(os.getenv("UTIL_KEEP_UPPER", "70"))               # above 70 → watch
    UTIL_INCREASE_THRESHOLD: float = float(os.getenv("UTIL_INCREASE_THRESHOLD", "80"))  # above → increase

    # Safety buffer above observed peak (20%)
    SAFETY_BUFFER_FACTOR: float = float(os.getenv("SAFETY_BUFFER_FACTOR", "1.20"))

    # Monthly pricing estimate (AWS Lambda price per GB-second, us-east-1)
    # https://aws.amazon.com/lambda/pricing/
    LAMBDA_PRICE_PER_GB_SECOND: float = float(os.getenv("LAMBDA_PRICE_PER_GB_SECOND", "0.0000166667"))
    # Assumed average invocation duration in seconds when not available from metrics
    ASSUMED_AVG_DURATION_SECONDS: float = float(os.getenv("ASSUMED_AVG_DURATION_SECONDS", "1.0"))

    # Memory rounding step (AWS accepts multiples of 64 MB)
    MEMORY_STEP_MB: int = int(os.getenv("MEMORY_STEP_MB", "64"))

    # Output
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "./output")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    @classmethod
    def get_log_level(cls) -> int:
        return getattr(logging, cls.LOG_LEVEL.upper(), logging.INFO)
