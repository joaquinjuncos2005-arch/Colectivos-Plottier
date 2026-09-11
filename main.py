import os
import re
import time
import urllib3
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

URL_PAGINA = "https://cuandollega.smartmovepro.net/indalo/recorridos"
URL_API = f"{URL_PAGINA}?handler=Arribos"

CONSULTAS = [
    # --- TUS PARADAS FAVORITAS ---
    {"seccion": "MIS FAVORITOS", "parada": "1260", "linea": "50B", "cod": "1014", "mostrar": True},
    {"seccion": "MIS FAVORITOS", "parada": "NV1311", "linea": "50B", "cod": "1014", "mostrar": True},
    {"seccion": "MIS FAVORITOS", "parada": "NV2316", "linea": "50B", "cod": "1014", "mostrar": True},
    # Nota: Puse "1014" provisionalmente para la 50R. Si no carga, hay que buscar su código real.
    {"seccion": "MIS FAVORITOS", "parada": "NV5019", "linea": "50R", "cod": "1014", "mostrar": True}, 

    # --- CABECERAS ORIGINALES ---
    {"seccion": "CABECERA", "parada": "NV2000", "linea": "50B", "cod": "1014", "mostrar": True},
    {"seccion": "CABECERA", "parada": "NV1014", "linea": "50A", "cod": "1013", "mostrar": True},

    # --- BARRIDO GPS ---
    {"seccion": "BARRIDO", "parada": "NV1259", "linea": "50B", "cod": "1014", "mostrar": False},
    {"seccion": "BARRIDO", "parada": "NV1058", "linea": "50B", "cod": "1014", "mostrar": False},
    {"seccion": "BARRIDO", "parada": "NV1058", "linea": "50A", "cod": "1013", "mostrar": False},
]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-419,es;q=0.9,en;q=0.8",
})
csrf_token = None

CACHE_TTL = 18
MAX_STALE_TTL = 90

ultimo_cache = {
    "timestamp": 0,
    "hora": "--:--:--",
    "items": [],
    "buses": []
}

def obtener_hora_arg():
    tz_arg = timezone(timedelta(hours=-3))
    return datetime.now(tz_arg).strftime("%H:%M:%S")

def renovar_token():
    global csrf_token, session
    session.cookies.clear()
    resp = session.get(URL_PAGINA, verify=False, timeout=10)
    resp.raise_for_status()

    tokens = re.findall(r'CfDJ8[A-Za-z0-9_\-]{80,}', resp.text)
    if tokens:
        csrf_token = tokens[0]
    else:
        m = re.search(r'__RequestVerificationToken.*?value=["\']([^"\']+)["\']', resp.text, re.IGNORECASE)
        csrf_token = m.group(1) if m else None

    if not csrf_token:
        raise Exception("No se pudo obtener el token CSRF.")

