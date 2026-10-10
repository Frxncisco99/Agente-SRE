"""
main.py — Module 5: The Orchestrator.

Wires all EWS modules together into a complete pipeline:

    metricas.csv ──► streaming_simulator ──► trend_analyzer ──► llm_context_filter ──► n8n_webhook_trigger
                     (row-by-row)           (slope/R² analysis)  (anti-decoy LLM)      (dispatch alert)

EXECUTION MODES:
    1. Single Tenant:   python main.py --tenant gracko
    2. All Tenants:     python main.py --all
    3. Competition Run: python main.py --corrida 48 --tenant gracko
    4. Dry Run:         python main.py --tenant gracko --dry-run
    5. Offline (no LLM):python main.py --tenant gracko --offline

OUTPUT:
    - Real-time console logging of detections, verdicts, and dispatches.
    - JSON alerts log saved to output/ directory.
    - Final summary with detection statistics.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    ALERT_COOLDOWN_MINUTES,
    ANOMALY_SCORE_TRIGGER,
    DATASET_BASE,
    LLM_CONFIDENCE_THRESHOLD,
    LOG_DATE_FORMAT,
    LOG_FORMAT,
    LOG_LEVEL,
    OUTPUT_DIR,
    TENANTS,
)
from models import (
    AnomalyType,
    DetectedAnomaly,
    LLMVerdict,
    MetricPoint,
    VerifiedAlert,
)
from streaming_simulator import stream_metrics, stream_all_tenants, stream_corrida
from trend_analyzer import TrendAnalyzer
from llm_context_filter import create_context_filter, LLMContextFilter, OfflineContextFilter
from n8n_webhook_trigger import WebhookDispatcher

logger = logging.getLogger("ews")


# ──────────────────────────────────────────────────────────────────────
# Pipeline Orchestrator
# ──────────────────────────────────────────────────────────────────────

class EWSOrchestrator:
    """Early Warning System orchestrator — the main pipeline runner.
    
    Connects the four processing stages:
      1. Streaming Simulator → yields MetricPoint one-by-one
      2. Trend Analyzer → detects slope-based anomalies
      3. LLM Context Filter → eliminates decoys via business context
      4. Webhook Dispatcher → fires alerts to n8n
    
    The orchestrator processes data strictly minute-by-minute, never
    looking ahead. It maintains running statistics and produces a
    final summary report.
    """

    def __init__(
        self,
        llm_provider: str = "google",
        dry_run: bool = False,
        webhook_url: str | None = None,
        data_dir: Path | None = None,
        corrida_num: int | None = None,
        confidence_threshold: float = LLM_CONFIDENCE_THRESHOLD,
    ) -> None:
        """Initialize all pipeline components.
        
        Args:
            llm_provider: LLM provider ("google", "openai", or "offline").
            dry_run: If True, don't actually POST to n8n.
            webhook_url: Override for the n8n webhook URL.
            data_dir: Base dataset directory override.
            corrida_num: Competition corrida number (None for main data).
            confidence_threshold: Minimum LLM confidence to dispatch alert.
        """
        self._data_dir = data_dir or DATASET_BASE
        self._corrida_num = corrida_num
        self._confidence_threshold = confidence_threshold

        # ── Stage 2: Trend Analyzer ─────────────────────────────
        self._analyzer = TrendAnalyzer(
            max_window=60,
            score_trigger=ANOMALY_SCORE_TRIGGER,
            cooldown_minutes=ALERT_COOLDOWN_MINUTES,
        )

        # ── Stage 3: LLM Context Filter ────────────────────────
        self._context_filter = create_context_filter(
            provider=llm_provider,
            data_dir=self._data_dir,
            corrida_num=corrida_num,
        )

        # ── Stage 4: Webhook Dispatcher ─────────────────────────
        from config import N8N_WEBHOOK_URL
        self._dispatcher = WebhookDispatcher(
            webhook_url=webhook_url or N8N_WEBHOOK_URL,
            dry_run=dry_run,
        )

        # ── Statistics ──────────────────────────────────────────
        self._stats: dict[str, Any] = {
            "start_time": None,
            "end_time": None,
            "total_points_processed": 0,
            "total_anomalies_detected": 0,
            "total_llm_calls": 0,
            "total_real_incidents": 0,
            "total_decoys_filtered": 0,
            "total_alerts_dispatched": 0,
            "alerts_by_tenant": {},
            "alerts_by_type": {},
        }

        logger.info("=" * 70)
        logger.info("🚨 EARLY WARNING SYSTEM — Initialized")
        logger.info("   Data dir: %s", self._data_dir)
        logger.info("   Corrida:  %s", corrida_num or "main dataset")
        logger.info("   LLM:      %s", llm_provider)
        logger.info("   Dry run:  %s", dry_run)
        logger.info("   Confidence threshold: %.2f", confidence_threshold)
        logger.info("=" * 70)

    def run_single_tenant(self, tenant_id: str) -> list[VerifiedAlert]:
        """Run the full pipeline for a single tenant.
        
        Streams the tenant's metricas.csv through the complete pipeline
        and returns all verified (dispatched) alerts.
        
        Args:
            tenant_id: Tenant identifier (e.g., "gracko").
            
        Returns:
            List of VerifiedAlert objects that were dispatched.
        """
        logger.info("▶ Starting pipeline for tenant '%s'", tenant_id)

        # Select the appropriate stream source
        if self._corrida_num is not None:
            stream = stream_corrida(self._corrida_num, tenant_id, self._data_dir)
        else:
            stream = stream_metrics(tenant_id, self._data_dir / tenant_id / "metricas.csv")

        alerts: list[VerifiedAlert] = []
        point_count = 0

        for point in stream:
            point_count += 1
            self._stats["total_points_processed"] += 1

            # ── Stage 2: Trend Analysis ─────────────────────────
            anomalies = self._analyzer.analyze(point)

            for anomaly in anomalies:
                self._stats["total_anomalies_detected"] += 1

                logger.info(
                    "⚠️  [%s @ %s] Anomaly detected: %s (score=%.3f)",
                    anomaly.tenant_id,
                    anomaly.timestamp.strftime("%m-%d %H:%M"),
                    anomaly.anomaly_type.value,
                    anomaly.score,
                )

                # ── Stage 3: LLM Context Filter ─────────────────
                self._stats["total_llm_calls"] += 1
                verdict = self._context_filter.evaluate(anomaly)

                if verdict.is_real_incident and verdict.confidence >= self._confidence_threshold:
                    # ✅ REAL INCIDENT — dispatch alert
                    self._stats["total_real_incidents"] += 1

                    logger.warning(
                        "🚨 [%s @ %s] REAL INCIDENT confirmed (confidence=%.2f): %s",
                        anomaly.tenant_id,
                        anomaly.timestamp.strftime("%m-%d %H:%M"),
                        verdict.confidence,
                        verdict.reasoning[:100],
                    )

                    # ── Stage 4: Dispatch to n8n ─────────────────
                    alert = self._dispatcher.create_alert(anomaly, verdict)
                    success = self._dispatcher.dispatch(alert)

                    if success:
                        self._stats["total_alerts_dispatched"] += 1
                        alerts.append(alert)

                    # Track by tenant and type
                    self._stats["alerts_by_tenant"].setdefault(tenant_id, 0)
                    self._stats["alerts_by_tenant"][tenant_id] += 1

                    atype = anomaly.anomaly_type.value
                    self._stats["alerts_by_type"].setdefault(atype, 0)
                    self._stats["alerts_by_type"][atype] += 1

                else:
                    # ❌ DECOY / Expected behavior — filtered out
                    self._stats["total_decoys_filtered"] += 1

                    logger.info(
                        "🛡️  [%s @ %s] Decoy filtered: is_real=%s, confidence=%.2f — %s",
                        anomaly.tenant_id,
                        anomaly.timestamp.strftime("%m-%d %H:%M"),
                        verdict.is_real_incident,
                        verdict.confidence,
                        verdict.reasoning[:100],
                    )

        logger.info(
            "✅ Tenant '%s' complete: %d points processed, %d alerts dispatched",
            tenant_id, point_count, len(alerts),
        )

        return alerts

    def run_all_tenants(self, tenant_ids: list[str] | None = None) -> list[VerifiedAlert]:
        """Run the pipeline for multiple tenants sequentially.
        
        Args:
            tenant_ids: List of tenant IDs. Defaults to all 4 tenants.
            
        Returns:
            Combined list of all verified alerts across tenants.
        """
        tenant_ids = tenant_ids or TENANTS
        self._stats["start_time"] = datetime.now().isoformat()

        all_alerts: list[VerifiedAlert] = []

        for tenant_id in tenant_ids:
            try:
                alerts = self.run_single_tenant(tenant_id)
                all_alerts.extend(alerts)
            except FileNotFoundError as exc:
                logger.error("Skipping tenant '%s': %s", tenant_id, exc)
            except Exception as exc:
                logger.exception("Unexpected error for tenant '%s': %s", tenant_id, exc)

        self._stats["end_time"] = datetime.now().isoformat()

        # Print and export final summary
        self._print_summary()
        self._export_results(all_alerts)

        return all_alerts

    def run_interleaved(self, tenant_ids: list[str] | None = None) -> list[VerifiedAlert]:
        """Run all tenants interleaved in global chronological order.
        
        This simulates a real multi-tenant monitoring system where
        events from all tenants arrive in timestamp order.
        
        Args:
            tenant_ids: List of tenant IDs. Defaults to all 4 tenants.
            
        Returns:
            Combined list of all verified alerts.
        """
        tenant_ids = tenant_ids or TENANTS
        self._stats["start_time"] = datetime.now().isoformat()

        all_alerts: list[VerifiedAlert] = []

        data_dir = self._data_dir
        if self._corrida_num is not None:
            data_dir = self._data_dir / "corridas" / f"corrida_{self._corrida_num}"

        for point in stream_all_tenants(tenant_ids, data_dir):
            self._stats["total_points_processed"] += 1

            # ── Stage 2: Trend Analysis ─────────────────────────
            anomalies = self._analyzer.analyze(point)

            for anomaly in anomalies:
                self._stats["total_anomalies_detected"] += 1

                # ── Stage 3: LLM Context Filter ─────────────────
                self._stats["total_llm_calls"] += 1
                verdict = self._context_filter.evaluate(anomaly)

                if verdict.is_real_incident and verdict.confidence >= self._confidence_threshold:
                    self._stats["total_real_incidents"] += 1

                    logger.warning(
                        "🚨 [%s @ %s] REAL INCIDENT (confidence=%.2f): %s",
                        anomaly.tenant_id,
                        anomaly.timestamp.strftime("%m-%d %H:%M"),
                        verdict.confidence,
                        anomaly.description[:80],
                    )

                    alert = self._dispatcher.create_alert(anomaly, verdict)
                    success = self._dispatcher.dispatch(alert)
                    if success:
                        self._stats["total_alerts_dispatched"] += 1
                        all_alerts.append(alert)

                    self._stats["alerts_by_tenant"].setdefault(anomaly.tenant_id, 0)
                    self._stats["alerts_by_tenant"][anomaly.tenant_id] += 1
                    atype = anomaly.anomaly_type.value
                    self._stats["alerts_by_type"].setdefault(atype, 0)
                    self._stats["alerts_by_type"][atype] += 1
                else:
                    self._stats["total_decoys_filtered"] += 1

        self._stats["end_time"] = datetime.now().isoformat()
        self._print_summary()
        self._export_results(all_alerts)

        return all_alerts

    # ── Summary & Export ────────────────────────────────────────────

    def _print_summary(self) -> None:
        """Print a formatted summary of the pipeline run."""
        s = self._stats
        logger.info("")
        logger.info("=" * 70)
        logger.info("📊 EARLY WARNING SYSTEM — RUN SUMMARY")
        logger.info("=" * 70)
        logger.info("  Points processed:      %d", s["total_points_processed"])
        logger.info("  Anomalies detected:    %d", s["total_anomalies_detected"])
        logger.info("  LLM calls made:        %d", s["total_llm_calls"])
        logger.info("  ─── Results ───")
        logger.info("  Real incidents:        %d", s["total_real_incidents"])
        logger.info("  Decoys filtered:       %d", s["total_decoys_filtered"])
        logger.info("  Alerts dispatched:     %d", s["total_alerts_dispatched"])

        if s["alerts_by_tenant"]:
            logger.info("  ─── By Tenant ───")
            for tenant, count in sorted(s["alerts_by_tenant"].items()):
                logger.info("    %-12s %d alerts", tenant, count)

        if s["alerts_by_type"]:
            logger.info("  ─── By Type ───")
            for atype, count in sorted(s["alerts_by_type"].items()):
                logger.info("    %-25s %d", atype, count)

        if s["total_anomalies_detected"] > 0:
            fp_rate = s["total_decoys_filtered"] / s["total_anomalies_detected"] * 100
            logger.info("  ─── Ratios ───")
            logger.info("  Decoy filter rate:     %.1f%%", fp_rate)

        logger.info("=" * 70)

    def _export_results(self, alerts: list[VerifiedAlert]) -> None:
        """Export alerts and statistics to the output directory."""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Export alerts log
        alerts_file = OUTPUT_DIR / "alerts.json"
        self._dispatcher.export_alerts_log(str(alerts_file))

        # Export statistics
        stats_file = OUTPUT_DIR / "run_stats.json"
        with open(stats_file, "w", encoding="utf-8") as f:
            json.dump(self._stats, f, indent=2, ensure_ascii=False)
        logger.info("📝 Statistics exported to %s", stats_file)

    @property
    def stats(self) -> dict[str, Any]:
        """Current pipeline statistics."""
        return self._stats.copy()


# ──────────────────────────────────────────────────────────────────────
# CLI Argument Parser
# ──────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="🚨 Early Warning System (EWS) — Predict server crashes before they happen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  # Run for a single tenant (main dataset, online LLM)
  python main.py --tenant gracko

  # Run for all tenants with dry-run (no n8n dispatch)
  python main.py --all --dry-run

  # Run a competition corrida in offline mode (no LLM API needed)
  python main.py --corrida 48 --tenant gracko --offline

  # Run all tenants interleaved in chronological order
  python main.py --all --interleaved

  # Specify LLM provider and log level
  python main.py --tenant kaiku --llm openai --log-level DEBUG
""",
    )

    # ── Target selection ────────────────────────────────────────
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--tenant", "-t",
        type=str,
        choices=TENANTS,
        help="Run for a single tenant.",
    )
    target.add_argument(
        "--all", "-a",
        action="store_true",
        help="Run for all tenants.",
    )

    # ── Data source ─────────────────────────────────────────────
    parser.add_argument(
        "--corrida", "-c",
        type=int,
        default=None,
        help="Competition corrida number (e.g., 48). Uses main dataset if not specified.",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override dataset base directory.",
    )

    # ── Processing mode ─────────────────────────────────────────
    parser.add_argument(
        "--interleaved",
        action="store_true",
        help="Process all tenants interleaved in global chronological order.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log alerts without actually POSTing to n8n.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use rule-based filtering instead of LLM API.",
    )

    # ── LLM configuration ──────────────────────────────────────
    parser.add_argument(
        "--llm",
        type=str,
        choices=["google", "openai", "offline"],
        default=None,
        help="LLM provider (default: auto-detect from env vars).",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=LLM_CONFIDENCE_THRESHOLD,
        help=f"Minimum LLM confidence to dispatch alert (default: {LLM_CONFIDENCE_THRESHOLD}).",
    )

    # ── Webhook ─────────────────────────────────────────────────
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=None,
        help="Override n8n webhook URL.",
    )

    # ── Logging ─────────────────────────────────────────────────
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=LOG_LEVEL,
        help=f"Logging level (default: {LOG_LEVEL}).",
    )

    return parser.parse_args(argv)


