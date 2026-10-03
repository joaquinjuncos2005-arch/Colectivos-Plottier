import os
import time
import json
import math
import requests
from datetime import datetime
import zoneinfo
from threading import Thread, RLock
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. Cargar paradas y trazas con rutas seguras
with open(os.path.join(BASE_DIR, "paradas_optimizadas.json"), "r", encoding="utf-8") as f:
    PARADAS_RAW = json.load(f)

with open(os.path.join(BASE_DIR, "urbano y r.json"), "r", encoding="utf-8") as f:
    raw_recorridos = json.load(f)["DBCuandoLlega"]["recorridos"]

MAPA_LINEAS = {
    "1013": "50A",
    "1014": "50B",
    "1015": "50R",
    "1109": "URBANO",
    "1079": "52 CENTRO",
    "1080": "52 UNION"
}
TRAZAS_GEO = {lin: {} for lin in MAPA_LINEAS.values()}

for rec in raw_recorridos:
    cod = str(rec.get("codigoLinea"))
    lin = MAPA_LINEAS.get(cod)
    if not lin:
        continue
    # Con las nuevas cabeceras en Plottier, IDA va hacia Neuquén y VUELTA hacia Plottier
    sentido = "HACIA_NEUQUEN" if "IDA" in rec.get("bandera", "").upper() else "HACIA_PLOTTIER"
    TRAZAS_GEO[lin][sentido] = [
        [round(float(p["latitud"]), 6), round(float(p["longitud"]), 6)]
        for p in rec.get("puntos", [])
        if p.get("latitud") and p.get("longitud")
    ]

# --- Paradas clave (resaltadas en el mapa, con próximos arribos) ---------------
#    (línea, código de parada, cruce, ciudad)
PARADAS_CLAVE_CFG = [
    ("50B", "1260", "Avenida Belgrano y Pulmari - Plottier", "Plottier"),
    ("50B", "51 00011", "Avenida Belgrano y Roca - Plottier", "Plottier"),
    ("50B", "NV1043", "Santa Cruz y Av. Plottier", "Plottier"),
    ("50B", "NV1261", "Las Lajas y Martellota", "Plottier"),
    ("50B", "NV1312", "Santa Fe Sur y Riavitz - Plottier", "Plottier"),
    ("50B", "N2249", "Buenos Aires y San Juan - Neuquén", "Neuquén"),
    ("50A", "NV1015", "Buenos Aires y Talero - Neuquén", "Neuquén"),
    ("50R", "NV4996", "Bartolomé Mitre y Corrientes - Neuquén", "Neuquén"),
    ("50R", "NV5019", "Ruta 22 ETOP Terminal de Plottier", "Plottier"),
]

def construir_paradas_clave():
    por_codigo = {p[0]: p for p in PARADAS_RAW}
    resultado = []
    for linea, codigo, cruce, ciudad in PARADAS_CLAVE_CFG:
        p = por_codigo.get(codigo)
        if not p:
            print(f"[AVISO] Parada clave {codigo} no encontrada en paradas_optimizadas.json")
            continue
        lineas = p[4]
        cod_linea = lineas.get(linea)
        if not cod_linea:
            print(f"[AVISO] La parada {codigo} no tiene la línea {linea} en paradas_optimizadas.json")
            continue
        otras = {
            nom: {"cod": cod, "parada": codigo}
            for nom, cod in lineas.items() if nom != linea
        }
        resultado.append({
            "codigo": codigo,
            "linea": linea,
            "cod_linea": cod_linea,
            "cruce": cruce,
            "ciudad": ciudad,
            "lat": p[1],
            "lon": p[2],
            "otras_lineas": otras
        })
    return resultado

PARADAS_CLAVE = construir_paradas_clave()
CLAVE_POR_CODIGO = {p["codigo"]: p for p in PARADAS_CLAVE}
ARRIBOS_CLAVE = {}  # codigo -> {"arribos": [...], "ts": epoch}

# 2. Agrupar paradas que estén a menos de 35 metros
def agrupar_paradas(paradas, radio_mts=35):
    grupos = []
    for p in paradas:
        id_p, lat, lon, desc, lineas = p
        unido = False
        for g in grupos:
            d_lat = (lat - g["lat"]) * 111139
            d_lon = (lon - g["lon"]) * 111139 * 0.777
            if math.sqrt(d_lat**2 + d_lon**2) <= radio_mts:
                for lin_nom, lin_cod in lineas.items():
                    if lin_nom not in g["lineas"]:
                        g["lineas"][lin_nom] = {"cod": lin_cod, "parada": id_p}
                if id_p not in g["ids"]:
                    g["ids"].append(id_p)
                unido = True
                break
        if not unido:
            grupos.append({
                "ids": [id_p],
                "lat": lat,
                "lon": lon,
                "desc": desc,
                "lineas": {lin_nom: {"cod": lin_cod, "parada": id_p} for lin_nom, lin_cod in lineas.items()}
            })
    return [
        [" / ".join(g["ids"][:2]), g["lat"], g["lon"], g["desc"], g["lineas"]]
        for g in grupos
    ]

# Las paradas clave quedan fuera del agrupado para conservar su código y ubicación exactos
PARADAS_CLUSTERIZADAS = agrupar_paradas(
    [p for p in PARADAS_RAW if p[0] not in CLAVE_POR_CODIGO]
)

