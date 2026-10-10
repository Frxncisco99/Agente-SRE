"""Bridge for the current LoRa serial alert protocol.

Reads lines emitted by Receptor.py / the Heltec master:
    ALERTA_SERVER|laptop_1|CPU del servidor en estado critico|85.0
    ALERTA_SERVER|laptop_2|Latencia alta|120.0

Each LoRa alert is converted into a verified Centinel alert and sent to n8n.
The emitter ID is preserved so alerts from multiple laptops can be identified
in the local dashboard.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from datetime import datetime

import requests

from config import N8N_WEBHOOK_URL, N8N_WEBHOOK_TIMEOUT
from remediation import build_remediation

logger = logging.getLogger("lora_bridge")


def parse_alert(line: str) -> dict | None:
    """Parse the new DATOS_SERVER|id|cpu|ram|disco|latencia|errores protocol."""
    parts = line.strip().split("|")
    if len(parts) < 7 or parts[0] != "DATOS_SERVER":
        return None

    try:
        source = parts[1]
        cpu, ram, disco = (float(parts[2]), float(parts[3]), float(parts[4]))
        latency = float(parts[5])
        errors = float(parts[6])
    except (ValueError, IndexError):
        logger.warning("Métrica LoRa inválida: %s", line)
        return None

    # Match the receiver's policy: notifications are suppressed during peak hours.
    now = datetime.now().astimezone()
    if 8 <= now.hour < 18:
        return None
    if cpu < 90.0 and latency <= 100.0 and errors <= 0.0:
        return None

    if cpu >= 90.0:
        anomaly_type = "resource_exhaustion"
        message = "CPU del servidor en estado crítico"
    elif latency > 100.0:
        anomaly_type = "resource_exhaustion"
        message = "Latencia del servidor fuera de rango"
    else:
        anomaly_type = "error_burst"
        message = "Errores de sistema detectados"

    description = (
        f"{source}: {message}. CPU={cpu:.1f}%, RAM={ram:.1f}%, "
        f"Disco={disco:.1f}%, Latencia={latency:.0f}ms, Errores={errors:.0f}"
    )
    remediation = build_remediation(anomaly_type, description)
    return {
        "tenant_id": "lora_real",
        "timestamp": now.isoformat(timespec="seconds"),
        "anomaly_type": anomaly_type,
        "severity": "critical",
        "anomaly_score": 1.0,
        "llm_confidence": 0.95,
        "llm_reasoning": f"Alerta confirmada por métricas LoRa de {source} fuera de horario pico.",
        "description": description,
        "remediation": remediation,
        "source_device": source,
        "temperature": None,
        "cpu": cpu,
        "ram": ram,
        "disco": disco,
        "latencia_ms": latency,
        "errores": errors,
    }


def send_n8n(payload: dict, dry_run: bool = False) -> bool:
    if dry_run:
        logger.info("[DRY RUN] %s", payload)
        return True
    try:
        response = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json", "X-EWS-Source": "lora-serial"},
            timeout=N8N_WEBHOOK_TIMEOUT,
        )
        if response.status_code in (200, 201, 202, 204):
            logger.info("Alerta LoRa enviada a n8n: %s", payload["source_device"])
            return True
        logger.error("n8n respondió HTTP %s: %s", response.status_code, response.text[:200])
    except requests.RequestException as exc:
        logger.error("No se pudo contactar n8n: %s", exc)
    return False


def update_dashboard(payload: dict) -> None:
    try:
        requests.post("http://127.0.0.1:8080/api/lora-alert", json={
            "timestamp": payload["timestamp"],
            "type": payload["anomaly_type"],
            "score": payload["anomaly_score"],
            "confidence": payload["llm_confidence"],
            "description": payload["description"],
            "reasoning": payload["llm_reasoning"],
            "remediation": payload["remediation"],
            "tenant_id": payload["tenant_id"],
            "source_device": payload["source_device"],
            "temperature": payload["temperature"],
        }, timeout=2)
    except requests.RequestException:
        logger.warning("Dashboard no disponible; la alerta se conserva en n8n.")


def run(port: str, baud: int, dry_run: bool = False, cooldown_seconds: int = 300) -> None:
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("Falta pyserial. Instala dependencias con: pip install pyserial") from exc

    logger.info("Escuchando receptor LoRa en %s a %d baudios (cooldown: %ds)", port, baud, cooldown_seconds)
    last_sent: dict[tuple[str, str], float] = {}
    try:
        with serial.Serial(port, baud, timeout=1) as connection:
            while True:
                line = connection.readline().decode("utf-8", errors="ignore").strip()
                payload = parse_alert(line)
                if payload is None:
                    continue
                dedup_key = (payload["source_device"], payload["description"])
                now = time.monotonic()
                previous = last_sent.get(dedup_key)
                if previous is not None and now - previous < cooldown_seconds:
                    logger.info("Alerta repetida suprimida: %s", payload["description"])
                    continue
                last_sent[dedup_key] = now
                logger.warning("Alerta recibida: %s", payload["description"])
                update_dashboard(payload)
                threading.Thread(target=send_n8n, args=(payload, dry_run), daemon=True).start()
    except serial.SerialException as exc:
        raise SystemExit(f"No se pudo abrir {port}: {exc}") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Puente serial LoRa → Centinel → n8n")
    parser.add_argument("--port", default="COM8", help="Puerto serial del Heltec receptor")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--dry-run", action="store_true", help="No enviar a n8n")
    parser.add_argument("--cooldown", type=int, default=300, help="Segundos entre alertas iguales por dispositivo")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    run(args.port, args.baud, args.dry_run, args.cooldown)
