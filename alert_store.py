"""Persistent SQLite history for Centinel alerts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("centinel_alerts.db")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                anomaly_type TEXT NOT NULL,
                severity TEXT,
                score REAL,
                confidence REAL,
                description TEXT,
                reasoning TEXT,
                remediation TEXT,
                source_device TEXT,
                temperature REAL,
                dispatched INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_alerts_tenant_time ON alerts(tenant_id, timestamp DESC)")


def save_alert(alert: dict[str, Any]) -> None:
    init_db()
    verdict = alert.get("verdict") or {}
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            INSERT INTO alerts
            (tenant_id, timestamp, anomaly_type, severity, score, confidence,
             description, reasoning, remediation, source_device, temperature, dispatched)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alert.get("tenant_id", ""),
            alert.get("timestamp", ""),
            alert.get("type", "unknown"),
            alert.get("severity"),
            float(alert.get("score", 0.0)),
            float(verdict.get("confidence", alert.get("confidence", 0.0))),
            alert.get("description", ""),
            verdict.get("reasoning", alert.get("reasoning", "")),
            alert.get("remediation", ""),
            alert.get("source_device"),
            alert.get("temperature"),
            int(bool(alert.get("dispatched", False))),
        ))


def list_alerts(tenant_id: str, limit: int = 25) -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT timestamp, description, anomaly_type AS type, score,
                   remediation, source_device, temperature, confidence, reasoning,
                   severity, dispatched
            FROM alerts WHERE tenant_id = ?
            ORDER BY timestamp DESC, id DESC LIMIT ?
        """, (tenant_id, max(1, min(int(limit), 500)))).fetchall()
    result = []
    for row in rows:
        result.append({
            "timestamp": row["timestamp"],
            "description": row["description"],
            "type": row["type"],
            "score": row["score"],
            "remediation": row["remediation"],
            "source_device": row["source_device"],
            "temperature": row["temperature"],
            "severity": row["severity"],
            "verdict": {
                "is_real_incident": bool(row["confidence"] >= 0.75),
                "confidence": row["confidence"] or 0.0,
                "reasoning": row["reasoning"] or "Registro histórico",
            },
        })
    return result