URL_WORKER = "https://radar-colectivos.gorolol.workers.dev/"

PARADAS_RADAR = [
    ("NV1244", "1013", "50A"),
    ("NV2002", "1013", "50A"),
    ("NV2258", "1014", "50B"),
    ("NV2002", "1014", "50B"),
    ("NV6043", "1015", "50R"),
    ("NV5019", "1015", "50R"),
    ("NV2258", "1109", "URBANO"),
    ("51 00012", "1109", "URBANO"),
    ("5200028", "1079", "52 CENTRO"),
    ("5200028", "1080", "52 UNION")
]

ESTADO_GLOBAL = {
    "timestamp": "--:--:--",
    "buses": {},
    "contador_ids": 1
}
LOCK_ESTADO = RLock()

def distancia_km(lat1, lon1, lat2, lon2):
    d_lat = (lat1 - lat2) * 111.0
    d_lon = (lon1 - lon2) * 111.0 * 0.777
    return math.sqrt(d_lat**2 + d_lon**2)

def normalizar_linea(linea_raw):
    lin = str(linea_raw or "").upper().strip()
    if "51" in lin or "URBANO" in lin:
        return "URBANO"
    if "50A" in lin or "50 A" in lin:
        return "50A"
    if "50B" in lin or "50 B" in lin:
        return "50B"
    if "50R" in lin or "50 R" in lin:
        return "50R"
    if "UNION" in lin:
        return "52 UNION"
    if "52" in lin:
        return "52 CENTRO"
    return lin

def deducir_sentido_real(ramal_raw, sentido_worker=""):
    """Corrige el sentido invertido que devuelve el Worker viejo."""
    txt = str(ramal_raw or "").upper()
    if "IDA" in txt:
        return "Hacia Neuquén", "HACIA_NEUQUEN"
    if "VUELTA" in txt or "VTA" in txt:
        return "Hacia Plottier", "HACIA_PLOTTIER"
    # Si viene del endpoint raíz del Worker sin nombre de bandera, invertimos el sentido del Worker
    sw = str(sentido_worker or "").upper()
    if "NEUQU" in sw:
        return "Hacia Plottier", "HACIA_PLOTTIER"
    if "PLOTTIER" in sw:
        return "Hacia Neuquén", "HACIA_NEUQUEN"
    return None, None

def procesar_nuevas_posiciones(detecciones):
    # Varios hilos (radar, paradas clave, peticiones web) escriben el mismo estado
    with LOCK_ESTADO:
        _procesar_nuevas_posiciones(detecciones)

def _procesar_nuevas_posiciones(detecciones):
    global ESTADO_GLOBAL
    ahora = time.time()

    for d in detecciones:
        try:
            lat = float(d.get("lat") or 0)
            lon = float(d.get("lon") or 0)
        except (ValueError, TypeError):
            continue
        linea = normalizar_linea(d.get("linea"))
        if not lat or not lon or not linea:
            continue

        sentido_fijo, code_fijo = deducir_sentido_real(d.get("ramal"), d.get("sentido"))

        bus_match_id = None
        min_dist = 1.8
        for b_id, b in ESTADO_GLOBAL["buses"].items():
            if b["linea"] == linea:
                dist = distancia_km(b["lat"], b["lon"], lat, lon)
                if dist < min_dist:
                    min_dist = dist
                    bus_match_id = b_id

        if bus_match_id:
            bus = ESTADO_GLOBAL["buses"][bus_match_id]
            delta_lon = lon - bus["lon"]

            # Priorizar el sentido oficial de la bandera (IDA/VUELTA) si está disponible
            if sentido_fijo:
                sentido = sentido_fijo
                sentido_code = code_fijo
            elif abs(delta_lon) > 0.0003:
                if delta_lon < 0:
                    sentido = "Hacia Plottier"
                    sentido_code = "HACIA_PLOTTIER"
                else:
                    sentido = "Hacia Neuquén"
                    sentido_code = "HACIA_NEUQUEN"
            else:
                sentido = bus["sentido"]
                sentido_code = bus["sentido_code"]

            bus.update({
                "lat": lat,
                "lon": lon,
                "ramal": d.get("ramal") or bus.get("ramal"),
                "sentido": sentido,
                "sentido_code": sentido_code,
                "tiempo_arribo": d.get("tiempo_arribo") or bus.get("tiempo_arribo"),
                "updated_at": ahora
            })
        else:
            nuevo_id = f"{linea}_{ESTADO_GLOBAL['contador_ids']}"
            ESTADO_GLOBAL["contador_ids"] += 1

            if sentido_fijo:
                sentido = sentido_fijo
                sentido_code = code_fijo
            else:
                sentido = "Hacia Plottier"
                sentido_code = "HACIA_PLOTTIER"
                if linea in TRAZAS_GEO:
                    pts_nqn = TRAZAS_GEO[linea].get("HACIA_NEUQUEN", [])
                    pts_plo = TRAZAS_GEO[linea].get("HACIA_PLOTTIER", [])
                    d_nqn = min([distancia_km(lat, lon, p[0], p[1]) for p in pts_nqn], default=99)
                    d_plo = min([distancia_km(lat, lon, p[0], p[1]) for p in pts_plo], default=99)
                    if d_nqn < d_plo:
                        sentido = "Hacia Neuquén"
                        sentido_code = "HACIA_NEUQUEN"

            ESTADO_GLOBAL["buses"][nuevo_id] = {
                "id": nuevo_id,
                "linea": linea,
                "ramal": d.get("ramal") or f"Línea {linea}",
                "sentido": sentido,
                "sentido_code": sentido_code,
                "tiempo_arribo": d.get("tiempo_arribo"),
                "lat": lat,
                "lon": lon,
                "updated_at": ahora
            }

    ESTADO_GLOBAL["buses"] = {
        k: v for k, v in ESTADO_GLOBAL["buses"].items()
        if ahora - v["updated_at"] < 95
    }

