"""
llm_context_filter.py — Module 3: The Anti-Decoy Agent (CORE COMPONENT).

This is the "secret weapon" for eliminating false positives. When the
trend_analyzer detects an anomaly, this module is called INSTEAD of
alerting immediately. It uses an LLM to cross-reference the anomaly
against the tenant's business context (contexto.md) to determine if
the detected pattern is:

    ✅ A REAL incident requiring immediate action, OR
    ❌ A DECOY / expected behavior (scheduled backup, payroll peak, etc.)

SUPPORTED PROVIDERS:
    - Google Gemini (via google-genai SDK)
    - OpenAI GPT (via openai SDK)

OUTPUT CONTRACT:
    The LLM is instructed to return strict JSON:
    {
        "is_real_incident": boolean,
        "confidence": float (0.0 – 1.0),
        "reasoning": "string"
    }
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    DATASET_BASE,
    GOOGLE_API_KEY,
    GOOGLE_MODEL,
    LLM_CONFIDENCE_THRESHOLD,
    LLM_MAX_RETRIES,
    LLM_PROVIDER,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    OPENAI_API_KEY,
    OPENAI_MODEL,
)
from models import DetectedAnomaly, LLMVerdict

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Context Loader
# ──────────────────────────────────────────────────────────────────────

class ContextLoader:
    """Loads and caches the business context (contexto.md) for each tenant.
    
    The context file contains business hours, scheduled maintenance windows,
    known events (payroll, promotions, backups), and system descriptions.
    This information is critical for the LLM to distinguish real incidents
    from expected behavior patterns.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or DATASET_BASE
        self._cache: dict[str, str] = {}

    def load_context(self, tenant_id: str, corrida_num: int | None = None) -> str:
        """Load the contexto.md content for a tenant.
        
        Args:
            tenant_id: Tenant identifier (e.g., "gracko").
            corrida_num: Optional corrida number. If provided, loads from
                         the corrida-specific directory.
            
        Returns:
            Full text content of contexto.md.
        """
        cache_key = f"{tenant_id}:{corrida_num}" if corrida_num else tenant_id

        if cache_key in self._cache:
            return self._cache[cache_key]

        if corrida_num is not None:
            path = self._data_dir / "corridas" / f"corrida_{corrida_num}" / tenant_id / "contexto.md"
        else:
            path = self._data_dir / tenant_id / "contexto.md"

        if not path.exists():
            logger.warning("Context file not found: %s", path)
            return f"[No context file available for tenant {tenant_id}]"

        content = path.read_text(encoding="utf-8")
        self._cache[cache_key] = content
        logger.debug("Loaded context for '%s' (%d chars)", cache_key, len(content))
        return content

    def load_logs(self, tenant_id: str, corrida_num: int | None = None) -> str:
        """Load the logs.md content for a tenant (optional enrichment).
        
        The logs file can be large, so we only load the most recent
        portion relevant to the anomaly timestamp.
        """
        if corrida_num is not None:
            path = self._data_dir / "corridas" / f"corrida_{corrida_num}" / tenant_id / "logs.md"
        else:
            path = self._data_dir / tenant_id / "logs.md"

        if not path.exists():
            return ""

        content = path.read_text(encoding="utf-8")
        return content


# ──────────────────────────────────────────────────────────────────────
# Prompt Builder
# ──────────────────────────────────────────────────────────────────────

def _extract_relevant_logs(
    full_logs: str,
    anomaly_ts: datetime,
    window_minutes: int = 60,
) -> str:
    """Extract log entries near the anomaly timestamp.
    
    Scans the logs for entries within [anomaly_ts - window, anomaly_ts]
    to provide the LLM with relevant operational context.
    Strictly causal: never includes logs after anomaly_ts.
    
    Args:
        full_logs: Complete logs.md content.
        anomaly_ts: Timestamp of the detected anomaly.
        window_minutes: How far back to look (minutes).
        
    Returns:
        Relevant log excerpt (may be empty if no logs match).
    """
    from datetime import timedelta

    if not full_logs:
        return "[No logs available]"

    window_start = anomaly_ts - timedelta(minutes=window_minutes)
    relevant_lines: list[str] = []
    in_code_block = False

    for line in full_logs.split("\n"):
        stripped = line.strip()

        # Track code block boundaries
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue

        if not in_code_block:
            continue

        # Try to extract timestamp from log line
        # Format: "2026-10-08T08:18:00 INFO ..."
        if len(stripped) >= 19 and stripped[4] == '-' and stripped[10] == 'T':
            try:
                log_ts = datetime.fromisoformat(stripped[:19])
                # Strictly causal: only include logs AT or BEFORE anomaly time
                if window_start <= log_ts <= anomaly_ts:
                    relevant_lines.append(stripped)
            except ValueError:
                continue

    if not relevant_lines:
        return "[No relevant log entries in the last 60 minutes]"

    # Limit to last 30 lines to keep prompt manageable
    if len(relevant_lines) > 30:
        relevant_lines = relevant_lines[-30:]

    return "\n".join(relevant_lines)


