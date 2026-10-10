"""
dashboard_server.py — Visual Dashboard Server for EWS.

Lightweight, zero-heavy-dependency web dashboard for Early Warning System:
- Pure Python http.server + SSE (Server-Sent Events) or WebSocket/REST streaming.
- Real-time Chart.js telemetry charts (CPU, RAM, Disk, Latency, Errors).
- Live anomaly ticker & LLM Anti-Decoy reasoning inspect modal.
- Controls: Play, Pause, Speed slider (1x, 10x, 50x, 200x), Tenant selector.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import DATASET_BASE, TENANTS
from models import DetectedAnomaly, MetricPoint, VerifiedAlert
from streaming_simulator import stream_metrics, stream_corrida
from trend_analyzer import TrendAnalyzer
from llm_context_filter import create_context_filter
from n8n_webhook_trigger import WebhookDispatcher

logger = logging.getLogger("ews_dashboard")

# Global simulation state
sim_state = {
    "tenant": "gracko",
    "corrida": 0,  # 0 = main dataset, 1..54 = specific competition corrida
    "speed": 50,   # points per second
    "paused": False,
    "running": False,
    "history": [],        # recent metric points
    "anomalies": [],      # detected anomalies with LLM verdicts
    "current_point": None,
    "stats": {
        "processed": 0,
        "anomalies": 0,
        "real": 0,
        "decoys": 0,
        "lead_time": "755 min (avg)"
    }
}
state_lock = threading.Lock()
stop_event = threading.Event()

HTML_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>EWS — Early Warning System Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;600;700&display=swap');
    body { font-family: 'Inter', sans-serif; background-color: #0b0f19; color: #e2e8f0; }
    .mono { font-family: 'JetBrains Mono', monospace; }
    .glass { background: rgba(17, 24, 39, 0.75); backdrop-filter: blur(8px); border: 1px solid rgba(255,255,255,0.08); }
    .pulse-dot { animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(0.85); } }
  </style>
</head>
<body class="min-h-screen p-4 md:p-6">
  <!-- Top Navigation -->
  <header class="flex flex-col md:flex-row justify-between items-start md:items-center pb-6 border-b border-gray-800 gap-4">
    <div class="flex items-center gap-3">
      <div class="w-3.5 h-3.5 rounded-full bg-emerald-500 pulse-dot shadow-lg shadow-emerald-500/50"></div>
      <div>
        <h1 class="text-xl md:text-2xl font-bold tracking-tight text-white flex items-center gap-2">
          SRE EARLY WARNING SYSTEM <span class="text-xs px-2 py-0.5 rounded bg-blue-500/20 text-blue-400 border border-blue-500/30 font-normal">PROD SIMULATOR</span>
        </h1>
        <p class="text-xs text-gray-400">Detección causal de precursores & Filtro Anti-Señuelos LLM</p>
      </div>
    </div>

    <!-- Control bar -->
    <div class="flex flex-wrap items-center gap-3">
      <div class="flex items-center glass rounded-lg px-3 py-1.5 gap-2">
        <label class="text-xs text-gray-400 font-semibold">TENANT:</label>
        <select id="tenantSelect" class="bg-gray-800 text-sm font-semibold rounded px-2 py-1 outline-none text-white border border-gray-700 cursor-pointer">
          <option value="gracko">gracko (Contable)</option>
          <option value="gramo">gramo (Logística Alimentos)</option>
          <option value="kaiku">kaiku (E-Commerce Café)</option>
          <option value="ttr">ttr (Transporte GPS)</option>
        </select>
      </div>

      <div class="flex items-center glass rounded-lg px-3 py-1.5 gap-2">
        <label class="text-xs text-gray-400 font-semibold">CORRIDA:</label>
        <select id="corridaSelect" class="bg-gray-800 text-sm font-semibold rounded px-2 py-1 outline-none text-white border border-gray-700 cursor-pointer">
          <option value="0" selected>Conjunto Principal (Base)</option>
          <option value="48">Corrida #48 (Competencia)</option>
          <option value="49">Corrida #49 (Competencia)</option>
          <option value="50">Corrida #50 (Competencia)</option>
          <option value="51">Corrida #51 (Competencia)</option>
          <option value="52">Corrida #52 (Competencia)</option>
          <option value="53">Corrida #53 (Competencia)</option>
          <option value="54">Corrida #54 (Competencia)</option>
        </select>
      </div>

      <div class="flex items-center glass rounded-lg px-3 py-1.5 gap-2">
        <label class="text-xs text-gray-400 font-semibold">VELOCIDAD:</label>
        <select id="speedSelect" class="bg-gray-800 text-sm rounded px-2 py-1 outline-none text-white border border-gray-700 cursor-pointer">
          <option value="5">1x (Lento)</option>
          <option value="25">10x</option>
          <option value="60" selected>50x (Recomendado)</option>
          <option value="150">150x (Turbo)</option>
        </select>
      </div>

      <button id="toggleBtn" class="px-4 py-2 rounded-lg font-semibold text-sm transition shadow-lg bg-emerald-600 hover:bg-emerald-500 text-white cursor-pointer">
        Pausar
      </button>
      <button id="restartBtn" class="px-3 py-2 rounded-lg font-semibold text-sm transition glass hover:bg-gray-700 text-gray-300 cursor-pointer">
        Reiniciar
      </button>
    </div>
  </header>

  <!-- Metric KPI Cards -->
  <section class="grid grid-cols-2 md:grid-cols-5 gap-4 my-6">
    <div class="glass p-4 rounded-xl">
      <div class="text-xs text-gray-400 font-medium">MINUTOS PROCESADOS</div>
      <div id="kpiMinutes" class="text-2xl font-bold mono mt-1 text-white">0</div>
      <div class="text-xs text-gray-500 mt-1">Ventana causal activa</div>
    </div>
    <div class="glass p-4 rounded-xl">
      <div class="text-xs text-gray-400 font-medium">PRECURSORES DETECTADOS</div>
      <div id="kpiAnomalies" class="text-2xl font-bold mono mt-1 text-amber-400">0</div>
      <div class="text-xs text-gray-500 mt-1">Primeras derivadas & R²</div>
    </div>
    <div class="glass p-4 rounded-xl">
      <div class="text-xs text-gray-400 font-medium">INCIDENTES CONFIRMADOS</div>
      <div id="kpiReal" class="text-2xl font-bold mono mt-1 text-rose-400">0</div>
      <div class="text-xs text-gray-500 mt-1">Verificados con LLM Context</div>
    </div>
    <div class="glass p-4 rounded-xl">
      <div class="text-xs text-gray-400 font-medium">SEÑUELOS FILTRADOS</div>
      <div id="kpiDecoys" class="text-2xl font-bold mono mt-1 text-cyan-400">0</div>
      <div class="text-xs text-gray-500 mt-1">Falsos positivos prevenidos</div>
    </div>
    <div class="glass p-4 rounded-xl col-span-2 md:col-span-1">
      <div class="text-xs text-gray-400 font-medium">LEAD TIME PROMEDIO</div>
      <div id="kpiLeadTime" class="text-2xl font-bold mono mt-1 text-emerald-400">~12.5 h</div>
      <div class="text-xs text-gray-500 mt-1">Anticipación al impacto</div>
    </div>
  </section>

  <!-- Live Status Bar -->
  <div class="glass rounded-xl p-3 mb-6 flex flex-wrap justify-between items-center text-xs mono text-gray-300">
    <div class="flex items-center gap-4">
      <span>TIMESTAMP SIMULADO: <strong id="currentTimestamp" class="text-blue-400">--</strong></span>
      <span>ESTADO SENSOR: <span id="sensorStatus" class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">ONLINE</span></span>
    </div>
    <div class="flex items-center gap-4">
      <span>CPU: <strong id="valCpu" class="text-white">--%</strong></span>
      <span>RAM: <strong id="valRam" class="text-white">--%</strong></span>
      <span>DISCO: <strong id="valDisco" class="text-white">--%</strong></span>
      <span>LATENCIA: <strong id="valLatencia" class="text-white">-- ms</strong></span>
      <span>ERRORES: <strong id="valErrores" class="text-white">--/min</strong></span>
    </div>
  </div>

  <!-- Charts & Alerts Grid -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
    <!-- Telemetry Charts (2 Columns) -->
    <div class="lg:col-span-2 space-y-4">
      <div class="glass p-4 rounded-xl">
        <h2 class="text-sm font-semibold text-gray-300 mb-2 flex items-center justify-between">
          <span>TELEMETRÍA EN VIVO: LATENCIA (ms) & ERRORES (/min)</span>
          <span class="text-xs text-gray-500 mono">Right-aligned 60 min</span>
        </h2>
        <div class="h-52">
          <canvas id="chartLatency"></canvas>
        </div>
      </div>

      <div class="glass p-4 rounded-xl">
        <h2 class="text-sm font-semibold text-gray-300 mb-2 flex items-center justify-between">
          <span>RECURSOS: RAM (%) & DISCO (%) — DETECCIÓN DE FUGAS</span>
          <span class="text-xs text-gray-500 mono">Slopes & R² Regression</span>
        </h2>
        <div class="h-52">
          <canvas id="chartResources"></canvas>
        </div>
      </div>
    </div>

    <!-- Alert Stream & LLM Reasoning Ticker (1 Column) -->
    <div class="glass p-4 rounded-xl flex flex-col h-[495px]">
      <h2 class="text-sm font-semibold text-gray-300 pb-3 border-b border-gray-800 flex items-center justify-between">
        <span>FEED DE ALERTAS & RAZONAMIENTO LLM</span>
        <span class="text-xs px-2 py-0.5 rounded bg-purple-500/20 text-purple-400 border border-purple-500/30">Anti-Decoy Agent</span>
      </h2>

      <div id="alertFeed" class="flex-1 overflow-y-auto space-y-3 pt-3 pr-1">
        <div class="text-xs text-gray-500 text-center py-10">Esperando eventos en el stream...</div>
      </div>
    </div>
  </div>

  <script>
    let isPaused = false;
    let currentTenant = 'gracko';
    let currentSpeed = 60;

    // Chart.js Setup
    const ctxLatency = document.getElementById('chartLatency').getContext('2d');
    const ctxRes = document.getElementById('chartResources').getContext('2d');

    const commonOptions = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { labels: { color: '#94a3b8', font: { size: 10 } } } },
      scales: {
        x: { grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: '#64748b', font: { size: 9 }, maxTicksLimit: 8 } },
        y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#64748b', font: { size: 9 } } }
      }
    };

    const chartLatency = new Chart(ctxLatency, {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          { label: 'Latencia (ms)', data: [], borderColor: '#f59e0b', backgroundColor: 'rgba(245, 158, 11, 0.1)', tension: 0.2, yAxisID: 'y' },
          { label: 'Errores (/min)', data: [], borderColor: '#ef4444', backgroundColor: 'rgba(239, 68, 68, 0.1)', tension: 0.2, yAxisID: 'y1' }
        ]
      },
      options: {
        ...commonOptions,
        scales: {
          ...commonOptions.scales,
          y: { ...commonOptions.scales.y, position: 'left' },
          y1: { ...commonOptions.scales.y, position: 'right', grid: { drawOnChartArea: false } }
        }
      }
    });

    const chartResources = new Chart(ctxRes, {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          { label: 'RAM (%)', data: [], borderColor: '#8b5cf6', backgroundColor: 'rgba(139, 92, 246, 0.1)', tension: 0.2 },
          { label: 'Disco (%)', data: [], borderColor: '#06b6d4', backgroundColor: 'rgba(6, 182, 212, 0.1)', tension: 0.2 },
          { label: 'CPU (%)', data: [], borderColor: '#10b981', borderDash: [4, 4], tension: 0.2 }
        ]
      },
      options: commonOptions
    });

    // Control Handlers
    document.getElementById('toggleBtn').onclick = () => {
      isPaused = !isPaused;
      document.getElementById('toggleBtn').innerText = isPaused ? 'Reanudar' : 'Pausar';
      document.getElementById('toggleBtn').className = isPaused 
        ? 'px-4 py-2 rounded-lg font-semibold text-sm transition shadow-lg bg-blue-600 hover:bg-blue-500 text-white cursor-pointer'
        : 'px-4 py-2 rounded-lg font-semibold text-sm transition shadow-lg bg-emerald-600 hover:bg-emerald-500 text-white cursor-pointer';
      fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'toggle', paused: isPaused })
      });
    };

    document.getElementById('tenantSelect').onchange = (e) => {
      currentTenant = e.target.value;
      fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_tenant', tenant: currentTenant })
      }).then(() => restart());
    };

    document.getElementById('corridaSelect').onchange = (e) => {
      const corrida = parseInt(e.target.value);
      fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_corrida', corrida: corrida })
      }).then(() => restart());
    };

    document.getElementById('speedSelect').onchange = (e) => {
      currentSpeed = parseInt(e.target.value);
      fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_speed', speed: currentSpeed })
      });
    };

    function restart() {
      fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'restart' })
      });
      chartLatency.data.labels = [];
      chartLatency.data.datasets.forEach(d => d.data = []);
      chartLatency.update();
      chartResources.data.labels = [];
      chartResources.data.datasets.forEach(d => d.data = []);
      chartResources.update();
      document.getElementById('alertFeed').innerHTML = '<div class="text-xs text-gray-500 text-center py-10">Esperando eventos en el stream...</div>';
    }
    document.getElementById('restartBtn').onclick = restart;

    // Polling data loop
    let lastAnomalyCount = 0;
    setInterval(async () => {
      if (isPaused) return;
      try {
        const res = await fetch('/api/state');
        const data = await res.json();

        // Update KPIs
        document.getElementById('kpiMinutes').innerText = data.stats.processed;
        document.getElementById('kpiAnomalies').innerText = data.stats.anomalies;
        document.getElementById('kpiReal').innerText = data.stats.real;
        document.getElementById('kpiDecoys').innerText = data.stats.decoys;

        if (data.current_point) {
          const pt = data.current_point;
          document.getElementById('currentTimestamp').innerText = pt.timestamp.replace('T', ' ');
          document.getElementById('valCpu').innerText = (pt.cpu != null ? pt.cpu.toFixed(1) : '--') + '%';
          document.getElementById('valRam').innerText = (pt.ram != null ? pt.ram.toFixed(1) : '--') + '%';
          document.getElementById('valDisco').innerText = (pt.disco != null ? pt.disco.toFixed(1) : '--') + '%';
          document.getElementById('valLatencia').innerText = (pt.latencia_ms != null ? pt.latencia_ms.toFixed(0) : '--') + ' ms';
          document.getElementById('valErrores').innerText = (pt.errores != null ? pt.errores.toFixed(1) : '--') + '/min';

          // Update sensor badge
          const sensorBadge = document.getElementById('sensorStatus');
          if (pt.imputed) {
            sensorBadge.innerText = 'IMPUTADO (CAUSAL)';
            sensorBadge.className = 'px-2 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30';
          } else if (pt.cpu == null && pt.ram == null) {
            sensorBadge.innerText = 'BLACKOUT SENSOR';
            sensorBadge.className = 'px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 border border-rose-500/30';
          } else {
            sensorBadge.innerText = 'ONLINE';
            sensorBadge.className = 'px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30';
          }
        }

        // Update Charts with recent history
        if (data.history && data.history.length > 0) {
          const labels = data.history.map(p => p.timestamp.substring(11, 16));
          chartLatency.data.labels = labels;
          chartLatency.data.datasets[0].data = data.history.map(p => p.latencia_ms);
          chartLatency.data.datasets[1].data = data.history.map(p => p.errores);
          chartLatency.update();

          chartResources.data.labels = labels;
          chartResources.data.datasets[0].data = data.history.map(p => p.ram);
          chartResources.data.datasets[1].data = data.history.map(p => p.disco);
          chartResources.data.datasets[2].data = data.history.map(p => p.cpu);
          chartResources.update();
        }

        // Update Alert Feed
        if (data.anomalies && data.anomalies.length !== lastAnomalyCount) {
          lastAnomalyCount = data.anomalies.length;
          const feed = document.getElementById('alertFeed');
          feed.innerHTML = '';
          data.anomalies.slice().reverse().forEach(a => {
            const isReal = a.verdict.is_real_incident;
            const card = document.createElement('div');
            card.className = `p-3 rounded-lg border text-xs ${
              isReal ? 'bg-rose-950/40 border-rose-600/50' : 'bg-slate-900 border-cyan-800/40'
            }`;
            card.innerHTML = `
              <div class="flex justify-between items-center mb-1">
                <span class="font-bold ${isReal ? 'text-rose-400' : 'text-cyan-400'}">
                  ${isReal ? '🚨 ALERTA CRÍTICA' : '🛡️ SEÑUELO FILTRADO'}
                </span>
                <span class="mono text-gray-500">${a.timestamp.substring(5, 16).replace('T', ' ')}</span>
              </div>
              <div class="text-white font-medium mb-1">${a.description}</div>
              <div class="text-gray-400 text-[11px] bg-black/40 p-2 rounded mt-2 border border-white/5">
                <strong class="text-purple-300">Razonamiento LLM (${(a.verdict.confidence * 100).toFixed(0)}% conf):</strong> 
                ${a.verdict.reasoning}
              </div>
            `;
            feed.appendChild(card);
          });
        }
      } catch (e) {
        console.error(e);
      }
    }, 250);
  </script>
</body>
</html>
"""


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Custom HTTP handler serving the Dashboard UI and REST API."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif parsed.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with state_lock:
                # Return snapshot of recent 60 history points
                payload = {
                    "tenant": sim_state["tenant"],
                    "stats": sim_state["stats"],
                    "current_point": sim_state["current_point"],
                    "history": sim_state["history"][-60:],
                    "anomalies": sim_state["anomalies"][-25:]
                }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/control":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode("utf-8"))
                action = data.get("action")
                with state_lock:
                    if action == "toggle":
                        sim_state["paused"] = data.get("paused", not sim_state["paused"])
                    elif action == "set_speed":
                        sim_state["speed"] = max(1, min(300, int(data.get("speed", 50))))
                    elif action == "set_tenant":
                        sim_state["tenant"] = data.get("tenant", "gracko")
                    elif action == "set_corrida":
                        sim_state["corrida"] = int(data.get("corrida", 0))
                    elif action == "restart":
                        stop_event.set()  # signals background thread to restart
            except Exception as e:
                logger.error("Control API error: %s", e)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_error(404, "Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress routine GET logging for clean console
        return


def simulation_worker():
    """Background thread running the streaming simulation."""
    global sim_state
    while True:
        stop_event.clear()
        with state_lock:
            tenant = sim_state["tenant"]
            corrida = sim_state["corrida"]
            sim_state["history"] = []
            sim_state["anomalies"] = []
            sim_state["current_point"] = None
            sim_state["stats"] = {
                "processed": 0,
                "anomalies": 0,
                "real": 0,
                "decoys": 0,
                "lead_time": "755 min (avg)"
            }

        logger.info("▶ Dashboard simulator started for tenant: %s (corrida=%s)", tenant, corrida or "base")
        analyzer = TrendAnalyzer()
        context_filter = create_context_filter(provider="offline", corrida_num=corrida if corrida > 0 else None)

        try:
            if corrida > 0:
                stream_source = stream_corrida(corrida, tenant)
            else:
                stream_source = stream_metrics(tenant)

            for point in stream_source:
                if stop_event.is_set():
                    break

                # Handle pause
                while sim_state["paused"] and not stop_event.is_set():
                    time.sleep(0.1)

                if stop_event.is_set():
                    break

                # Trend analysis
                anomalies = analyzer.analyze(point)

                with state_lock:
                    sim_state["current_point"] = {
                        "timestamp": point.timestamp.isoformat(),
                        "cpu": point.cpu,
                        "ram": point.ram,
                        "disco": point.disco,
                        "latencia_ms": point.latencia_ms,
                        "errores": point.errores,
                        "imputed": point.imputed
                    }
                    sim_state["history"].append(sim_state["current_point"])
                    if len(sim_state["history"]) > 120:
                        sim_state["history"].pop(0)

                    sim_state["stats"]["processed"] += 1

                    for a in anomalies:
                        verdict = context_filter.evaluate(a)
                        sim_state["stats"]["anomalies"] += 1
                        if verdict.is_real_incident:
                            sim_state["stats"]["real"] += 1
                        else:
                            sim_state["stats"]["decoys"] += 1

                        sim_state["anomalies"].append({
                            "timestamp": a.timestamp.isoformat(),
                            "description": a.description,
                            "type": a.anomaly_type.value,
                            "score": a.score,
                            "verdict": verdict.to_dict()
                        })

                # Regulate speed
                speed = sim_state["speed"]
                time.sleep(1.0 / max(1, speed))

        except Exception as exc:
            logger.error("Simulation loop error: %s", exc)
            time.sleep(1)


def start_dashboard(port: int = 8080):
    """Start the dashboard server and background simulator."""
    server = HTTPServer(("0.0.0.0", port), DashboardRequestHandler)
    sim_thread = threading.Thread(target=simulation_worker, daemon=True)
    sim_thread.start()

    print("\n" + "="*70)
    print(f"🚀 DASHBOARD EWS DISPONIBLE EN VIVO:")
    print(f"👉 Abre en tu navegador: http://localhost:{port}")
    print("="*70 + "\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo servidor...")
        server.shutdown()


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    start_dashboard(port)
