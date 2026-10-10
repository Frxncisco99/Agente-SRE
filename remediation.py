"""Safe, explainable remediation suggestions for detected incidents."""

from __future__ import annotations


def build_remediation(anomaly_type: str, description: str = "", reasoning: str = "") -> str:
    """Return an advisory next step; no command is executed automatically."""
    suggestions = {
        "memory_leak": "Revisar el proceso que aumenta su RAM, confirmar el PID y sus logs; reiniciar solo después de validar el impacto y buscar la causa del crecimiento sostenido.",
        "disk_leak": "Revisar espacio por volumen y archivos temporales/logs grandes; rotar o comprimir logs y verificar que ningún proceso escriba sin límite.",
        "resource_exhaustion": "Revisar conexiones activas, pool de base de datos y bloqueos; comparar latencia por endpoint antes de aumentar recursos o reiniciar servicios.",
        "error_burst": "Agrupar errores por código y servicio, revisar timeouts/conexiones y confirmar si el origen es una dependencia externa antes de aplicar rollback.",
        "lora_alert": "Verificar el equipo LoRa emisor, el valor reportado y la conexión; comparar con la línea base antes de intervenir el equipo.",
    }
    return suggestions.get(anomaly_type, "Revisar métricas y logs de los últimos 15 minutos, confirmar el alcance y documentar la acción antes de modificar el servicio.")