def recolector_fondo():
    global ESTADO_GLOBAL
    idx = 0
    while True:
        try:
            r = requests.get(URL_WORKER, timeout=8)
            if r.status_code == 200:
                data = r.json()
                procesar_nuevas_posiciones(data.get("buses", []))
        except Exception:
            pass

        lote = [PARADAS_RADAR[(idx + i) % len(PARADAS_RADAR)] for i in range(4)]
        idx = (idx + 4) % len(PARADAS_RADAR)

        for p_id, p_cod, p_lin in lote:
            try:
                rp = requests.get(
                    f"{URL_WORKER}parada",
                    params={"id": p_id, "cod": p_cod, "linea": p_lin},
                    timeout=6
                )
                if rp.status_code == 200:
                    arribos = rp.json().get("arribos", [])
                    procesar_nuevas_posiciones([
                        {**a, "linea": p_lin, "tiempo_arribo": a.get("tiempo")}
                        for a in arribos if a.get("lat") and a.get("lon")
                    ])
            except Exception:
                pass

        try:
            tz = zoneinfo.ZoneInfo("America/Argentina/Buenos_Aires")
            ESTADO_GLOBAL["timestamp"] = datetime.now(tz).strftime("%H:%M:%S")
        except Exception:
            ESTADO_GLOBAL["timestamp"] = time.strftime("%H:%M:%S")

        time.sleep(10)

def recolector_paradas_clave():
    """Refresca en segundo plano los arribos de las paradas clave, una por vez,
    para que el panel y los marcadores los muestren al instante."""
    idx = 0
    while True:
        if not PARADAS_CLAVE:
            time.sleep(30)
            continue
        p = PARADAS_CLAVE[idx % len(PARADAS_CLAVE)]
        idx += 1
        try:
            rp = requests.get(
                f"{URL_WORKER}parada",
                params={"id": p["codigo"], "cod": p["cod_linea"], "linea": p["linea"]},
                timeout=6
            )
            if rp.status_code == 200:
                arribos = rp.json().get("arribos", [])
                for a in arribos:
                    sent_real, _ = deducir_sentido_real(a.get("ramal"), a.get("sentido"))
                    if sent_real:
                        a["sentido"] = sent_real
                ARRIBOS_CLAVE[p["codigo"]] = {"arribos": arribos, "ts": time.time()}
                procesar_nuevas_posiciones([
                    {**a, "linea": p["linea"], "tiempo_arribo": a.get("tiempo")}
                    for a in arribos if a.get("lat") and a.get("lon")
                ])
        except Exception:
            pass
        time.sleep(3)

Thread(target=recolector_fondo, daemon=True).start()
Thread(target=recolector_paradas_clave, daemon=True).start()

