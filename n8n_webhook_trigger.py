"""
n8n_webhook_trigger.py — Module 4: The Mitigation Action.

Dispatches verified alerts to an n8n webhook via HTTP POST.
If the LLM Context Filter returns is_real_incident: true with high
confidence, this module triggers the automated mitigation workflow.

PAYLOAD SCHEMA:
    {
        "tenant_id": str,
        "timestamp": str (ISO 8601),
        "anomaly_type": str,
        "severity": str,
        "anomaly_score": float,
        "llm_confidence": float,
        "llm_reasoning": str,
        "description": str
    }

FEATURES:
    - Retry with exponential backoff on transient failures.
    - Timeout protection.
    - Comprehensive logging of dispatch attempts.
    - Dry-run mode for testing without a live n8n instance.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from config import (
    N8N_WEBHOOK_RETRIES,
    N8N_WEBHOOK_TIMEOUT,
    N8N_WEBHOOK_URL,
)
from models import (
    AnomalyType,
    DetectedAnomaly,
    LLMVerdict,
    Severity,
    VerifiedAlert,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Webhook Dispatcher
# ──────────────────────────────────────────────────────────────────────

class WebhookDispatcher:
    """Dispatches verified alerts to an n8n webhook endpoint.
    
    This module is the final stage of the EWS pipeline. It only fires
    when an anomaly has been:
      1. Detected by the TrendAnalyzer (mathematical trend analysis).
      2. Verified by the LLM Context Filter (anti-decoy reasoning).
      3. Passed the confidence threshold.
    
    Usage:
        dispatcher = WebhookDispatcher()
        alert = dispatcher.create_alert(anomaly, verdict)
        success = dispatcher.dispatch(alert)
    """

    def __init__(
        self,
        webhook_url: str = N8N_WEBHOOK_URL,
        timeout: int = N8N_WEBHOOK_TIMEOUT,
        max_retries: int = N8N_WEBHOOK_RETRIES,
        dry_run: bool = False,
    ) -> None:
        """Initialize the webhook dispatcher.
        
        Args:
            webhook_url: The n8n webhook URL to POST to.
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts on failure.
            dry_run: If True, log the payload but don't actually POST.
        """
        self._webhook_url = webhook_url
        self._timeout = timeout
        self._max_retries = max_retries
        self._dry_run = dry_run

        # Statistics
        self._dispatched_count: int = 0
        self._failed_count: int = 0
        self._alerts_log: list[VerifiedAlert] = []

        mode = "DRY RUN" if dry_run else "LIVE"
        logger.info(
            "🔔 Webhook Dispatcher initialized (%s) → %s",
            mode, webhook_url,
        )

    def create_alert(
        self,
        anomaly: DetectedAnomaly,
        verdict: LLMVerdict,
    ) -> VerifiedAlert:
        """Create a VerifiedAlert from an anomaly + LLM verdict pair.
        
        Args:
            anomaly: The detected anomaly.
            verdict: The LLM's verdict confirming it's a real incident.
            
        Returns:
            VerifiedAlert ready for dispatch.
        """
        return VerifiedAlert(
            tenant_id=anomaly.tenant_id,
            timestamp=anomaly.timestamp,
            anomaly_type=anomaly.anomaly_type,
            severity=anomaly.severity,
            anomaly_score=anomaly.score,
            llm_confidence=verdict.confidence,
            llm_reasoning=verdict.reasoning,
            description=anomaly.description,
        )

    def dispatch(self, alert: VerifiedAlert) -> bool:
        """Dispatch a verified alert to the n8n webhook.
        
        Sends an HTTP POST with the alert payload. Retries on transient
        failures with exponential backoff.
        
        Args:
            alert: The verified alert to dispatch.
            
        Returns:
            True if dispatch was successful (or dry-run), False otherwise.
        """
        payload = alert.to_webhook_payload()
        self._alerts_log.append(alert)

        if self._dry_run:
            logger.info(
                "🔔 [DRY RUN] Would dispatch alert:\n%s",
                json.dumps(payload, indent=2, ensure_ascii=False),
            )
            alert.dispatched = True
            alert.dispatch_status_code = 200
            self._dispatched_count += 1
            return True

        # Live dispatch with retries
        try:
            import requests
        except ImportError:
            logger.error(
                "requests package not installed. Run: pip install requests. "
                "Falling back to dry-run logging."
            )
            self._log_failed_alert(alert, payload, "requests not installed")
            return False

        for attempt in range(1, self._max_retries + 1):
            try:
                logger.info(
                    "📤 Dispatching alert to n8n (attempt %d/%d): %s [%s] %s",
                    attempt, self._max_retries,
                    alert.tenant_id, alert.anomaly_type.value, alert.timestamp.isoformat(),
                )

                response = requests.post(
                    self._webhook_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-EWS-Source": "early-warning-system",
                        "X-EWS-Tenant": alert.tenant_id,
                    },
                    timeout=self._timeout,
                )

                alert.dispatch_status_code = response.status_code

                if response.status_code in (200, 201, 202, 204):
                    alert.dispatched = True
                    self._dispatched_count += 1
                    logger.info(
                        "✅ Alert dispatched successfully (HTTP %d)",
                        response.status_code,
                    )
                    return True
                elif response.status_code >= 500:
                    # Server error — retry
                    logger.warning(
                        "⚠️  n8n returned HTTP %d (attempt %d), retrying...",
                        response.status_code, attempt,
                    )
                else:
                    # Client error — don't retry
                    logger.error(
                        "❌ n8n returned HTTP %d: %s",
                        response.status_code, response.text[:200],
                    )
                    self._failed_count += 1
                    return False

            except requests.exceptions.Timeout:
                logger.warning(
                    "⏱️  n8n request timed out after %d s (attempt %d)",
                    self._timeout, attempt,
                )
            except requests.exceptions.ConnectionError as exc:
                logger.warning(
                    "🔌 Connection error to n8n (attempt %d): %s",
                    attempt, exc,
                )
            except requests.exceptions.RequestException as exc:
                logger.error(
                    "❌ Unexpected request error (attempt %d): %s",
                    attempt, exc,
                )

            # Exponential backoff before retry
            if attempt < self._max_retries:
                backoff = 2 ** attempt
                logger.debug("Backing off %d seconds before retry", backoff)
                time.sleep(backoff)

        # All retries exhausted
        self._log_failed_alert(alert, payload, "All retries exhausted")
        self._failed_count += 1
        return False

    def _log_failed_alert(
        self,
        alert: VerifiedAlert,
        payload: dict[str, Any],
        reason: str,
    ) -> None:
        """Log a failed dispatch for later review/replay."""
        logger.error(
            "❌ FAILED to dispatch alert (%s): %s\nPayload: %s",
            reason,
            f"{alert.tenant_id} [{alert.anomaly_type.value}] {alert.timestamp}",
            json.dumps(payload, indent=2, ensure_ascii=False),
        )

    # ── Batch Operations ────────────────────────────────────────

    def dispatch_batch(self, alerts: list[VerifiedAlert]) -> int:
        """Dispatch multiple alerts. Returns count of successful dispatches."""
        success_count = 0
        for alert in alerts:
            if self.dispatch(alert):
                success_count += 1
        return success_count

    # ── Statistics & Logging ────────────────────────────────────

    @property
    def dispatched_count(self) -> int:
        """Total number of successfully dispatched alerts."""
        return self._dispatched_count

    @property
    def failed_count(self) -> int:
        """Total number of failed dispatches."""
        return self._failed_count

    @property
    def alerts_log(self) -> list[VerifiedAlert]:
        """Full log of all alerts (dispatched or not)."""
        return self._alerts_log

    def get_summary(self) -> dict[str, Any]:
        """Get dispatch statistics summary."""
        return {
            "total_alerts": len(self._alerts_log),
            "dispatched": self._dispatched_count,
            "failed": self._failed_count,
            "webhook_url": self._webhook_url,
            "dry_run": self._dry_run,
        }

    def export_alerts_log(self, filepath: str) -> None:
        """Export the alerts log to a JSON file for review."""
        from pathlib import Path

        records = []
        for alert in self._alerts_log:
            record = alert.to_webhook_payload()
            record["dispatched"] = alert.dispatched
            record["dispatch_status_code"] = alert.dispatch_status_code
            records.append(record)

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)

        logger.info("📝 Alerts log exported to %s (%d alerts)", filepath, len(records))


# ──────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s | %(message)s")

    # Create a test alert in dry-run mode
    dispatcher = WebhookDispatcher(dry_run=True)

    test_alert = VerifiedAlert(
        tenant_id="gracko",
        timestamp=datetime(2026, 10, 13, 0, 30),
        anomaly_type=AnomalyType.RESOURCE_EXHAUSTION,
        severity=Severity.HIGH,
        anomaly_score=0.82,
        llm_confidence=0.91,
        llm_reasoning="Latency climbing while CPU stable indicates connection pool exhaustion.",
        description="Resource exhaustion: latency climbing 2.3 ms/min (R²=0.89)",
    )

    success = dispatcher.dispatch(test_alert)
    print(f"\nDispatch success: {success}")
    print(f"Summary: {json.dumps(dispatcher.get_summary(), indent=2)}")