# ──────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """Main entry point for the Early Warning System.
    
    Returns:
        Exit code (0 for success, 1 for errors).
    """
    args = parse_args(argv)

    # Ensure UTF-8 stdout encoding where supported to prevent cp1252 charmap errors
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # -- Configure logging ---------------------------------------
    stream_handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=[stream_handler],
    )

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # ── Determine LLM provider ──────────────────────────────────
    llm_provider = args.llm or ("offline" if args.offline else None) or "google"

    # ── Resolve data directory ──────────────────────────────────
    data_dir = Path(args.data_dir) if args.data_dir else DATASET_BASE

    # ── Create orchestrator ─────────────────────────────────────
    try:
        orchestrator = EWSOrchestrator(
            llm_provider=llm_provider,
            dry_run=args.dry_run,
            webhook_url=args.webhook_url,
            data_dir=data_dir,
            corrida_num=args.corrida,
            confidence_threshold=args.confidence,
        )
    except Exception as exc:
        logger.exception("Failed to initialize EWS: %s", exc)
        return 1

    # ── Run pipeline ────────────────────────────────────────────
    start_wall = time.monotonic()

    try:
        if args.all:
            if args.interleaved:
                alerts = orchestrator.run_interleaved()
            else:
                alerts = orchestrator.run_all_tenants()
        else:
            alerts = orchestrator.run_single_tenant(args.tenant)

            # Still print summary for single tenant runs
            orchestrator._stats["start_time"] = orchestrator._stats.get("start_time") or datetime.now().isoformat()
            orchestrator._stats["end_time"] = datetime.now().isoformat()
            orchestrator._print_summary()
            orchestrator._export_results(alerts)

    except KeyboardInterrupt:
        logger.warning("⛔ Pipeline interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("❌ Pipeline failed: %s", exc)
        return 1

    elapsed = time.monotonic() - start_wall
    logger.info("⏱️  Total wall time: %.1f seconds", elapsed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
