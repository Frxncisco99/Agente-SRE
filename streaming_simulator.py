"""
streaming_simulator.py — Module 1: The Data Feeder.

Simulates a real-time production data stream by reading metricas.csv
row-by-row (minute-by-minute) and yielding MetricPoint objects.

CAUSALITY GUARANTEE:
    - Data is yielded strictly in chronological order.
    - Forward-fill imputation is applied with a configurable limit
      (default: 3 consecutive minutes). Beyond that limit, the value
      remains None (sensor blackout).
    - No future data is ever accessed at any point.
"""

from __future__ import annotations

import csv
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Generator, Iterator

from config import (
    DATASET_BASE,
    FFILL_LIMIT,
    METRIC_COLUMNS,
    NUMERIC_COLUMNS,
)
from models import MetricPoint

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Causal Forward-Fill Imputer
# ──────────────────────────────────────────────────────────────────────

class CausalImputer:
    """Strictly causal forward-fill imputer.
    
    Tracks the last known value for each numeric column and a counter
    for how many consecutive minutes that column has been missing.
    When the counter exceeds `ffill_limit`, the value stays None
    (sensor blackout) rather than propagating stale data.
    
    This class NEVER looks ahead — it only uses past values.
    """

    def __init__(self, columns: list[str], ffill_limit: int = FFILL_LIMIT) -> None:
        self._columns = columns
        self._ffill_limit = ffill_limit
        # Last known good value for each column
        self._last_known: dict[str, float | None] = {col: None for col in columns}
        # Consecutive NaN counter per column
        self._nan_streak: dict[str, int] = {col: 0 for col in columns}

    def impute(self, raw_values: dict[str, float | None]) -> tuple[dict[str, float | None], bool]:
        """Apply causal forward-fill to a single row.
        
        Args:
            raw_values: Raw parsed values (None where CSV cell was empty).
            
        Returns:
            Tuple of (imputed_values, was_imputed) where was_imputed is
            True if any value was forward-filled.
        """
        imputed = {}
        any_imputed = False

        for col in self._columns:
            raw_val = raw_values.get(col)

            if raw_val is not None:
                # ✅ Fresh data: reset streak, update last known
                self._last_known[col] = raw_val
                self._nan_streak[col] = 0
                imputed[col] = raw_val
            else:
                # ❌ Missing data: check if we can forward-fill
                self._nan_streak[col] += 1

                if (
                    self._last_known[col] is not None
                    and self._nan_streak[col] <= self._ffill_limit
                ):
                    # Forward-fill within allowed limit
                    imputed[col] = self._last_known[col]
                    any_imputed = True
                    logger.debug(
                        "Imputed %s = %.2f (ffill, streak=%d)",
                        col, self._last_known[col], self._nan_streak[col]
                    )
                else:
                    # Sensor blackout: beyond ffill limit or no prior value
                    imputed[col] = None
                    if self._nan_streak[col] == self._ffill_limit + 1:
                        logger.warning(
                            "Sensor blackout for '%s' — %d consecutive NaN (limit=%d)",
                            col, self._nan_streak[col], self._ffill_limit
                        )

        return imputed, any_imputed

    def reset(self) -> None:
        """Reset imputer state (e.g., when switching tenants)."""
        self._last_known = {col: None for col in self._columns}
        self._nan_streak = {col: 0 for col in self._columns}


# ──────────────────────────────────────────────────────────────────────
# CSV Row Parser
# ──────────────────────────────────────────────────────────────────────

def _parse_timestamp(raw: str) -> datetime:
    """Parse ISO 8601 timestamp string to datetime."""
    return datetime.fromisoformat(raw.strip())


def _parse_float(raw: str) -> float | None:
    """Parse a numeric cell, returning None for missing/invalid values."""
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        logger.warning("Unparseable numeric value: '%s'", stripped)
        return None


def _parse_row(row: dict[str, str], tenant_id: str) -> tuple[datetime, dict[str, float | None]]:
    """Parse a CSV DictReader row into (timestamp, numeric_values)."""
    ts = _parse_timestamp(row["timestamp"])
    numeric_vals: dict[str, float | None] = {}
    for col in NUMERIC_COLUMNS:
        numeric_vals[col] = _parse_float(row.get(col, ""))
    return ts, numeric_vals


# ──────────────────────────────────────────────────────────────────────
# Streaming Generator
# ──────────────────────────────────────────────────────────────────────

