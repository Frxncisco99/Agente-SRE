"""
trend_analyzer.py — Module 2: The Precursor Detector.

Detects pre-incident patterns using mathematical trend analysis with
RIGHT-ALIGNED sliding windows. No static thresholds are used — all
detection is based on the FIRST DERIVATIVE (slope / rate of change)
and goodness-of-fit (R²) of linear regression over recent data.

CAUSALITY GUARANTEE:
    - All windows are strictly right-aligned: window at time t only
      uses data from [t - window_size + 1, t].
    - No future data is ever accessed.
    - numpy.polyfit is computed only on available past data.

DETECTED PATTERNS:
    1. Memory Leak   — ram increasing steadily over 30+ min without release.
    2. Disk Leak     — disco increasing steadily over 30+ min.
    3. Resource Exhaustion / Deadlock — latencia_ms climbing while cpu stable.
    4. Error Burst   — errores/min spiking rapidly over 15 min.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

import numpy as np

from config import (
    ANOMALY_SCORE_TRIGGER,
    CPU_STABILITY_MAX_SLOPE,
    ERROR_BURST_THRESHOLD,
    ERROR_BURST_WINDOW,
    EXHAUSTION_MIN_WINDOW,
    EXHAUSTION_R2_THRESHOLD,
    LATENCY_SLOPE_THRESHOLD,
    LEAK_MIN_WINDOW,
    LEAK_R2_THRESHOLD,
    LEAK_SLOPE_THRESHOLD,
    WINDOW_SIZES,
    ALERT_COOLDOWN_MINUTES,
)
from models import (
    AnomalyType,
    DetectedAnomaly,
    MetricPoint,
    Severity,
    TrendResult,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Linear Regression (Strictly Causal)
# ──────────────────────────────────────────────────────────────────────

def _compute_slope_r2(values: list[float]) -> tuple[float, float]:
    """Compute the slope (first derivative) and R² via OLS linear regression.
    
    The independent variable is simply the minute index [0, 1, 2, ...N-1].
    The slope is in units of `metric_value / minute`.
    
    Args:
        values: Chronologically ordered metric values (no NaN).
        
    Returns:
        (slope, r_squared) tuple. R² ranges [0, 1]; values near 1 indicate
        a strong linear trend, essential for distinguishing real leaks
        from noisy fluctuations.
    """
    n = len(values)
    if n < 3:
        return 0.0, 0.0

    x = np.arange(n, dtype=np.float64)
    y = np.array(values, dtype=np.float64)

    # Ordinary Least Squares via numpy.polyfit (degree 1)
    coeffs = np.polyfit(x, y, 1)
    slope = float(coeffs[0])

    # R² = 1 - SS_res / SS_tot
    y_pred = np.polyval(coeffs, x)
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)

    if ss_tot < 1e-12:
        # All values identical — perfect "flat" trend
        r_squared = 1.0 if abs(slope) < 1e-12 else 0.0
    else:
        r_squared = max(0.0, 1.0 - ss_res / ss_tot)

    return slope, r_squared


def compute_trend(
    metric_name: str,
    values: list[float],
    window_minutes: int,
) -> TrendResult:
    """Compute trend statistics for a metric over a right-aligned window.
    
    Args:
        metric_name: Name of the metric (e.g., "ram").
        values: The most recent `window_minutes` values (strictly past data).
        window_minutes: Window size used.
        
    Returns:
        TrendResult with slope, R², and summary statistics.
    """
    slope, r2 = _compute_slope_r2(values)

    return TrendResult(
        metric_name=metric_name,
        window_minutes=window_minutes,
        slope=slope,
        r_squared=r2,
        mean_value=float(np.mean(values)),
        start_value=values[0],
        end_value=values[-1],
        data_points=len(values),
    )


# ──────────────────────────────────────────────────────────────────────
# Rolling Window Buffer (Strictly Right-Aligned)
# ──────────────────────────────────────────────────────────────────────

class MetricBuffer:
    """Thread-safe, fixed-size ring buffer for time-series metric data.
    
    Stores the last N minutes of data for a single tenant. The buffer
    is strictly right-aligned: at time t, it contains data from
    [t - max_window + 1, t] at most.
    """

    def __init__(self, max_window: int = 60) -> None:
        """Initialize the buffer.
        
        Args:
            max_window: Maximum window size to support (in minutes).
                        The buffer retains at most this many data points.
        """
        self._max_window = max_window
        # Per-metric deques, each capped at max_window
        self._buffers: dict[str, deque[float | None]] = {
            "cpu": deque(maxlen=max_window),
            "ram": deque(maxlen=max_window),
            "disco": deque(maxlen=max_window),
            "latencia_ms": deque(maxlen=max_window),
            "errores": deque(maxlen=max_window),
        }
        self._timestamps: deque[datetime] = deque(maxlen=max_window)
        self._count: int = 0

    def push(self, point: MetricPoint) -> None:
        """Add a new data point to the buffer (right side / most recent)."""
        self._buffers["cpu"].append(point.cpu)
        self._buffers["ram"].append(point.ram)
        self._buffers["disco"].append(point.disco)
        self._buffers["latencia_ms"].append(point.latencia_ms)
        self._buffers["errores"].append(point.errores)
        self._timestamps.append(point.timestamp)
        self._count += 1

    def get_window(self, metric: str, window_size: int) -> list[float]:
        """Get the last `window_size` non-None values for a metric.
        
        This is STRICTLY RIGHT-ALIGNED: only the most recent values
        up to window_size are returned. NaN/None values are excluded
        from the regression but DO count as time passing.
        
        Args:
            metric: Metric name (e.g., "ram").
            window_size: Number of most recent minutes to look back.
            
        Returns:
            List of non-None float values (chronological order).
        """
        buf = self._buffers[metric]
        # Take at most `window_size` from the right end
        start = max(0, len(buf) - window_size)
        return [v for v in list(buf)[start:] if v is not None]

    def get_window_with_nones(self, metric: str, window_size: int) -> list[float | None]:
        """Like get_window but preserving None positions (for gap analysis)."""
        buf = self._buffers[metric]
        start = max(0, len(buf) - window_size)
        return list(buf)[start:]

    @property
    def size(self) -> int:
        """Current number of data points in the buffer."""
        return min(self._count, self._max_window)

    @property
    def latest_timestamp(self) -> datetime | None:
        """Most recent timestamp, or None if buffer is empty."""
        return self._timestamps[-1] if self._timestamps else None

    def reset(self) -> None:
        """Clear all buffers."""
        for buf in self._buffers.values():
            buf.clear()
        self._timestamps.clear()
        self._count = 0


# ──────────────────────────────────────────────────────────────────────
# Anomaly Detectors (Pattern-Specific)
# ──────────────────────────────────────────────────────────────────────

def _detect_leak(
    metric_name: str,
    buffer: MetricBuffer,
    anomaly_type: AnomalyType,
    tenant_id: str,
    timestamp: datetime,
) -> DetectedAnomaly | None:
    """Detect Memory Leak or Disk Leak pattern.
    
    Pattern: `ram` or `disco` increasing steadily over 30+ minutes
    without releasing. We use the SLOPE (first derivative) and R² to
    distinguish a real linear growth from noisy oscillations.
    
    Detection criteria:
        - slope ≥ LEAK_SLOPE_THRESHOLD (metric rising per minute)
        - R² ≥ LEAK_R2_THRESHOLD (linear trend, not random noise)
        - window ≥ LEAK_MIN_WINDOW (sustained over enough time)
    """
    best_trend: TrendResult | None = None
    best_score: float = 0.0

    for window in WINDOW_SIZES:
        if window < LEAK_MIN_WINDOW:
            continue

        values = buffer.get_window(metric_name, window)
        if len(values) < LEAK_MIN_WINDOW * 0.8:  # Need 80% data coverage
            continue

        trend = compute_trend(metric_name, values, window)

        # Check if slope and R² meet leak criteria
        if trend.slope >= LEAK_SLOPE_THRESHOLD and trend.r_squared >= LEAK_R2_THRESHOLD:
            # Score: weighted combination of slope magnitude and fit quality
            score = min(1.0, (trend.slope / (LEAK_SLOPE_THRESHOLD * 3)) * 0.6 + trend.r_squared * 0.4)
            if score > best_score:
                best_score = score
                best_trend = trend

    if best_trend is None:
        return None

    # Determine severity from score
    severity = _score_to_severity(best_score)

    delta = best_trend.end_value - best_trend.start_value
    nombre_metrica = "RAM" if metric_name == "ram" else ("DISCO" if metric_name == "disco" else metric_name.upper())
    return DetectedAnomaly(
        tenant_id=tenant_id,
        timestamp=timestamp,
        anomaly_type=anomaly_type,
        severity=severity,
        score=best_score,
        description=(
            f"Fuga sostenida de {nombre_metrica}: +{delta:.1f}% acumulado en "
            f"{best_trend.window_minutes} min (pendiente={best_trend.slope:.3f}/min, "
            f"R²={best_trend.r_squared:.3f}, actual={best_trend.end_value:.1f}%)"
        ),
        trends=[best_trend],
        raw_metrics={
            metric_name: best_trend.end_value,
            f"{metric_name}_slope": best_trend.slope,
        },
    )


def _detect_resource_exhaustion(
    buffer: MetricBuffer,
    tenant_id: str,
    timestamp: datetime,
) -> DetectedAnomaly | None:
    """Detect Resource Exhaustion / Deadlock pattern.
    
    Pattern: latencia_ms climbing continuously while cpu remains
    stable or LOW. This indicates a deadlock, connection pool
    exhaustion, or I/O bottleneck — the server isn't CPU-bound but
    requests are queuing up.
    
    Detection criteria:
        - latency slope ≥ LATENCY_SLOPE_THRESHOLD
        - latency R² ≥ EXHAUSTION_R2_THRESHOLD (steady climb)
        - cpu slope < CPU_STABILITY_MAX_SLOPE (cpu NOT climbing)
    """
    best_trends: list[TrendResult] = []
    best_score: float = 0.0

    for window in WINDOW_SIZES:
        if window < EXHAUSTION_MIN_WINDOW:
            continue

        lat_vals = buffer.get_window("latencia_ms", window)
        cpu_vals = buffer.get_window("cpu", window)

        if len(lat_vals) < EXHAUSTION_MIN_WINDOW * 0.8:
            continue
        if len(cpu_vals) < EXHAUSTION_MIN_WINDOW * 0.5:
            continue

        lat_trend = compute_trend("latencia_ms", lat_vals, window)
        cpu_trend = compute_trend("cpu", cpu_vals, window)

        # Latency must be climbing with a good linear fit
        if lat_trend.slope < LATENCY_SLOPE_THRESHOLD:
            continue
        if lat_trend.r_squared < EXHAUSTION_R2_THRESHOLD:
            continue
        # CPU must be stable (not climbing with latency)
        if cpu_trend.slope > CPU_STABILITY_MAX_SLOPE:
            continue

        # Score: emphasizes latency climb rate and cpu stability
        lat_score = min(1.0, lat_trend.slope / (LATENCY_SLOPE_THRESHOLD * 4))
        cpu_stability = max(0.0, 1.0 - abs(cpu_trend.slope) / CPU_STABILITY_MAX_SLOPE)
        score = lat_score * 0.5 + lat_trend.r_squared * 0.3 + cpu_stability * 0.2

        if score > best_score:
            best_score = score
            best_trends = [lat_trend, cpu_trend]

    if not best_trends:
        return None

    severity = _score_to_severity(best_score)
    lat_t = best_trends[0]
    cpu_t = best_trends[1]

    return DetectedAnomaly(
        tenant_id=tenant_id,
        timestamp=timestamp,
        anomaly_type=AnomalyType.RESOURCE_EXHAUSTION,
        severity=severity,
        score=best_score,
        description=(
            f"Agotamiento de recursos / Deadlock: latencia escalando a +{lat_t.slope:.2f} ms/min "
            f"(R²={lat_t.r_squared:.3f}) con CPU plana ({cpu_t.slope:.3f}/min). "
            f"Latencia actual={lat_t.end_value:.0f} ms, CPU={cpu_t.end_value:.1f}%"
        ),
        trends=best_trends,
        raw_metrics={
            "latencia_ms": lat_t.end_value,
            "latencia_slope": lat_t.slope,
            "cpu": cpu_t.end_value,
            "cpu_slope": cpu_t.slope,
        },
    )


def _detect_error_burst(
    buffer: MetricBuffer,
    tenant_id: str,
    timestamp: datetime,
) -> DetectedAnomaly | None:
    """Detect Error Burst pattern.
    
    Pattern: rapid increase in error rate over a short window (15 min).
    Complements leak/exhaustion detectors with error-rate signal.
    """
    values = buffer.get_window("errores", ERROR_BURST_WINDOW)
    if len(values) < ERROR_BURST_WINDOW * 0.7:
        return None

    trend = compute_trend("errores", values, ERROR_BURST_WINDOW)

    if trend.slope < ERROR_BURST_THRESHOLD:
        return None
    if trend.r_squared < 0.40:
        return None

    score = min(1.0, trend.slope / (ERROR_BURST_THRESHOLD * 3) * 0.7 + trend.r_squared * 0.3)
    severity = _score_to_severity(score)

    return DetectedAnomaly(
        tenant_id=tenant_id,
        timestamp=timestamp,
        anomaly_type=AnomalyType.ERROR_BURST,
        severity=severity,
        score=score,
        description=(
            f"Ráfaga sostenida de errores: subiendo a +{trend.slope:.3f} err/min "
            f"(R²={trend.r_squared:.3f}) en {ERROR_BURST_WINDOW} min. "
            f"Tasa actual={trend.end_value:.1f} err/min"
        ),
        trends=[trend],
        raw_metrics={
            "errores": trend.end_value,
            "errores_slope": trend.slope,
        },
    )


def _score_to_severity(score: float) -> Severity:
    """Map a 0.0–1.0 anomaly score to a Severity enum."""
    if score >= 0.85:
        return Severity.CRITICAL
    elif score >= 0.70:
        return Severity.HIGH
    elif score >= 0.50:
        return Severity.MEDIUM
    else:
        return Severity.LOW


# ──────────────────────────────────────────────────────────────────────
# Alert Cooldown Tracker
# ──────────────────────────────────────────────────────────────────────

class CooldownTracker:
    """Prevents repeated alerts for the same anomaly type within a cooldown window.
    
    This avoids flooding the LLM with duplicate detections during a
    sustained anomaly. Once an anomaly type triggers, it cannot trigger
    again for ALERT_COOLDOWN_MINUTES.
    """

    def __init__(self, cooldown_minutes: int = ALERT_COOLDOWN_MINUTES) -> None:
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._last_alert: dict[str, datetime] = {}  # key: "tenant_id:anomaly_type"

    def is_cooled_down(self, tenant_id: str, anomaly_type: AnomalyType, now: datetime) -> bool:
        """Check if enough time has passed since the last alert of this type."""
        key = f"{tenant_id}:{anomaly_type.value}"
        last = self._last_alert.get(key)
        if last is None:
            return True
        return (now - last) >= self._cooldown

    def record_alert(self, tenant_id: str, anomaly_type: AnomalyType, now: datetime) -> None:
        """Record that an alert was issued."""
        key = f"{tenant_id}:{anomaly_type.value}"
        self._last_alert[key] = now


# ──────────────────────────────────────────────────────────────────────
# Main Analyzer Class
# ──────────────────────────────────────────────────────────────────────

class TrendAnalyzer:
    """Orchestrates multi-pattern trend analysis over streaming data.
    
    For each incoming MetricPoint, the analyzer:
      1. Pushes the point into the tenant's MetricBuffer.
      2. Runs all pattern detectors against the buffer.
      3. Filters by anomaly score threshold and cooldown.
      4. Returns any detected anomalies for LLM evaluation.
    """

    def __init__(
        self,
        max_window: int = 60,
        score_trigger: float = ANOMALY_SCORE_TRIGGER,
        cooldown_minutes: int = ALERT_COOLDOWN_MINUTES,
    ) -> None:
        self._max_window = max_window
        self._score_trigger = score_trigger
        self._buffers: dict[str, MetricBuffer] = {}
        self._cooldown = CooldownTracker(cooldown_minutes)

    def _get_buffer(self, tenant_id: str) -> MetricBuffer:
        """Get or create a MetricBuffer for a tenant."""
        if tenant_id not in self._buffers:
            self._buffers[tenant_id] = MetricBuffer(self._max_window)
        return self._buffers[tenant_id]

    def analyze(self, point: MetricPoint) -> list[DetectedAnomaly]:
        """Process a single MetricPoint and return any detected anomalies.
        
        This is the main entry point called by the orchestrator for
        each minute of telemetry data.
        
        Args:
            point: A single minute of telemetry data.
            
        Returns:
            List of DetectedAnomaly objects that passed the score
            threshold and cooldown filter. May be empty.
        """
        tenant_id = point.tenant_id
        buffer = self._get_buffer(tenant_id)
        buffer.push(point)

        # Need at least 15 minutes of data before analysis
        if buffer.size < min(WINDOW_SIZES):
            return []

        anomalies: list[DetectedAnomaly] = []
        ts = point.timestamp

        # ── Pattern 1: Memory Leak ──────────────────────────────
        mem_leak = _detect_leak("ram", buffer, AnomalyType.MEMORY_LEAK, tenant_id, ts)
        if mem_leak and mem_leak.score >= self._score_trigger:
            if self._cooldown.is_cooled_down(tenant_id, AnomalyType.MEMORY_LEAK, ts):
                anomalies.append(mem_leak)
                self._cooldown.record_alert(tenant_id, AnomalyType.MEMORY_LEAK, ts)
                logger.warning("🧠 [%s] %s", tenant_id, mem_leak.description)

        # ── Pattern 2: Disk Leak ────────────────────────────────
        disk_leak = _detect_leak("disco", buffer, AnomalyType.DISK_LEAK, tenant_id, ts)
        if disk_leak and disk_leak.score >= self._score_trigger:
            if self._cooldown.is_cooled_down(tenant_id, AnomalyType.DISK_LEAK, ts):
                anomalies.append(disk_leak)
                self._cooldown.record_alert(tenant_id, AnomalyType.DISK_LEAK, ts)
                logger.warning("💾 [%s] %s", tenant_id, disk_leak.description)

        # ── Pattern 3: Resource Exhaustion ──────────────────────
        exhaustion = _detect_resource_exhaustion(buffer, tenant_id, ts)
        if exhaustion and exhaustion.score >= self._score_trigger:
            if self._cooldown.is_cooled_down(tenant_id, AnomalyType.RESOURCE_EXHAUSTION, ts):
                anomalies.append(exhaustion)
                self._cooldown.record_alert(tenant_id, AnomalyType.RESOURCE_EXHAUSTION, ts)
                logger.warning("⏳ [%s] %s", tenant_id, exhaustion.description)

        # ── Pattern 4: Error Burst ──────────────────────────────
        err_burst = _detect_error_burst(buffer, tenant_id, ts)
        if err_burst and err_burst.score >= self._score_trigger:
            if self._cooldown.is_cooled_down(tenant_id, AnomalyType.ERROR_BURST, ts):
                anomalies.append(err_burst)
                self._cooldown.record_alert(tenant_id, AnomalyType.ERROR_BURST, ts)
                logger.warning("🔴 [%s] %s", tenant_id, err_burst.description)

        return anomalies

    def get_current_trends(self, tenant_id: str) -> dict[str, list[TrendResult]]:
        """Get current trend analysis for all metrics across all windows.
        
        Useful for debugging and visualization.
        """
        buffer = self._get_buffer(tenant_id)
        results: dict[str, list[TrendResult]] = {}

        for metric in ["cpu", "ram", "disco", "latencia_ms", "errores"]:
            metric_trends: list[TrendResult] = []
            for window in WINDOW_SIZES:
                values = buffer.get_window(metric, window)
                if len(values) >= 5:
                    metric_trends.append(compute_trend(metric, values, window))
            results[metric] = metric_trends

        return results

    def reset(self, tenant_id: str | None = None) -> None:
        """Reset buffers and cooldowns for a tenant or all tenants."""
        if tenant_id:
            if tenant_id in self._buffers:
                self._buffers[tenant_id].reset()
        else:
            for buf in self._buffers.values():
                buf.reset()
            self._buffers.clear()


# ──────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    from streaming_simulator import stream_metrics

    tenant = sys.argv[1] if len(sys.argv) > 1 else "gracko"
    analyzer = TrendAnalyzer()
    total_anomalies = 0

    print(f"🔍 Analyzing trends for tenant '{tenant}'...\n")

    for point in stream_metrics(tenant):
        detected = analyzer.analyze(point)
        for anomaly in detected:
            total_anomalies += 1
            print(
                f"  ⚠️  [{anomaly.timestamp}] {anomaly.anomaly_type.value} "
                f"(score={anomaly.score:.3f}, severity={anomaly.severity.value})"
            )
            print(f"      {anomaly.description}\n")

    print(f"\n📊 Total anomalies detected: {total_anomalies}")