def build_prompt(
    anomaly: DetectedAnomaly,
    context_md: str,
    relevant_logs: str,
) -> str:
    """Build the LLM prompt for anomaly evaluation.
    
    The prompt is carefully structured to:
    1. Define the LLM's role as an SRE expert.
    2. Provide the FULL business context (contexto.md).
    3. Present the anomaly data with all trend statistics.
    4. Provide recent server logs for correlation.
    5. Ask for a strict JSON verdict.
    
    Args:
        anomaly: The detected anomaly with all trend data.
        context_md: Full content of the tenant's contexto.md.
        relevant_logs: Extracted log lines near the anomaly timestamp.
        
    Returns:
        Complete prompt string for the LLM.
    """
    anomaly_data = json.dumps(anomaly.to_dict(), indent=2, ensure_ascii=False)

    prompt = f"""You are an expert SRE (Site Reliability Engineer) analyzing server telemetry for a small business IT infrastructure. Your job is to determine whether a detected anomaly is a REAL incident that will impact customers, or a DECOY — an expected behavior pattern like a scheduled backup, payroll processing, a promotion peak, or routine maintenance.

## BUSINESS CONTEXT FOR TENANT "{anomaly.tenant_id.upper()}"

{context_md}

## DETECTED ANOMALY DATA

```json
{anomaly_data}
```

## RECENT SERVER LOGS (last 60 minutes before the anomaly)

```
{relevant_logs}
```

## YOUR TASK

Analyze the anomaly against the business context above. Consider:

1. **Scheduled events**: Does the anomaly timestamp coincide with a known scheduled event?
   - Nightly backups (respaldo nocturno) cause disk I/O and CPU spikes.
   - Payroll processing (cierre de nómina) causes ERP load.
   - Promotions / campaigns cause expected traffic spikes.
   - Database dumps (mysqldump) cause temporary CPU/disk load.
   - Inventory snapshots cause periodic load.

2. **Time of day**: Is this during business hours? After hours?
   - Some anomalies at night (backups, maintenance) are expected.
   - The same anomaly during peak hours is more concerning.

3. **Pattern shape**: A leak (steady linear growth over 30+ min) is very different from a spike (sudden jump that plateaus). Backups cause spikes, not leaks.

4. **Metric correlation**: If latency rises but CPU stays flat, it's likely a deadlock or pool exhaustion — these are almost always real incidents regardless of scheduled events.

5. **Log correlation**: Do the logs show expected activities (backup scripts, scheduled tasks) or unexpected errors (connection refused, timeout, OOM)?

## RESPONSE FORMAT

Respond with ONLY a JSON object (no markdown, no explanation outside the JSON):

{{
    "is_real_incident": true/false,
    "confidence": 0.0 to 1.0,
    "reasoning": "Brief explanation of your analysis"
}}"""

    return prompt


# ──────────────────────────────────────────────────────────────────────
# LLM Providers
# ──────────────────────────────────────────────────────────────────────