@app.route('/api/parada')
def api_parada():
    p_id = request.args.get("id")
    p_cod = request.args.get("cod")
    p_linea = request.args.get("linea")

    if not p_id or not p_cod:
        return jsonify({"arribos": []})

    try:
        r = requests.get(
            f"{URL_WORKER}parada",
            params={"id": p_id, "cod": p_cod, "linea": p_linea},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            arribos = data.get("arribos", [])
            # Corregir el texto de sentido en cada arribo antes de enviarlo al navegador
            for a in arribos:
                sent_real, _ = deducir_sentido_real(a.get("ramal"), a.get("sentido"))
                if sent_real:
                    a["sentido"] = sent_real

            procesar_nuevas_posiciones([
                {**a, "linea": p_linea, "tiempo_arribo": a.get("tiempo")}
                for a in arribos if a.get("lat") and a.get("lon")
            ])

            # Si es una parada clave (con su línea clave), actualizar también su caché
            clave = CLAVE_POR_CODIGO.get(p_id)
            if clave and clave["linea"] == p_linea:
                ARRIBOS_CLAVE[p_id] = {"arribos": arribos, "ts": time.time()}
            return jsonify({"arribos": arribos})
    except Exception:
        pass
    return jsonify({"arribos": []})

@app.route('/api/paradas_clave')
def api_paradas_clave():
    ahora = time.time()
    lista = []
    for p in PARADAS_CLAVE:
        c = ARRIBOS_CLAVE.get(p["codigo"])
        lista.append({
            "codigo": p["codigo"],
            "linea": p["linea"],
            "arribos": c["arribos"] if c else None,
            "edad": int(ahora - c["ts"]) if c else None
        })
    return jsonify({"timestamp": ESTADO_GLOBAL["timestamp"], "paradas": lista})

@app.route('/api/radar')
def api_radar():
    with LOCK_ESTADO:
        buses = list(ESTADO_GLOBAL["buses"].values())
    return jsonify({
        "timestamp": ESTADO_GLOBAL["timestamp"],
        "buses": buses,
        "total_buses": len(buses)
    })

@app.route('/api/static_data')
def api_static_data():
    return jsonify({
        "trazas": TRAZAS_GEO,
        "paradas": PARADAS_CLUSTERIZADAS,
        "paradas_clave": PARADAS_CLAVE
    })

@app.route('/')
def home():
    return render_template_string(HTML_COMPLETO)

HTML_COMPLETO = r"""
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Transporte Plottier - En Vivo</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background-color: #181825; color: #CDD6F4; padding: 16px 14px 95px; }
    header { margin-bottom: 12px; }
    h1 { font-size: 22px; color: #89B4FA; font-weight: 800; }
    .sub { font-size: 13px; color: #A6ADC8; margin-top: 2px; }
    #map-container { position: relative; margin-bottom: 16px; border-radius: 14px; overflow: hidden; border: 1px solid #313244; }
    #map { height: 440px; width: 100%; background: #11111B; }
    .map-badge { position: absolute; top: 10px; left: 10px; z-index: 1000; background: rgba(24, 24, 37, 0.9); backdrop-filter: blur(6px); padding: 6px 12px; border-radius: 8px; font-size: 12px; color: #CDD6F4; border: 1px solid #313244; font-weight: 700; }
    .map-controls { position: absolute; bottom: 10px; right: 10px; z-index: 1000; display: flex; gap: 6px; }
    .btn-map-control { background: rgba(24, 24, 37, 0.9); border: 1px solid #45475A; color: #CDD6F4; font-size: 11px; font-weight: 700; padding: 6px 10px; border-radius: 8px; cursor: pointer; }
    
    .bar-fixed { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(24, 24, 37, 0.96); backdrop-filter: blur(10px); padding: 12px 16px; border-top: 1px solid #313244; display: flex; justify-content: space-between; align-items: center; z-index: 2000; }
    .btn-refresh { background: #89B4FA; color: #11111B; border: none; font-size: 14px; font-weight: 700; padding: 10px 18px; border-radius: 10px; cursor: pointer; }
    .status-text { font-size: 12px; color: #A6ADC8; }

    .bus-marker { display: flex; align-items: center; gap: 4px; padding: 3px 7px; border-radius: 8px; font-size: 11px; font-weight: 800; white-space: nowrap; box-shadow: 0 2px 6px rgba(0,0,0,0.6); }
    .bus-marker-50a { background: #1E3A24; border: 2px solid #A6E3A1; color: #A6E3A1; }
    .bus-marker-50b { background: #1E2D42; border: 2px solid #89B4FA; color: #89B4FA; }
    .bus-marker-50r { background: #38243E; border: 2px solid #CBA6F7; color: #CBA6F7; }
    .bus-marker-urbano { background: #3E3724; border: 2px solid #F9E2AF; color: #F9E2AF; }
    .bus-marker-52 { background: #3E2824; border: 2px solid #FAB387; color: #FAB387; }

    .stop-popup-title { font-size: 13px; font-weight: 800; color: #111; }
    .stop-popup-desc { font-size: 11px; color: #555; margin-bottom: 6px; }
    .btn-query-stop { background: #1E1E2E; color: #CDD6F4; border: 1px solid #45475A; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; cursor: pointer; margin: 2px; }
    .result-box { margin-top: 6px; padding-top: 4px; border-top: 1px solid #ccc; font-size: 11px; color: #111; }

    /* ---------- Paradas clave en el mapa (más pequeñas) ---------- */
    .k-50a { --c: #A6E3A1; }
    .k-50b { --c: #89B4FA; }
    .k-50r { --c: #CBA6F7; }
    .stop-key { position: relative; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; }
    .stop-key-ring { position: absolute; top: 1px; left: 1px; right: 1px; bottom: 1px; border-radius: 50%; border: 1.5px solid var(--c); animation: ringKey 2.2s ease-out infinite; }
    .stop-key-core { position: relative; width: 18px; height: 18px; border-radius: 50%; background: var(--c); color: #11111B; font-size: 11px; font-weight: 900; line-height: 1; display: flex; align-items: center; justify-content: center; border: 2px solid #11111B; box-shadow: 0 0 0 1.5px var(--c), 0 2px 6px rgba(0,0,0,0.7); }
    .stop-key-tag { position: absolute; top: 100%; left: 50%; transform: translateX(-50%); margin-top: -4px; background: #11111B; color: var(--c); border: 1px solid var(--c); border-radius: 5px; font-size: 9px; font-weight: 800; padding: 0 4px; white-space: nowrap; box-shadow: 0 2px 4px rgba(0,0,0,0.6); }
    .z-bajo .stop-key-tag { display: none; }
    @keyframes ringKey {
      0% { transform: scale(0.7); opacity: 0.9; }
      100% { transform: scale(1.3); opacity: 0; }
    }

    /* ---------- Panel de paradas clave (filas hacia abajo) ---------- */
    #panel-clave { margin-bottom: 16px; background: #1E1E2E; border: 1px solid #313244; border-radius: 14px; padding: 12px; }
    .panel-title { font-size: 15px; font-weight: 800; color: #F9E2AF; margin-bottom: 10px; }
    .panel-title small { font-size: 11px; font-weight: 700; color: #A6ADC8; margin-left: 6px; }
    .filtros { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
    .fila-filtro { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .fila-filtro .lbl { font-size: 11px; font-weight: 700; color: #A6ADC8; min-width: 52px; }
    .chip { background: #181825; color: #CDD6F4; border: 1px solid #45475A; border-radius: 999px; font-size: 12px; font-weight: 700; padding: 5px 12px; cursor: pointer; }
    .chip.active { background: #89B4FA; color: #11111B; border-color: #89B4FA; }

    #lista-clave { display: flex; flex-direction: column; gap: 8px; }
    .card-clave { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; background: #181825; border: 1px solid #313244; border-left: 4px solid var(--c); border-radius: 10px; padding: 9px 12px; cursor: pointer; }
    .card-lado { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; min-width: 64px; }
    .badge-lin { background: var(--c); color: #11111B; font-weight: 800; font-size: 12px; padding: 1px 9px; border-radius: 6px; }
    .badge-ciudad { font-size: 10px; font-weight: 800; padding: 1px 7px; border-radius: 999px; border: 1px solid; }
    .badge-ciudad.plo { color: #94E2D5; border-color: #94E2D5; }
    .badge-ciudad.nqn { color: #FAB387; border-color: #FAB387; }
    .card-info { flex: 1 1 190px; min-width: 0; }
    .card-cruce { font-size: 13px; font-weight: 700; color: #CDD6F4; }
    .card-meta { font-size: 10px; color: #A6ADC8; margin-top: 2px; }
    .card-arr { flex: 0 1 auto; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
    .arr-chip { font-size: 11px; font-weight: 800; padding: 3px 10px; border-radius: 999px; background: #313244; color: #CDD6F4; }
    .arr-chip small { font-weight: 600; opacity: 0.8; margin-left: 4px; }
    .arr-chip.ya { background: #A6E3A1; color: #11111B; }
    .arr-chip.pronto { background: #F9E2AF; color: #11111B; }
    .arr-vacio { font-size: 11px; color: #A6ADC8; }
    .btn-card-refresh { background: transparent; border: 1px solid #45475A; color: #A6ADC8; border-radius: 6px; font-size: 12px; font-weight: 700; padding: 3px 9px; cursor: pointer; }
  </style>
</head>
<body>
  <header>
    <h1>Transporte Plottier</h1>
    <p class="sub">GPS en vivo (50A, 50B, 50R, 51 Urbano y 52)</p>
  </header>

  <div id="map-container">
    <div class="map-badge" id="bus-count">Sincronizando radar...</div>
    <div class="map-controls">
      <button class="btn-map-control" id="btn-toggle-stops" onclick="toggleParadas()">📍 Ocultar Paradas</button>
      <button class="btn-map-control" id="btn-clear" onclick="limpiarRutas()" style="display:none;">✕ Quitar Recorrido</button>
    </div>
    <div id="map"></div>
  </div>

  <section id="panel-clave">
    <div class="panel-title">⭐ Paradas clave · próximos arribos <small id="cuenta-clave"></small></div>
    <div class="filtros">
      <div class="fila-filtro" id="filtro-linea">
        <span class="lbl">Línea</span>
        <button class="chip active" data-valor="TODAS">Todas</button>
        <button class="chip" data-valor="50A">50A</button>
        <button class="chip" data-valor="50B">50B</button>
        <button class="chip" data-valor="50R">50R</button>
      </div>
      <div class="fila-filtro" id="filtro-ciudad">
        <span class="lbl">Ciudad</span>
        <button class="chip active" data-valor="TODAS">Todas</button>
        <button class="chip" data-valor="Plottier">Plottier</button>
        <button class="chip" data-valor="Neuquén">Neuquén</button>
      </div>
    </div>
    <div id="lista-clave"><div class="arr-vacio">Cargando paradas clave...</div></div>
  </section>

  <div class="bar-fixed">
    <button id="btn" class="btn-refresh" onclick="pedirDatos()">Actualizar</button>
    <div class="status-text" id="status">Sincronizando...</div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    let map, capaRuta, capaParadas, capaClave;
    let marcadoresBuses = {};
    let RUTAS_GEO = {};
    let PARADAS_CLAVE = [];
    let CLAVE_POR_COD = {};
    let marcadoresClave = {};   // codigo -> marcador
    let arribosClave = {};      // codigo -> { arribos, edad }
    let filtroLinea = 'TODAS';
    let filtroCiudad = 'TODAS';
    let mostrandoParadas = true;

    function esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function claseLineaClave(l) { return l === "50A" ? "k-50a" : (l === "50R" ? "k-50r" : "k-50b"); }
    function pasaFiltroClave(p) {
      return (filtroLinea === 'TODAS' || p.linea === filtroLinea) &&
             (filtroCiudad === 'TODAS' || p.ciudad === filtroCiudad);
    }

    async function init() {
      map = L.map('map', { zoomControl: false }).setView([-38.955, -68.18], 12);
      L.control.zoom({ position: 'topright' }).addTo(map);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18 }).addTo(map);

      capaRuta = L.layerGroup().addTo(map);
      capaParadas = L.layerGroup().addTo(map);
      capaClave = L.layerGroup().addTo(map);

      // Con poco zoom se ocultan las etiquetas de las paradas clave para no amontonar el mapa
      const ajustarEtiquetas = () => map.getContainer().classList.toggle('z-bajo', map.getZoom() < 14);
      map.on('zoomend', ajustarEtiquetas);
      ajustarEtiquetas();

      const r = await fetch('/api/static_data');
      const staticData = await r.json();
      RUTAS_GEO = staticData.trazas;
      PARADAS_CLAVE = staticData.paradas_clave || [];
      PARADAS_CLAVE.forEach(p => { CLAVE_POR_COD[p.codigo] = p; });

      dibujarParadas(staticData.paradas);
      dibujarParadasClave();
      configurarPanelClave();
      renderPanelClave();
      cargarArribosClave();
      setInterval(cargarArribosClave, 9000);

      pedirDatos();
      setInterval(pedirDatos, 10000);
    }

    function dibujarParadas(paradas) {
      capaParadas.clearLayers();
      paradas.forEach(p => {
        const id = p[0], lat = p[1], lon = p[2], desc = p[3], lineas = p[4];
        let botones = '';
        for (const [linNom, info] of Object.entries(lineas)) {
          botones += `<button class="btn-query-stop" onclick="consultarParada('${info.parada}', '${info.cod}', '${linNom}')">⏱️ ${linNom}</button>`;
        }

        const marker = L.circleMarker([lat, lon], {
          radius: 4.5, color: '#FAB387', fillColor: '#F9E2AF', fillOpacity: 0.85, weight: 1.5
        });

        marker.bindPopup(`
          <div style="min-width:160px;">
            <div class="stop-popup-title">📍 ${desc}</div>
            <div class="stop-popup-desc">Ref: ${id}</div>
            <div>${botones}</div>
            <div class="result-box" style="display:none;"></div>
          </div>
        `);
        capaParadas.addLayer(marker);
      });
    }

    // ---------- Arribos: utilidades ----------
    function minutosDeTiempo(t) {
      const s = String(t == null ? '' : t).toLowerCase();
      if (/arrib|llegand|ahora|inminente|en parada/.test(s)) return 0;
      const hm = s.match(/(\d{1,2}):(\d{2})/);
      if (hm) {
        const ahora = new Date();
        let diff = (parseInt(hm[1], 10) * 60 + parseInt(hm[2], 10)) - (ahora.getHours() * 60 + ahora.getMinutes());
        if (diff < -60) diff += 1440;
        return Math.max(0, diff);
      }
      if (/seg/.test(s) && !/min/.test(s)) return 0;
      const m = s.match(/(\d+)/);
      return m ? parseInt(m[1], 10) : 999;
    }

    function ordenarArribos(arribos) {
      return (arribos || [])
        .map(a => ({ ...a, _min: minutosDeTiempo(a.tiempo) }))
        .sort((x, y) => x._min - y._min);
    }

    function sentidoCorto(s) {
      const t = String(s || '').toUpperCase();
      if (t.includes('NEUQU')) return '→ Neuquén';
      if (t.includes('PLOTTIER')) return '→ Plottier';
      return s || '';
    }

    function textoEdad(seg) {
      if (seg == null) return '';
      if (seg < 60) return `act. hace ${seg}s`;
      return `act. hace ${Math.floor(seg / 60)} min`;
    }

    // ---------- Paradas clave ----------
    function etiquetaClave(p) {
      const ar = arribosClave[p.codigo];
      let tag = p.linea;
      if (ar && ar.arribos && ar.arribos.length) {
        const prox = ordenarArribos(ar.arribos)[0]._min;
        if (prox < 999) tag += prox === 0 ? ' · ya' : ` · ${prox}'`;
      }
      return tag;
    }

    function iconoClave(p, tag) {
      return L.divIcon({
        className: 'custom-icon',
        html: `<div class="stop-key ${claseLineaClave(p.linea)}"><span class="stop-key-ring"></span><span class="stop-key-core">★</span><span class="stop-key-tag">${esc(tag)}</span></div>`,
        iconSize: [30, 30],
        iconAnchor: [15, 15],
        popupAnchor: [0, -14]
      });
    }

    function popupClave(p) {
      let otras = '';
      for (const [linNom, info] of Object.entries(p.otras_lineas || {})) {
        otras += `<button class="btn-query-stop" onclick="consultarParada('${info.parada}', '${info.cod}', '${linNom}')">⏱️ ${linNom}</button>`;
      }
      return `
        <div style="min-width:190px;">
          <div class="stop-popup-title">⭐ ${esc(p.cruce)}</div>
          <div class="stop-popup-desc">Parada ${esc(p.codigo)} · Línea clave ${esc(p.linea)}</div>
          <div>
            <button class="btn-query-stop" onclick="consultarParada('${p.codigo}', '${p.cod_linea}', '${p.linea}')">↻ ${esc(p.linea)}</button>
            ${otras}
          </div>
          <div class="result-box" style="display:none;"></div>
        </div>
      `;
    }

    function dibujarParadasClave() {
      capaClave.clearLayers();
      PARADAS_CLAVE.forEach(p => {
        const tag = etiquetaClave(p);
        const m = L.marker([p.lat, p.lon], {
          icon: iconoClave(p, tag),
          zIndexOffset: 500,
          title: `${p.cruce} (${p.codigo})`
        });
        m._tagActual = tag;
        m.bindPopup(popupClave(p));
        m.on('popupopen', async () => {
          await consultarParada(p.codigo, p.cod_linea, p.linea);
          cargarArribosClave();
        });
        marcadoresClave[p.codigo] = m;
        capaClave.addLayer(m);
      });
    }

    function actualizarIconosClave() {
      PARADAS_CLAVE.forEach(p => {
        const m = marcadoresClave[p.codigo];
        if (!m) return;
        const tag = etiquetaClave(p);
        if (tag !== m._tagActual) {
          m._tagActual = tag;
          m.setIcon(iconoClave(p, tag));
        }
      });
    }

    function aplicarFiltroClave() {
      PARADAS_CLAVE.forEach(p => {
        const m = marcadoresClave[p.codigo];
        if (!m) return;
        const visible = pasaFiltroClave(p);
        if (visible && !capaClave.hasLayer(m)) capaClave.addLayer(m);
        if (!visible && capaClave.hasLayer(m)) capaClave.removeLayer(m);
      });
      document.querySelectorAll('#filtro-linea .chip').forEach(c => {
        c.classList.toggle('active', c.dataset.valor === filtroLinea);
      });
      document.querySelectorAll('#filtro-ciudad .chip').forEach(c => {
        c.classList.toggle('active', c.dataset.valor === filtroCiudad);
      });
      renderPanelClave();
    }

    function renderPanelClave() {
      const cont = document.getElementById('lista-clave');
      const lista = PARADAS_CLAVE.filter(pasaFiltroClave);
      document.getElementById('cuenta-clave').innerText = `${lista.length} de ${PARADAS_CLAVE.length}`;
      if (lista.length === 0) {
        cont.innerHTML = '<div class="arr-vacio">No hay paradas clave con ese filtro.</div>';
        return;
      }
      cont.innerHTML = lista.map(p => {
        const ar = arribosClave[p.codigo];
        let cuerpo;
        if (!ar || ar.arribos == null) {
          cuerpo = '<span class="arr-vacio">Consultando arribos…</span>';
        } else {
          const ord = ordenarArribos(ar.arribos).slice(0, 3);
          if (ord.length === 0) {
            cuerpo = '<span class="arr-vacio">Sin arribos próximos</span>';
          } else {
            cuerpo = ord.map(a => {
              const cls = a._min === 0 ? 'ya' : (a._min <= 5 ? 'pronto' : '');
              return `<span class="arr-chip ${cls}" title="${esc(a.ramal)}">${esc(a.tiempo)}<small>${esc(sentidoCorto(a.sentido))}</small></span>`;
            }).join('');
          }
        }
        const edad = ar && ar.edad != null ? textoEdad(ar.edad) : '';
        const claseCiudad = p.ciudad === 'Neuquén' ? 'nqn' : 'plo';
        return `
          <div class="card-clave ${claseLineaClave(p.linea)}" data-cod="${esc(p.codigo)}">
            <div class="card-lado">
              <span class="badge-lin">${esc(p.linea)}</span>
              <span class="badge-ciudad ${claseCiudad}">${esc(p.ciudad)}</span>
            </div>
            <div class="card-info">
              <div class="card-cruce">${esc(p.cruce)}</div>
              <div class="card-meta">Parada ${esc(p.codigo)}${edad ? ' · ' + edad : ''}</div>
            </div>
            <div class="card-arr">${cuerpo}</div>
            <button class="btn-card-refresh" data-refresh="${esc(p.codigo)}">↻</button>
          </div>
        `;
      }).join('');
    }

    function configurarPanelClave() {
      document.getElementById('filtro-linea').addEventListener('click', e => {
        const b = e.target.closest('.chip');
        if (!b) return;
        filtroLinea = b.dataset.valor;
        aplicarFiltroClave();
      });
      document.getElementById('filtro-ciudad').addEventListener('click', e => {
        const b = e.target.closest('.chip');
        if (!b) return;
        filtroCiudad = b.dataset.valor;
        aplicarFiltroClave();
      });
      document.getElementById('lista-clave').addEventListener('click', async e => {
        const rf = e.target.closest('[data-refresh]');
        if (rf) {
          e.stopPropagation();
          const p = CLAVE_POR_COD[rf.dataset.refresh];
          if (!p) return;
          rf.innerText = '…';
          try {
            const r = await fetch(`/api/parada?id=${encodeURIComponent(p.codigo)}&cod=${encodeURIComponent(p.cod_linea)}&linea=${encodeURIComponent(p.linea)}`);
            await r.json();
          } catch (err) {}
          await cargarArribosClave();
          return;
        }
        const card = e.target.closest('.card-clave');
        if (card) enfocarParadaClave(card.dataset.cod);
      });
    }

    function enfocarParadaClave(cod) {
      const p = CLAVE_POR_COD[cod];
      const m = marcadoresClave[cod];
      if (!p || !m) return;
      document.getElementById('map-container').scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (!capaClave.hasLayer(m)) capaClave.addLayer(m);
      map.flyTo([p.lat, p.lon], 16, { duration: 0.8 });
      map.once('moveend', () => m.openPopup());
    }

    async function cargarArribosClave() {
      try {
        const r = await fetch('/api/paradas_clave');
        const d = await r.json();
        (d.paradas || []).forEach(p => {
          arribosClave[p.codigo] = { arribos: p.arribos, edad: p.edad };
        });
        renderPanelClave();
        actualizarIconosClave();
      } catch (e) {}
    }

    async function consultarParada(idParada, codLinea, linNom) {
      const box = document.querySelector('.leaflet-popup-content .result-box');
      if (box) {
        box.style.display = 'block';
        box.innerHTML = `<em>Consultando arribos de ${linNom}...</em>`;
      }

      try {
        const r = await fetch(`/api/parada?id=${encodeURIComponent(idParada)}&cod=${encodeURIComponent(codLinea)}&linea=${encodeURIComponent(linNom)}`);
        const data = await r.json();
        if (!data.arribos || data.arribos.length === 0) {
          if (box) box.innerHTML = `Sin arribos próximos para ${linNom}.`;
        } else {
          let h = '';
          data.arribos.forEach(a => {
            h += `<div style="margin-bottom:3px;"><strong>${a.ramal}</strong> (${a.sentido})<br>Arribo: <span style="color:#27ae60; font-weight:bold;">${a.tiempo}</span></div>`;
          });
          if (box) box.innerHTML = h;
          pedirDatos();
        }
      } catch (e) {
        if (box) box.innerHTML = 'Error al consultar parada.';
      }
    }

    function deslizarMarcador(marker, destLat, destLon) {
      const from = marker.getLatLng();
      const to = L.latLng(destLat, destLon);
      if (from.distanceTo(to) < 1) return;
      if (from.distanceTo(to) > 2500) { marker.setLatLng(to); return; }

      let start = null;
      const duration = 1200;

      function step(timestamp) {
        if (!start) start = timestamp;
        const progress = Math.min((timestamp - start) / duration, 1);
        const lat = from.lat + (to.lat - from.lat) * progress;
        const lon = from.lng + (to.lng - from.lng) * progress;
        marker.setLatLng([lat, lon]);
        if (progress < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    }

    function mostrarRuta(linea, sentidoCode) {
      capaRuta.clearLayers();
      if (!RUTAS_GEO[linea] || !RUTAS_GEO[linea][sentidoCode]) return;

      let color = "#89B4FA";
      if (linea === "50A") color = "#A6E3A1";
      if (linea === "50R") color = "#CBA6F7";
      if (linea === "URBANO") color = "#F9E2AF";
      if (linea.includes("52")) color = "#FAB387";

      capaRuta.addLayer(L.polyline(RUTAS_GEO[linea][sentidoCode], { color: color, weight: 5, opacity: 0.9 }));
      document.getElementById('btn-clear').style.display = 'block';
    }

    function limpiarRutas() {
      capaRuta.clearLayers();
      document.getElementById('btn-clear').style.display = 'none';
    }

    function toggleParadas() {
      const b = document.getElementById('btn-toggle-stops');
      if (mostrandoParadas) {
        map.removeLayer(capaParadas);
        b.innerText = '📍 Ver Paradas';
        mostrandoParadas = false;
      } else {
        map.addLayer(capaParadas);
        b.innerText = '📍 Ocultar Paradas';
        mostrandoParadas = true;
      }
    }

    async function pedirDatos() {
      try {
        const r = await fetch('/api/radar');
        const d = await r.json();
        document.getElementById('status').innerText = `Actualizado: ${d.timestamp}`;
        actualizarBuses(d.buses);
      } catch (e) {}
    }

    function actualizarBuses(buses) {
      const label = document.getElementById('bus-count');
      label.innerText = buses.length > 0 ? `🚌 ${buses.length} colectivos en vivo` : "Buscando unidades en recorrido...";

      const idsActivos = new Set();
      buses.forEach(b => {
        idsActivos.add(b.id);
        let clase = "bus-marker-50b";
        if (b.linea === "50A") clase = "bus-marker-50a";
        if (b.linea === "50R") clase = "bus-marker-50r";
        if (b.linea === "URBANO") clase = "bus-marker-urbano";
        if (b.linea.includes("52")) clase = "bus-marker-52";

        const icon = L.divIcon({
          className: 'custom-icon',
          html: `<div class="bus-marker ${clase}">🚌 ${b.linea}</div>`,
          iconSize: [68, 24], iconAnchor: [34, 12]
        });

        const colorSentido = b.sentido_code === 'HACIA_NEUQUEN' ? '#FAB387' : '#A6E3A1';
        const popup = `
          <div style="font-family:sans-serif; font-size:12px;">
            <strong style="font-size:14px;">Línea ${b.linea}</strong><br>
            <span style="color:${colorSentido}; font-weight:700;">${b.sentido}</span><br>
            <span style="color:#666;">${b.ramal}</span>
            <div style="color:#27ae60; font-weight:bold; margin-top:4px;">⏱️ Arribo: ${b.tiempo_arribo || 'En camino'}</div>
          </div>
        `;

        if (marcadoresBuses[b.id]) {
          deslizarMarcador(marcadoresBuses[b.id], b.lat, b.lon);
          marcadoresBuses[b.id].getPopup().setContent(popup);
          marcadoresBuses[b.id].off('click').on('click', () => mostrarRuta(b.linea, b.sentido_code));
        } else {
          // zIndexOffset: los colectivos quedan por encima de las paradas clave
          const m = L.marker([b.lat, b.lon], { icon: icon, zIndexOffset: 1000 });
          m.bindPopup(popup);
          m.on('click', () => mostrarRuta(b.linea, b.sentido_code));
          m.addTo(map);
          marcadoresBuses[b.id] = m;
        }
      });

      for (let id in marcadoresBuses) {
        if (!idsActivos.has(id)) {
          map.removeLayer(marcadoresBuses[id]);
          delete marcadoresBuses[id];
        }
      }
    }

    init();
  </script>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