def stream_metrics(
    tenant_id: str,
    csv_path: Path | None = None,
    ffill_limit: int = FFILL_LIMIT,
) -> Generator[MetricPoint, None, None]:
    """Yield MetricPoint objects one-by-one from a tenant's metricas.csv.
    
    This generator simulates a live production data stream:
    - Reads the CSV row-by-row (never loads the full file into memory).
    - Applies causal forward-fill imputation.
    - Yields data in strict chronological order.
    
    Args:
        tenant_id: Tenant identifier (e.g., "gracko").
        csv_path: Optional override for the CSV file path.
                  Defaults to DATASET_BASE / tenant_id / "metricas.csv".
        ffill_limit: Maximum consecutive NaN minutes to forward-fill.
        
    Yields:
        MetricPoint for each minute of telemetry data.
        
    Raises:
        FileNotFoundError: If the CSV file doesn't exist.
    """
    if csv_path is None:
        csv_path = DATASET_BASE / tenant_id / "metricas.csv"

    if not csv_path.exists():
        raise FileNotFoundError(f"Metrics file not found: {csv_path}")

    logger.info("📡 Starting stream for tenant '%s' from %s", tenant_id, csv_path)

    imputer = CausalImputer(NUMERIC_COLUMNS, ffill_limit)
    row_count = 0

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            try:
                ts, raw_values = _parse_row(row, tenant_id)
            except (KeyError, ValueError) as exc:
                logger.error("Skipping malformed row #%d: %s — %s", row_count, row, exc)
                continue

            # Apply strictly causal forward-fill imputation
            imputed_values, was_imputed = imputer.impute(raw_values)

            point = MetricPoint(
                timestamp=ts,
                cpu=imputed_values.get("cpu"),
                ram=imputed_values.get("ram"),
                disco=imputed_values.get("disco"),
                latencia_ms=imputed_values.get("latencia_ms"),
                errores=imputed_values.get("errores"),
                tenant_id=tenant_id,
                imputed=was_imputed,
            )

            row_count += 1
            yield point

    logger.info(
        "✅ Stream completed for tenant '%s': %d data points yielded",
        tenant_id, row_count,
    )


# ──────────────────────────────────────────────────────────────────────
# Multi-Tenant Interleaved Stream
# ──────────────────────────────────────────────────────────────────────

def stream_all_tenants(
    tenant_ids: list[str] | None = None,
    data_dir: Path | None = None,
) -> Generator[MetricPoint, None, None]:
    """Interleave streams from multiple tenants in chronological order.
    
    Merges the per-tenant streams by timestamp so the orchestrator
    processes events in global time order (as a real monitoring system would).
    
    Uses a priority-like approach: peek the next timestamp from each
    tenant's stream and yield the earliest one. This is strictly causal.
    
    Args:
        tenant_ids: List of tenant IDs. Defaults to config.TENANTS.
        data_dir: Base directory. Defaults to config.DATASET_BASE.
        
    Yields:
        MetricPoint objects in global chronological order.
    """
    from config import TENANTS

    if tenant_ids is None:
        tenant_ids = TENANTS
    if data_dir is None:
        data_dir = DATASET_BASE

    # Initialize per-tenant generators with their first peeked value
    streams: list[tuple[MetricPoint, Generator[MetricPoint, None, None]]] = []
    for tid in tenant_ids:
        csv_path = data_dir / tid / "metricas.csv"
        gen = stream_metrics(tid, csv_path)
        try:
            first = next(gen)
            streams.append((first, gen))
        except (StopIteration, FileNotFoundError) as exc:
            logger.warning("No data for tenant '%s': %s", tid, exc)

    logger.info("🔀 Interleaving %d tenant streams", len(streams))

    while streams:
        # Find the stream with the earliest next timestamp
        min_idx = 0
        min_ts = streams[0][0].timestamp
        for i in range(1, len(streams)):
            if streams[i][0].timestamp < min_ts:
                min_ts = streams[i][0].timestamp
                min_idx = i

        # Yield the earliest point
        point, gen = streams[min_idx]
        yield point

        # Advance that generator
        try:
            next_point = next(gen)
            streams[min_idx] = (next_point, gen)
        except StopIteration:
            # This tenant's stream is exhausted
            streams.pop(min_idx)

    logger.info("✅ All tenant streams exhausted")


# ──────────────────────────────────────────────────────────────────────
# Corrida (Competition Run) Stream
# ──────────────────────────────────────────────────────────────────────

def stream_corrida(
    corrida_num: int,
    tenant_id: str,
    base_dir: Path | None = None,
) -> Generator[MetricPoint, None, None]:
    """Stream a specific corrida (competition run) for a tenant.
    
    Args:
        corrida_num: The corrida number (e.g., 48).
        tenant_id: Tenant identifier.
        base_dir: Base dataset directory.
        
    Yields:
        MetricPoint objects from the corrida's metricas.csv.
    """
    if base_dir is None:
        base_dir = DATASET_BASE

    csv_path = base_dir / "corridas" / f"corrida_{corrida_num}" / tenant_id / "metricas.csv"
    yield from stream_metrics(tenant_id, csv_path)


# ──────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s | %(message)s")

    tenant = sys.argv[1] if len(sys.argv) > 1 else "gracko"
    count = 0
    imputed_count = 0

    for point in stream_metrics(tenant):
        count += 1
        if point.imputed:
            imputed_count += 1
        if count <= 20:
            print(
                f"  [{point.timestamp}] cpu={point.cpu} ram={point.ram} "
                f"disco={point.disco} lat={point.latencia_ms} err={point.errores}"
                f"{' (imputed)' if point.imputed else ''}"
            )

    print(f"\n📊 Total: {count} points, {imputed_count} imputed ({imputed_count/count*100:.1f}%)")