def _call_google_gemini(prompt: str) -> str:
    """Call the Google Gemini API.
    
    Uses the google-genai SDK (pip install google-genai).
    
    Returns:
        Raw LLM response text.
        
    Raises:
        RuntimeError: If the API call fails after retries.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise ImportError(
            "google-genai package not installed. Run: pip install google-genai"
        )

    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY environment variable not set")

    client = genai.Client(api_key=GOOGLE_API_KEY)

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            logger.debug("Gemini API call attempt %d/%d", attempt, LLM_MAX_RETRIES)

            response = client.models.generate_content(
                model=GOOGLE_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=LLM_TEMPERATURE,
                    max_output_tokens=500,
                    response_mime_type="application/json",
                ),
            )

            text = response.text
            if text:
                return text.strip()
            else:
                raise RuntimeError("Empty response from Gemini API")

        except Exception as exc:
            logger.warning("Gemini API attempt %d failed: %s", attempt, exc)
            if attempt < LLM_MAX_RETRIES:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise RuntimeError(
                    f"Gemini API failed after {LLM_MAX_RETRIES} attempts: {exc}"
                ) from exc

    raise RuntimeError("Unreachable")  # pragma: no cover


def _call_openai(prompt: str) -> str:
    """Call the OpenAI API.
    
    Uses the openai SDK (pip install openai).
    
    Returns:
        Raw LLM response text.
        
    Raises:
        RuntimeError: If the API call fails after retries.
    """
    try:
        import openai
    except ImportError:
        raise ImportError(
            "openai package not installed. Run: pip install openai"
        )

    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY environment variable not set")

    client = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT_SECONDS)

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            logger.debug("OpenAI API call attempt %d/%d", attempt, LLM_MAX_RETRIES)

            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an SRE expert. Always respond with valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=LLM_TEMPERATURE,
                max_tokens=500,
                response_format={"type": "json_object"},
            )

            text = response.choices[0].message.content
            if text:
                return text.strip()
            else:
                raise RuntimeError("Empty response from OpenAI API")

        except Exception as exc:
            logger.warning("OpenAI API attempt %d failed: %s", attempt, exc)
            if attempt < LLM_MAX_RETRIES:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(
                    f"OpenAI API failed after {LLM_MAX_RETRIES} attempts: {exc}"
                ) from exc

    raise RuntimeError("Unreachable")  # pragma: no cover


# ──────────────────────────────────────────────────────────────────────
# Response Parser (Robust JSON Extraction)
# ──────────────────────────────────────────────────────────────────────

def _parse_llm_response(raw_response: str) -> LLMVerdict:
    """Parse the raw LLM response into a structured LLMVerdict.
    
    Handles common LLM quirks:
    - Markdown code fences around JSON.
    - Extra text before/after the JSON object.
    - Missing or malformatted fields (falls back to safe defaults).
    
    Args:
        raw_response: Raw text from the LLM API.
        
    Returns:
        LLMVerdict with parsed or default values.
    """
    # Strip markdown code fences if present
    text = raw_response.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Try to find JSON object boundaries
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse LLM JSON response: %s\nRaw: %s", exc, raw_response[:500])
        # Conservative fallback: treat as real incident to avoid missing alerts
        return LLMVerdict(
            is_real_incident=True,
            confidence=0.5,
            reasoning=f"[PARSE ERROR] Could not parse LLM response. Defaulting to real. Raw: {raw_response[:200]}",
        )

    # Extract fields with validation
    is_real = data.get("is_real_incident")
    if isinstance(is_real, str):
        is_real = is_real.lower() in ("true", "yes", "1", "si", "sí")
    elif not isinstance(is_real, bool):
        is_real = True  # Conservative default

    confidence = data.get("confidence", 0.5)
    if isinstance(confidence, str):
        try:
            confidence = float(confidence)
        except ValueError:
            confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))

    reasoning = str(data.get("reasoning", data.get("razonamiento", "[No reasoning provided]")))

    return LLMVerdict(
        is_real_incident=bool(is_real),
        confidence=confidence,
        reasoning=reasoning,
    )


# ──────────────────────────────────────────────────────────────────────
# Main Context Filter Class
# ──────────────────────────────────────────────────────────────────────

class LLMContextFilter:
    """The Anti-Decoy Agent: filters anomalies through LLM + business context.
    
    When the TrendAnalyzer detects an anomaly, this class:
    1. Loads the tenant's business context (contexto.md).
    2. Extracts relevant recent logs.
    3. Builds a detailed prompt.
    4. Calls the LLM API (Google Gemini or OpenAI).
    5. Parses the response into a structured verdict.
    
    Usage:
        filter = LLMContextFilter()
        verdict = filter.evaluate(anomaly)
        if verdict.is_real_incident and verdict.confidence >= 0.75:
            dispatch_alert(anomaly, verdict)
    """

    def __init__(
        self,
        provider: str = LLM_PROVIDER,
        data_dir: Path | None = None,
        corrida_num: int | None = None,
    ) -> None:
        """Initialize the context filter.
        
        Args:
            provider: LLM provider ("google" or "openai").
            data_dir: Base dataset directory.
            corrida_num: Optional corrida number for competition mode.
        """
        self._provider = provider.lower()
        self._context_loader = ContextLoader(data_dir)
        self._corrida_num = corrida_num
        self._call_count = 0
        self._total_latency_ms = 0.0

        logger.info(
            "🤖 LLM Context Filter initialized (provider=%s, model=%s)",
            self._provider,
            GOOGLE_MODEL if self._provider == "google" else OPENAI_MODEL,
        )

    def evaluate(self, anomaly: DetectedAnomaly) -> LLMVerdict:
        """Evaluate a detected anomaly against business context via LLM.
        
        This is the main entry point. It builds the prompt, calls the LLM,
        and returns a structured verdict.
        
        Args:
            anomaly: The anomaly detected by the TrendAnalyzer.
            
        Returns:
            LLMVerdict with is_real_incident, confidence, and reasoning.
        """
        tenant_id = anomaly.tenant_id

        # 1. Load business context
        context_md = self._context_loader.load_context(tenant_id, self._corrida_num)

        # 2. Load and extract relevant logs
        full_logs = self._context_loader.load_logs(tenant_id, self._corrida_num)
        relevant_logs = _extract_relevant_logs(full_logs, anomaly.timestamp, window_minutes=60)

        # 3. Build the prompt
        prompt = build_prompt(anomaly, context_md, relevant_logs)
        logger.debug("Prompt length: %d chars", len(prompt))

        # 4. Call the LLM
        start_time = time.monotonic()
        try:
            if self._provider == "google":
                raw_response = _call_google_gemini(prompt)
            elif self._provider == "openai":
                raw_response = _call_openai(prompt)
            else:
                raise ValueError(f"Unknown LLM provider: {self._provider}")
        except (RuntimeError, ImportError, ValueError) as exc:
            logger.error("LLM call failed: %s", exc)
            # Conservative fallback: treat as real to avoid missing alerts
            return LLMVerdict(
                is_real_incident=True,
                confidence=0.5,
                reasoning=f"[LLM ERROR] {exc}. Defaulting to real incident.",
            )

        elapsed_ms = (time.monotonic() - start_time) * 1000
        self._call_count += 1
        self._total_latency_ms += elapsed_ms

        logger.info(
            "🤖 LLM response received in %.0f ms (call #%d, avg=%.0f ms)",
            elapsed_ms, self._call_count, self._total_latency_ms / self._call_count,
        )

        # 5. Parse the response
        verdict = _parse_llm_response(raw_response)

        logger.info(
            "🎯 [%s] Verdict: is_real=%s, confidence=%.2f — %s",
            tenant_id,
            verdict.is_real_incident,
            verdict.confidence,
            verdict.reasoning[:120],
        )

        return verdict

    @property
    def stats(self) -> dict[str, Any]:
        """Return call statistics."""
        return {
            "total_calls": self._call_count,
            "avg_latency_ms": (
                self._total_latency_ms / self._call_count
                if self._call_count > 0
                else 0
            ),
        }


# ──────────────────────────────────────────────────────────────────────
# Offline / Dry-Run Mode (for testing without LLM API)
# ──────────────────────────────────────────────────────────────────────

class OfflineContextFilter:
    """Rule-based fallback when no LLM API is available.
    
    Uses simple heuristics based on the contexto.md to filter
    obvious decoys (backups at known times, etc.). Less accurate
    than the LLM but requires no API key.
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        corrida_num: int | None = None,
    ) -> None:
        self._context_loader = ContextLoader(data_dir)
        self._corrida_num = corrida_num
        logger.info("📴 Offline Context Filter initialized (no LLM API)")

    def evaluate(self, anomaly: DetectedAnomaly) -> LLMVerdict:
        """Evaluate using rule-based heuristics."""
        context = self._context_loader.load_context(
            anomaly.tenant_id, self._corrida_num
        ).lower()
        ts = anomaly.timestamp
        hour = ts.hour
        day_of_week = ts.weekday()  # 0=Monday

        # ── Known scheduled events ──────────────────────────────
        # Nightly backups (respaldo nocturno) typically at 23:00 - 01:00
        if 22 <= hour or hour <= 2:
            if any(kw in context for kw in ["respaldo", "backup", "respaldo nocturno", "mysqldump", "reporte_rutas"]):
                if anomaly.anomaly_type.value in ("disk_leak", "error_burst"):
                    return LLMVerdict(
                        is_real_incident=False,
                        confidence=0.80,
                        reasoning=f"Señuelo filtrado: coincide con ventana de respaldo/mantenimiento nocturno programado a las {hour}:00 h según contexto.",
                    )

        # Business event: Carga masiva de declaraciones (Friday 17:00-19:30)
        if day_of_week == 4 and 16 <= hour <= 20:
            if "carga masiva de declaraciones" in context or "declaraciones" in context:
                if anomaly.anomaly_type.value in ("disk_leak", "resource_exhaustion"):
                    return LLMVerdict(
                        is_real_incident=False,
                        confidence=0.88,
                        reasoning="Señuelo filtrado: carga masiva planificada de declaraciones de clientes (Viernes 17:00–19:30 h). Comportamiento esperado.",
                    )

        # Business event: Cierre de nomina quincenal (Monday 10:00-14:00)
        if day_of_week == 0 and 10 <= hour <= 14:
            if "nomina" in context or "cierre de nomina" in context:
                if anomaly.anomaly_type.value in ("resource_exhaustion", "error_burst"):
                    return LLMVerdict(
                        is_real_incident=False,
                        confidence=0.85,
                        reasoning="Señuelo filtrado: cierre quincenal de nómina en ERP programado los lunes de 10:00 a 14:00 h. Sobrecarga benigna.",
                    )

        # Database dumps (mysqldump_diario) typically at 03:00
        if 2 <= hour <= 5:
            if any(kw in context for kw in ["mysqldump", "respaldo", "reindexado", "snapshot"]):
                if anomaly.anomaly_type.value in ("disk_leak", "memory_leak"):
                    return LLMVerdict(
                        is_real_incident=False,
                        confidence=0.75,
                        reasoning=f"Señuelo filtrado: snapshot o mysqldump periódico de base de datos a las {hour}:00 h documentado en la arquitectura.",
                    )

        # Resource exhaustion (latency ↑ + cpu stable) is almost always real
        if anomaly.anomaly_type == "resource_exhaustion":
            return LLMVerdict(
                is_real_incident=True,
                confidence=0.85,
                reasoning="Incidente Crítico Real: escalada sostenida de latencia con CPU plana indica agotamiento de pool de conexiones o contención/deadlock.",
            )

        # High-score anomalies during business hours are likely real
        if anomaly.score >= 0.8 and 7 <= hour <= 20:
            return LLMVerdict(
                is_real_incident=True,
                confidence=0.75,
                reasoning=f"Incidente Real: anomalía severa (puntuación={anomaly.score:.2f}) en horario operativo pico sin mantenimiento asociado.",
            )

        # Default: moderate confidence it's real
        return LLMVerdict(
            is_real_incident=True,
            confidence=0.60,
            reasoning="Sin eventos programados registrados en el contexto para este período. Se califica como precursor de incidente real.",
        )


