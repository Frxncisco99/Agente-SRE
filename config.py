"""
config.py — Central Configuration for the Early Warning System (EWS).

All tunable parameters, paths, API keys, and tenant definitions live here.
Modifying this single file adapts the entire pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

# ──────────────────────────────────────────────────────────────────────
# 1. DATASET PATHS
# ──────────────────────────────────────────────────────────────────────
# Base directory where the zikit-dataset is extracted.
# Override via environment variable ZIKIT_DATA_DIR if needed.
DATASET_BASE: Final[Path] = Path(
    os.environ.get("ZIKIT_DATA_DIR", r"c:\Agente SRE\zikit-dataset\zikit-dataset-main\data")
)

# The four competition tenants
TENANTS: Final[list[str]] = ["gracko", "gramo", "kaiku", "ttr"]

# CSV column definitions (strict order matching metricas.csv)
METRIC_COLUMNS: Final[list[str]] = [
    "timestamp", "cpu", "ram", "disco", "latencia_ms", "errores"
]

# Numeric columns (everything except timestamp)
NUMERIC_COLUMNS: Final[list[str]] = ["cpu", "ram", "disco", "latencia_ms", "errores"]

# ──────────────────────────────────────────────────────────────────────
# 2. STREAMING & IMPUTATION
# ──────────────────────────────────────────────────────────────────────
# Maximum consecutive NaN minutes to forward-fill before marking as blackout.
# After this limit, the value stays NaN (sensor offline).
FFILL_LIMIT: Final[int] = 3

# ──────────────────────────────────────────────────────────────────────
# 3. TREND ANALYSIS — SLIDING WINDOWS & THRESHOLDS
# ──────────────────────────────────────────────────────────────────────
# Right-aligned window sizes in minutes for multi-scale trend analysis.
WINDOW_SIZES: Final[list[int]] = [15, 30, 60]

# ── Derivative (slope) thresholds ────────────────────────────────────
# These define the *minimum* slope (per minute) to flag a trend as anomalous.
# They are NOT static value thresholds — they detect *rate of change*.

# Memory/Disk Leak: steady upward slope over 30+ minutes
LEAK_SLOPE_THRESHOLD: Final[float] = 0.08          # % per minute for ram/disco
LEAK_MIN_WINDOW: Final[int] = 30                    # minimum window (minutes)
LEAK_R2_THRESHOLD: Final[float] = 0.70              # R² of linear fit (trend consistency)

# Latency Exhaustion: latency climbing while CPU remains stable/low
LATENCY_SLOPE_THRESHOLD: Final[float] = 2.0         # ms per minute (filters minor 0.5-1.5 noise)
CPU_STABILITY_MAX_SLOPE: Final[float] = 0.15         # cpu slope must be below this
EXHAUSTION_MIN_WINDOW: Final[int] = 15               # minimum window (minutes)
EXHAUSTION_R2_THRESHOLD: Final[float] = 0.70         # R² of latency linear fit

# Error burst: errors per minute spiking
ERROR_BURST_THRESHOLD: Final[float] = 0.15           # errors/min slope
ERROR_BURST_WINDOW: Final[int] = 15                  # window to evaluate

# Composite anomaly score threshold to trigger LLM evaluation
ANOMALY_SCORE_TRIGGER: Final[float] = 0.65           # 0.0 – 1.0 scale

# Cooldown: minimum minutes between repeated alerts for the same anomaly type
ALERT_COOLDOWN_MINUTES: Final[int] = 120

# ──────────────────────────────────────────────────────────────────────
# 4. LLM CONFIGURATION (Anti-Decoy Agent)
# ──────────────────────────────────────────────────────────────────────
# Supported providers: "google" (Gemini) or "openai" (GPT-4o).
LLM_PROVIDER: Final[str] = os.environ.get("LLM_PROVIDER", "google")

# API keys — set via environment variables (never hardcode).
GOOGLE_API_KEY: Final[str] = os.environ.get("GOOGLE_API_KEY", "")
OPENAI_API_KEY: Final[str] = os.environ.get("OPENAI_API_KEY", "")

# Model names
GOOGLE_MODEL: Final[str] = os.environ.get("GOOGLE_MODEL", "gemini-2.0-flash")
OPENAI_MODEL: Final[str] = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# LLM call parameters
LLM_TEMPERATURE: Final[float] = 0.1      # Low temperature for deterministic reasoning
LLM_TIMEOUT_SECONDS: Final[int] = 30     # Timeout for API calls
LLM_MAX_RETRIES: Final[int] = 2          # Retry on transient failures

# Confidence threshold to dispatch an alert to n8n
LLM_CONFIDENCE_THRESHOLD: Final[float] = 0.75

# ──────────────────────────────────────────────────────────────────────
# 5. N8N WEBHOOK
# ──────────────────────────────────────────────────────────────────────
N8N_WEBHOOK_URL: Final[str] = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://your-n8n-instance.app/webhook/ews-alert"
)
N8N_WEBHOOK_TIMEOUT: Final[int] = 10     # seconds
N8N_WEBHOOK_RETRIES: Final[int] = 3

# ──────────────────────────────────────────────────────────────────────
# 6. LOGGING
# ──────────────────────────────────────────────────────────────────────
LOG_LEVEL: Final[str] = os.environ.get("EWS_LOG_LEVEL", "INFO")
LOG_FORMAT: Final[str] = "%(asctime)s | %(name)-22s | %(levelname)-7s | %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S"

# ──────────────────────────────────────────────────────────────────────
# 7. OUTPUT
# ──────────────────────────────────────────────────────────────────────
# Directory for persisting alert logs and results
OUTPUT_DIR: Final[Path] = Path(
    os.environ.get("EWS_OUTPUT_DIR", r"c:\Agente SRE\output")
)