def consultar_arribos(parada, cod_linea):
    global csrf_token
    if not csrf_token:
        renovar_token()

    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": "https://cuandollega.smartmovepro.net",
        "Referer": URL_PAGINA,
        "RequestVerificationToken": csrf_token,
        "X-Requested-With": "XMLHttpRequest"
    }
    payload = {"IdentificadorParada": parada, "CodigoLinea": str(cod_linea)}

    resp = session.post(URL_API, json=payload, headers=headers, verify=False, timeout=8)
    if resp.status_code in (400, 401, 403, 500):
        time.sleep(0.3)
        renovar_token()
        headers["RequestVerificationToken"] = csrf_token
        resp = session.post(URL_API, json=payload, headers=headers, verify=False, timeout=8)

    resp.raise_for_status()
    return resp.json().get("arribos", [])

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Líneas 50A / 50B - Plottier</title>
  <meta name="theme-color" content="#181825">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background-color: #181825; color: #CDD6F4; padding: 16px 14px 95px; }
    header { margin-bottom: 12px; }
    h1 { font-size: 22px; color: #89B4FA; font-weight: 800; }
    .sub { font-size: 13px; color: #A6ADC8; margin-top: 2px; }
    #map-container { position: relative; margin-bottom: 16px; border-radius: 14px; overflow: hidden; border: 1px solid #313244; }
    #map { height: 280px; width: 100%; background: #11111B; }
    .map-badge { position: absolute; top: 10px; left: 10px; z-index: 1000; background: rgba(24, 24, 37, 0.85); padding: 4px 10px; border-radius: 8px; font-size: 11px; font-weight: 600; }
    .section-title { font-size: 14px; color: #F5E0DC; text-transform: uppercase; font-weight: 700; margin: 12px 0 8px; }
    .card { background: #1E1E2E; border: 1px solid #313244; border-radius: 14px; padding: 14px 16px; margin-bottom: 10px; }
    .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .line-tag { background: #313244; font-size: 12px; font-weight: 700; padding: 4px 8px; border-radius: 6px; }
    .line-50b { color: #89B4FA; }
    .line-50a { color: #A6E3A1; }
    .stop-tag { font-size: 12px; color: #6C7086; }
    .arrival-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-top: 1px solid #2A2B3D; }
    .time { font-size: 15px; font-weight: 700; color: #A6E3A1; }
    .branch { font-size: 13px; color: #CDD6F4; }
    .btn-focus { background: #313244; color: #CDD6F4; border: 1px solid #45475A; font-size: 11px; padding: 6px 10px; border-radius: 8px; cursor: pointer; }
    .empty { font-size: 13px; color: #A6ADC8; font-style: italic; }
    .bar-fixed { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(24, 24, 37, 0.96); padding: 12px 16px; border-top: 1px solid #313244; display: flex; justify-content: space-between; align-items: center; z-index: 2000; }
    .btn-refresh { background: #89B4FA; color: #11111B; border: none; font-size: 15px; font-weight: 700; padding: 12px 20px; border-radius: 12px; cursor: pointer; }
    .status-text { font-size: 12px; color: #A6ADC8; }
    .bus-marker { display: flex; align-items: center; gap: 3px; padding: 3px 6px; border-radius: 8px; font-size: 11px; font-weight: 800; }
    .bus-marker-50b { background-color: #1E2D42; border: 2px solid #89B4FA; color: #89B4FA; }
    .bus-marker-50a { background-color: #1E3A24; border: 2px solid #A6E3A1; color: #A6E3A1; }
  </style>
</head>
<body>
  <header>
    <h1>Transporte Plottier</h1>
    <p class="sub">Tus Favoritos + GPS en vivo</p>
  </header>
  <div id="map-container">
    <div class="map-badge" id="bus-count">Buscando coches...</div>
    <div id="map"></div>
  </div>
  <div id="contenido">Cargando paradas...</div>
  <div class="bar-fixed">
    <button id="btn" class="btn-refresh" onclick="pedirDatos()">Actualizar</button>
    <div class="status-box">
      <div id="status" class="status-text">Iniciando...</div>
      <div id="hint" style="font-size: 10px; color: #FAB387;"></div>
    </div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    let map, busLayer, cooldownTimer = null;

    function initMap() {
      map = L.map('map', { zoomControl: false }).setView([-38.955, -68.16], 12);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
      busLayer = L.layerGroup().addTo(map);
    }

    function centrarEn(lat, lon) {
      map.setView([lat, lon], 15, { animate: true });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    async function pedirDatos() {
      const btn = document.getElementById('btn');
      const status = document.getElementById('status');

      btn.disabled = true;
      status.innerText = "Consultando...";

      try {
        const res = await fetch('/api/arribos');
        const data = await res.json();

        renderTarjetas(data.items);
        actualizarMapa(data.buses);
        status.innerText = "Consulta: " + data.hora;
        btn.disabled = false;
      } catch (err) {
        status.innerText = "Error temporal";
        btn.disabled = false;
      }
    }

    function actualizarMapa(buses) {
      busLayer.clearLayers();
      document.getElementById('bus-count').innerText = buses.length > 0 ? `🚌 ${buses.length} coche(s)` : "Sin coches activos";

      buses.forEach(b => {
        const claseCss = b.linea === "50A" ? "bus-marker-50a" : "bus-marker-50b";
        const icon = L.divIcon({ className: 'custom-icon', html: `<div class="bus-marker ${claseCss}">🚌 ${b.linea}</div>`, iconSize: [52, 24] });
        L.marker([b.lat, b.lon], { icon: icon }).bindPopup(`<b>Línea ${b.linea}</b><br>${b.ramal}<br>${b.tiempo}`).addTo(busLayer);
      });
    }

    function renderTarjetas(items) {
      let html = '';
      let secActual = '';

      items.forEach(item => {
        if (item.seccion !== secActual) {
          secActual = item.seccion;
          html += `<div class="section-title">📍 ${secActual}</div>`;
        }

        html += `<div class="card"><div class="card-header"><span class="line-tag line-50b">Línea ${item.linea}</span><span class="stop-tag">Parada ${item.parada}</span></div>`;

        if (!item.arribos || item.arribos.length === 0) {
          html += `<div class="empty">Sin unidades reportando</div>`;
        } else {
          item.arribos.forEach(c => {
            const btn = (c.lat && c.lon) ? `<button class="btn-focus" onclick="centrarEn(${c.lat}, ${c.lon})">Mapa</button>` : '';
            html += `<div class="arrival-row"><div><div class="branch">${c.ramal}</div><div class="time">• ${c.tiempo}</div></div>${btn}</div>`;
          });
        }
        html += `</div>`;
      });
      document.getElementById('contenido').innerHTML = html;
    }

    initMap();
    pedirDatos();
  </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/arribos')
def api_arribos():
    global ultimo_cache
    ahora = time.time()
    antiguedad = ahora - ultimo_cache["timestamp"]

    if antiguedad < CACHE_TTL and ultimo_cache["items"]:
        return jsonify({"hora": ultimo_cache["hora"], "items": ultimo_cache["items"], "buses": ultimo_cache["buses"], "from_cache": True})

    resultados_visibles = []
    buses_detectados = {}
    consultas_exitosas = 0

    for item in CONSULTAS:
        try:
            arribos_raw = consultar_arribos(item["parada"], item["cod"])
            arribos_limpios = []

            for c in arribos_raw:
                lat, lon, tiempo, ramal = c.get("latitud"), c.get("longitud"), c.get("tiempoRestanteArribo", "Sin datos"), c.get("descripcionBandera", item["linea"])

                if item["mostrar"]:
                    arribos_limpios.append({"tiempo": tiempo, "ramal": ramal, "lat": lat, "lon": lon})

                if lat and lon and str(lat).strip() and str(lon).strip():
                    coord_key = f"{round(float(lat), 3)}_{round(float(lon), 3)}"
                    if coord_key not in buses_detectados:
                        buses_detectados[coord_key] = {"linea": item["linea"], "ramal": ramal, "tiempo": tiempo, "lat": float(lat), "lon": float(lon)}

            if item["mostrar"]:
                resultados_visibles.append({**item, "arribos": arribos_limpios})

            consultas_exitosas += 1
            time.sleep(0.25)
        except Exception:
            if item["mostrar"]:
                datos_previos = next((x["arribos"] for x in ultimo_cache["items"] if x["parada"] == item["parada"] and x["linea"] == item["linea"]), [])
                resultados_visibles.append({**item, "arribos": datos_previos if antiguedad < MAX_STALE_TTL else []})

    hora_actual = obtener_hora_arg()
    lista_buses = list(buses_detectados.values())

    if consultas_exitosas > 0 or antiguedad >= MAX_STALE_TTL:
        ultimo_cache.update({"timestamp": ahora, "hora": hora_actual, "items": resultados_visibles, "buses": lista_buses})
        return jsonify({"hora": hora_actual, "items": resultados_visibles, "buses": lista_buses, "from_cache": False})

    return jsonify({"hora": ultimo_cache["hora"], "items": ultimo_cache["items"], "buses": ultimo_cache["buses"], "from_cache": True})

if __name__ == '__main__':
  port = int(os.environ.get("PORT", 5000))
  app.run(host='0.0.0.0', port=port)