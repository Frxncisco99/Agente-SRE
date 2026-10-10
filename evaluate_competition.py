"""
evaluate_competition.py — Competition Scorer & Validator for EWS.

Evaluates an Early Warning System against ground truth labels (etiquetas.csv):
  - Lead Time: Time between early alert and ground truth incident impact.
  - Precision / False Positive Rate: Distinguishing real incidents vs decoys/normal noise.
  - Recall: Ensuring all critical incidents are captured.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import DATASET_BASE, TENANTS
from models import VerifiedAlert
from main import EWSOrchestrator


@dataclass
class IncidentLabel:
    timestamp: datetime
    tipo: str
    es_real: bool


def load_ground_truth(tenant_id: str, data_dir: Path | None = None) -> list[IncidentLabel]:
    data_dir = data_dir or DATASET_BASE
    path = data_dir / "_solucion" / tenant_id / "etiquetas.csv"
    if not path.exists():
        return []

    labels = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = datetime.fromisoformat(row["inicio_incidente"].strip())
            es_real = row["es_real"].strip().lower() == "true"
            labels.append(IncidentLabel(timestamp=ts, tipo=row["tipo"].strip(), es_real=es_real))
    return labels


def score_run(alerts: list[VerifiedAlert], ground_truth: list[IncidentLabel]) -> dict[str, Any]:
    """Score alerts against real incidents (True Positives, False Positives, Lead Time)."""
    real_incidents = [l for l in ground_truth if l.es_real]
    decoys = [l for l in ground_truth if not l.es_real]

    matched_incidents: set[int] = set()
    lead_times_minutes: list[float] = []
    false_positives = 0
    true_positives = 0

    # Tolerance window: alert can happen up to 48 hours before impact down to 30 mins after impact
    for alert in alerts:
        match_found = False
        for idx, inc in enumerate(real_incidents):
            # Lead time is positive if detected before the impact minute
            diff = (inc.timestamp - alert.timestamp).total_seconds() / 60.0
            if -30 <= diff <= 2880:  # alerted within valid anticipation window
                matched_incidents.add(idx)
                lead_times_minutes.append(diff)
                true_positives += 1
                match_found = True
                break

        if not match_found:
            false_positives += 1

    recall = len(matched_incidents) / len(real_incidents) if real_incidents else 1.0
    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) else 0.0
    avg_lead_time = sum(lead_times_minutes) / len(lead_times_minutes) if lead_times_minutes else 0.0

    return {
        "total_alerts": len(alerts),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "real_incidents_total": len(real_incidents),
        "real_incidents_detected": len(matched_incidents),
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "avg_lead_time_minutes": round(avg_lead_time, 1),
    }


if __name__ == "__main__":
    import sys
    tenant = sys.argv[1] if len(sys.argv) > 1 else "gracko"
    print(f"--- Scoring EWS for tenant: {tenant} ---")
    orchestrator = EWSOrchestrator(llm_provider="offline", dry_run=True)
    alerts = orchestrator.run_single_tenant(tenant)
    gt = load_ground_truth(tenant)
    scores = score_run(alerts, gt)
    for k, v in scores.items():
        print(f"  {k}: {v}")