# ──────────────────────────────────────────────────────────────────────
# Factory Function
# ──────────────────────────────────────────────────────────────────────

def create_context_filter(
    provider: str | None = None,
    data_dir: Path | None = None,
    corrida_num: int | None = None,
) -> LLMContextFilter | OfflineContextFilter:
    """Factory to create the appropriate context filter.
    
    If an LLM API key is configured, creates an LLMContextFilter.
    Otherwise, falls back to OfflineContextFilter with rule-based heuristics.
    
    Args:
        provider: LLM provider override ("google", "openai", or "offline").
        data_dir: Base dataset directory.
        corrida_num: Optional corrida number.
        
    Returns:
        Context filter instance.
    """
    provider = (provider or LLM_PROVIDER).lower()

    if provider == "offline":
        return OfflineContextFilter(data_dir, corrida_num)

    # Check if API key is available
    if provider == "google" and GOOGLE_API_KEY:
        return LLMContextFilter(provider, data_dir, corrida_num)
    elif provider == "openai" and OPENAI_API_KEY:
        return LLMContextFilter(provider, data_dir, corrida_num)
    else:
        logger.warning(
            "No API key found for provider '%s'. Falling back to offline mode. "
            "Set GOOGLE_API_KEY or OPENAI_API_KEY environment variable.",
            provider,
        )
        return OfflineContextFilter(data_dir, corrida_num)


# ──────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s | %(message)s")

    # Create a synthetic anomaly for testing
    test_anomaly = DetectedAnomaly(
        tenant_id="gracko",
        timestamp=datetime(2026, 10, 8, 23, 15),
        anomaly_type="disk_leak",
        severity="medium",
        score=0.72,
        description="DISCO leak detected: +5.2% over 30 min (slope=0.173/min, R²=0.85)",
        trends=[],
        raw_metrics={"disco": 63.2, "disco_slope": 0.173},
    )

    ctx_filter = create_context_filter()
    verdict = ctx_filter.evaluate(test_anomaly)

    print(f"\n{'='*60}")
    print(f"Anomaly: {test_anomaly.description}")
    print(f"Verdict: is_real={verdict.is_real_incident}, confidence={verdict.confidence:.2f}")
    print(f"Reasoning: {verdict.reasoning}")
