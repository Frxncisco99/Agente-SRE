"""
models.py — Data Models for the Early Warning System.

Pydantic-free, pure dataclass definitions for type safety and serialization.
All inter-module communication flows through these structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


# ──────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────

class AnomalyType(str, Enum):
    """Types of anomalies the trend analyzer can detect."""
    MEMORY_LEAK = "memory_leak"
    DISK_LEAK = "disk_leak"
    RESOURCE_EXHAUSTION = "resource_exhaustion"      # latency ↑ while cpu stable
    ERROR_BURST = "error_burst"
    COMPOSITE = "composite"                          # multiple signals combined


class Severity(str, Enum):
    """Alert severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ──────────────────────────────────────────────────────────────────────
# Data Point — single minute of telemetry
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MetricPoint:
    """A single minute of telemetry data from metricas.csv.
    
    All numeric fields are Optional[float] to represent sensor blackouts.
    """
    timestamp: datetime
    cpu: float | None
    ram: float | None
    disco: float | None
    latencia_ms: float | None
    errores: float | None
    tenant_id: str = ""
    imputed: bool = False  # True if any field was forward-filled

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary."""
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────
# Trend Analysis Result — slope + statistics for a single window
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TrendResult:
    """Result of linear regression on a single metric over a window."""
    metric_name: str           # e.g. "ram", "disco", "latencia_ms"
    window_minutes: int        # window size used
    slope: float               # first derivative (units per minute)
    r_squared: float           # goodness of fit (0.0 – 1.0)
    mean_value: float          # mean of the metric in the window
    start_value: float         # first value in window
    end_value: float           # last value in window
    data_points: int           # number of non-NaN points used


# ──────────────────────────────────────────────────────────────────────
# Detected Anomaly — output of the trend analyzer
# ──────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class DetectedAnomaly:
    """An anomaly detected by the trend analyzer, pre-LLM filtering."""
    tenant_id: str
    timestamp: datetime
    anomaly_type: AnomalyType
    severity: Severity
    score: float                               # 0.0 – 1.0 composite score
    description: str                           # human-readable explanation
    trends: list[TrendResult] = field(default_factory=list)
    raw_metrics: dict[str, float | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for LLM prompt or logging."""
        return {
            "tenant_id": self.tenant_id,
            "timestamp": self.timestamp.isoformat(),
            "anomaly_type": self.anomaly_type.value,
            "severity": self.severity.value,
            "score": round(self.score, 4),
            "description": self.description,
            "trends": [asdict(t) for t in self.trends],
            "raw_metrics": self.raw_metrics,
        }


# ──────────────────────────────────────────────────────────────────────
# LLM Verdict — output of the context filter
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LLMVerdict:
    """Structured response from the LLM anti-decoy agent."""
    is_real_incident: bool
    confidence: float          # 0.0 – 1.0
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────
# Alert — final verified alert dispatched to n8n
# ──────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class VerifiedAlert:
    """A verified alert ready for dispatch to n8n."""
    tenant_id: str
    timestamp: datetime
    anomaly_type: AnomalyType
    severity: Severity
    anomaly_score: float
    llm_confidence: float
    llm_reasoning: str
    description: str
    remediation: str = ""
    dispatched: bool = False
    dispatch_status_code: int | None = None

    def to_webhook_payload(self) -> dict[str, Any]:
        """Build the n8n webhook payload."""
        return {
            "tenant_id": self.tenant_id,
            "timestamp": self.timestamp.isoformat(),
            "anomaly_type": self.anomaly_type.value,
            "severity": self.severity.value,
            "anomaly_score": round(self.anomaly_score, 4),
            "llm_confidence": round(self.llm_confidence, 4),
            "llm_reasoning": self.llm_reasoning,
            "description": self.description,
            "remediation": self.remediation,
        }
