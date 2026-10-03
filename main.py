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
    _PARADAS_CRUDAS = json.load(f)

# El archivo de recorridos puede llamarse "urbano y r.json" (original) o "urbano_y_r.json"
_RUTA_RECORRIDOS = os.path.join(BASE_DIR, "urbano y r.json")
if not os.path.exists(_RUTA_RECORRIDOS):
    _RUTA_RECORRIDOS = os.path.join(BASE_DIR, "urbano_y_r.json")

with open(_RUTA_RECORRIDOS, "r", encoding="utf-8") as f:
    raw_recorridos = json.load(f)["DBCuandoLlega"]["recorridos"]

# Línea URBANO (1109) eliminada por completo
MAPA_LINEAS = {
    "1013": "50A",
    "1014": "50B",
    "1015": "50R",
    "1079": "52 CENTRO",
    "1080": "52 UNION"
}
LINEA_COD = {nom: cod for cod, nom in MAPA_LINEAS.items()}
LINEAS_ACTIVAS = set(MAPA_LINEAS.values())

# Paradas: se descartan referencias a líneas que ya no se usan (URBANO) y
# las paradas que quedaron sin ninguna línea activa.
PARADAS_RAW = []
for _p in _PARADAS_CRUDAS:
    _lineas = {k: v for k, v in _p[4].items() if k in LINEAS_ACTIVAS}
    if _lineas:
        PARADAS_RAW.append([_p[0], _p[1], _p[2], _p[3], _lineas])

# --- Limpieza de trazas GPS (mejora del trazado) ---------------------------
def _dist_m(a, b):
    return math.hypot((a[0] - b[0]) * 111139, (a[1] - b[1]) * 111139 * 0.777)

def _dist_punto_segmento_m(p, a, b):
    px, py = p[1] * 111139 * 0.777, p[0] * 111139
    ax, ay = a[1] * 111139 * 0.777, a[0] * 111139
    bx, by = b[1] * 111139 * 0.777, b[0] * 111139
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

def _douglas_peucker(pts, tol_m):
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    pila = [(0, len(pts) - 1)]
    while pila:
        i, j = pila.pop()
        max_d, max_k = 0.0, None
        for k in range(i + 1, j):
            d = _dist_punto_segmento_m(pts[k], pts[i], pts[j])
            if d > max_d:
                max_d, max_k = d, k
        if max_k is not None and max_d > tol_m:
            keep[max_k] = True
            pila.append((i, max_k))
            pila.append((max_k, j))
    return [p for p, k in zip(pts, keep) if k]

def limpiar_traza(pts, dup_m=3.0, tol_m=2.0):
    """Quita puntos repetidos, picos de GPS (zigzags aislados) y puntos
    redundantes alineados, sin alterar la forma real del recorrido."""
    if len(pts) < 3:
        return pts
    # 1. Duplicados / puntos casi idénticos consecutivos
    out = []
    for p in pts:
        if not out or _dist_m(out[-1], p) >= dup_m:
            out.append(p)
    if out[-1] != pts[-1]:
        out[-1] = pts[-1]
    # 2. Picos aislados: el punto se aleja y vuelve casi al mismo lugar
    cambio = True
    while cambio and len(out) >= 3:
        cambio = False
        for i in range(1, len(out) - 1):
            d1 = _dist_m(out[i - 1], out[i])
            d2 = _dist_m(out[i], out[i + 1])
            if min(d1, d2) > 30 and _dist_m(out[i - 1], out[i + 1]) < 0.35 * min(d1, d2):
                del out[i]
                cambio = True
                break
    # 3. Puntos colineales redundantes (tolerancia de 2 metros)
    return _douglas_peucker(out, tol_m)

TRAZAS_GEO = {lin: {} for lin in MAPA_LINEAS.values()}

for rec in raw_recorridos:
    cod = str(rec.get("codigoLinea"))
    lin = MAPA_LINEAS.get(cod)
    if not lin:
        continue
    # Con las nuevas cabeceras en Plottier, IDA va hacia Neuquén y VUELTA hacia Plottier
    sentido = "HACIA_NEUQUEN" if "IDA" in rec.get("bandera", "").upper() else "HACIA_PLOTTIER"
    puntos = [
        [round(float(p["latitud"]), 6), round(float(p["longitud"]), 6)]
        for p in rec.get("puntos", [])
        if p.get("latitud") and p.get("longitud")
    ]
    TRAZAS_GEO[lin][sentido] = limpiar_traza(puntos)

# Cabeceras conocidas donde los colectivos apagan el GPS al terminar el recorrido
CABECERAS_GEO = [
    (-38.9314, -68.2494, "Cabecera Plottier Norte"),
    (-38.9345, -68.2585, "Cabecera Casa de Té"),
    (-38.9302, -68.2585, "Cabecera Aristóbulo del Valle"),
    (-38.9607, -68.2491, "Cabecera Plottier Sur"),
    (-38.9645, -68.2433, "Cabecera Zabaleta"),
    (-38.9562, -68.2176, "Terminal ETOP Plottier"),
    (-38.9561, -68.3385, "Cabecera Las Lilas / 50R"),
    (-38.9840, -68.3494, "Cabecera B° Las Perlas"),
    (-38.9822, -68.3070, "Cabecera Las Perlas Este"),
    (-38.9461, -68.0575, "Cabecera Neuquén Centro"),
    (-38.9571, -68.0562, "Cabecera Neuquén Parque Central")
]

# 2. Paradas clave (se resaltan en el mapa y muestran próximos arribos)
#    (línea, código de parada, cruce)
PARADAS_CLAVE_CFG = [
    ("50B", "1260", "Avenida Belgrano y Pulmari - Plottier"),
    ("50B", "51 00011", "Avenida Belgrano y Roca - Plottier"),
    ("50B", "NV1043", "Santa Cruz y Av. Plottier"),
    ("50B", "NV1261", "Las Lajas y Martellota"),
    ("50B", "NV1312", "Santa Fe Sur y Riavitz - Plottier"),
    ("50B", "N2249", "Buenos Aires y San Juan - Neuquén"),
    ("50A", "NV1015", "Buenos Aires y Talero - Neuquén"),
    ("50R", "NV4996", "Bartolomé Mitre y Corrientes - Neuquén"),
    ("50R", "NV5019", "Ruta 22 ETOP Terminal de Plottier"),
]

def construir_paradas_clave():
    por_codigo = {p[0]: p for p in PARADAS_RAW}
    resultado = []
    for linea, codigo, cruce in PARADAS_CLAVE_CFG:
        p = por_codigo.get(codigo)
        if not p:
            print(f"[AVISO] Parada clave {codigo} no encontrada en paradas_optimizadas.json")
            continue
        lineas = p[4]
        cod_linea = lineas.get(linea) or LINEA_COD.get(linea)
        otras = {
            nom: {"cod": cod, "parada": codigo}
            for nom, cod in lineas.items() if nom != linea
        }
        resultado.append({
            "codigo": codigo,
            "linea": linea,
            "cod_linea": cod_linea,
            "cruce": cruce,
            "lat": p[1],
            "lon": p[2],
            "otras_lineas": otras
        })
    return resultado

PARADAS_CLAVE = construir_paradas_clave()
CLAVE_POR_CODIGO = {p["codigo"]: p for p in PARADAS_CLAVE}
ARRIBOS_CLAVE = {}  # codigo -> {"arribos": [...], "ts": epoch}

# 3. Agrupar paradas que estén a menos de 35 metros (las paradas clave quedan
#    fuera del agrupado para conservar su código y ubicación exactos)
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

PARADAS_CLUSTERIZADAS = agrupar_paradas(
    [p for p in PARADAS_RAW if p[0] not in CLAVE_POR_CODIGO]
)

URL_WORKER = "https://radar-colectivos.gorolol.workers.dev/"

# Paradas terminales y estratégicas para capturar todas las unidades en ambos sentidos
PARADAS_RADAR = [
    ("NV1102", "1013", "50A"),
    ("NV1244", "1013", "50A"),
    ("NV1012", "1013", "50A"),
    ("NV4120", "1014", "50B"),
    ("NV2249", "1014", "50B"),
    ("NV2258", "1014", "50B"),
    ("NV 4998", "1015", "50R"),
    ("NV6000", "1015", "50R"),
    ("NV5019", "1015", "50R"),
    ("5200001", "1079", "52 CENTRO"),
    ("5200047", "1079", "52 CENTRO"),
    ("5200001", "1080", "52 UNION"),
    ("5200047", "1080", "52 UNION")
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

def detectar_cabecera(lat, lon):
    for c_lat, c_lon, c_nom in CABECERAS_GEO:
        if distancia_km(lat, lon, c_lat, c_lon) <= 0.35:
            return c_nom
    return None

def normalizar_linea(linea_raw):
    lin = str(linea_raw or "").upper().strip()
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
    # Cualquier otra línea (incluida URBANO / 51) se descarta
    return None

def deducir_sentido_real(ramal_raw, sentido_worker=""):
    txt = str(ramal_raw or "").upper()
    if "IDA" in txt:
        return "Hacia Neuquén", "HACIA_NEUQUEN"
    if "VUELTA" in txt or "VTA" in txt:
        return "Hacia Plottier", "HACIA_PLOTTIER"
    sw = str(sentido_worker or "").upper()
    if "NEUQU" in sw:
        return "Hacia Plottier", "HACIA_PLOTTIER"
    if "PLOTTIER" in sw:
        return "Hacia Neuquén", "HACIA_NEUQUEN"
    return None, None

def procesar_nuevas_posiciones(detecciones):
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
        if not lat or not lon or not linea or linea not in LINEAS_ACTIVAS:
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
            dist_avance = distancia_km(bus["lat"], bus["lon"], lat, lon)
            delta_lon = lon - bus["lon"]
            dt = max(ahora - bus["last_gps_at"], 1.0)

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

            # Si el GPS realmente reportó una coordenada distinta (> 12 metros)
            if dist_avance > 0.012:
                vel_calc = (dist_avance / dt) * 3600.0
                vel_kmh = round(max(18.0, min(vel_calc, 58.0)), 1)
                bus.update({
                    "prev_lat": bus["lat"],
                    "prev_lon": bus["lon"],
                    "lat": lat,
                    "lon": lon,
                    "vel_kmh": vel_kmh,
                    "last_gps_at": ahora
                })
            else:
                # Misma coordenada pero la API confirma que sigue activo
                if ahora - bus["last_gps_at"] < 40:
                    bus["last_gps_at"] = ahora - 10

            bus.update({
                "ramal": d.get("ramal") or bus.get("ramal"),
                "sentido": sentido,
                "sentido_code": sentido_code,
                "cabecera": detectar_cabecera(lat, lon),
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
                "lat": lat,
                "lon": lon,
                "prev_lat": lat,
                "prev_lon": lon,
                "vel_kmh": 34.0,
                "cabecera": detectar_cabecera(lat, lon),
                "last_gps_at": ahora,
                "updated_at": ahora
            }

    # Mantener unidades hasta 6 minutos (360s) para mostrar "Señal débil" y "+3 min posible avería"
    ESTADO_GLOBAL["buses"] = {
        k: v for k, v in ESTADO_GLOBAL["buses"].items()
        if ahora - v["last_gps_at"] < 360
    }

def consultar_arribos_parada(p_id, p_cod, p_lin, timeout=6):
    """Consulta los arribos de una parada al worker. Devuelve la lista de
    arribos (normalizando el sentido) o None si la consulta falló."""
    try:
        rp = requests.get(
            f"{URL_WORKER}parada",
            params={"id": p_id, "cod": p_cod, "linea": p_lin},
            timeout=timeout
        )
        if rp.status_code != 200:
            return None
        arribos = rp.json().get("arribos", [])
        for a in arribos:
            sent_real, _ = deducir_sentido_real(a.get("ramal"), a.get("sentido"))
            if sent_real:
                a["sentido"] = sent_real
        procesar_nuevas_posiciones([
            {**a, "linea": p_lin}
            for a in arribos if a.get("lat") and a.get("lon")
        ])
        return arribos
    except Exception:
        return None

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

        lote = [PARADAS_RADAR[(idx + i) % len(PARADAS_RADAR)] for i in range(5)]
        idx = (idx + 5) % len(PARADAS_RADAR)

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
                        {**a, "linea": p_lin}
                        for a in arribos if a.get("lat") and a.get("lon")
                    ])
            except Exception:
                pass

        try:
            tz = zoneinfo.ZoneInfo("America/Argentina/Buenos_Aires")
            ESTADO_GLOBAL["timestamp"] = datetime.now(tz).strftime("%H:%M:%S")
        except Exception:
            ESTADO_GLOBAL["timestamp"] = time.strftime("%H:%M:%S")

        time.sleep(9)

def recolector_paradas_clave():
    """Refresca en segundo plano los arribos de las paradas clave, una por una,
    para que la interfaz los muestre al instante sin consultar al worker."""
    idx = 0
    while True:
        if not PARADAS_CLAVE:
            time.sleep(30)
            continue
        p = PARADAS_CLAVE[idx % len(PARADAS_CLAVE)]
        idx += 1
        arribos = consultar_arribos_parada(p["codigo"], p["cod_linea"], p["linea"])
        if arribos is not None:
            ARRIBOS_CLAVE[p["codigo"]] = {"arribos": arribos, "ts": time.time()}
        time.sleep(3)

Thread(target=recolector_fondo, daemon=True).start()
Thread(target=recolector_paradas_clave, daemon=True).start()

@app.route('/api/parada')
def api_parada():
    p_id = request.args.get("id")
    p_cod = request.args.get("cod")
    p_linea = request.args.get("linea")

    if not p_id or not p_cod or p_linea not in LINEAS_ACTIVAS:
        return jsonify({"arribos": []})

    arribos = consultar_arribos_parada(p_id, p_cod, p_linea, timeout=8)
    if arribos is None:
        return jsonify({"arribos": []})

    clave = CLAVE_POR_CODIGO.get(p_id)
    if clave and clave["linea"] == p_linea:
        ARRIBOS_CLAVE[p_id] = {"arribos": arribos, "ts": time.time()}
    return jsonify({"arribos": arribos})

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
    ahora = time.time()
    lista = []
    with LOCK_ESTADO:
        buses = list(ESTADO_GLOBAL["buses"].values())
    for b in buses:
        edad = int(max(0, ahora - b["last_gps_at"]))
        lista.append({**b, "edad_senal": edad})
    return jsonify({
        "timestamp": ESTADO_GLOBAL["timestamp"],
        "buses": lista,
        "total_buses": len(lista)
    })

@app.route('/api/static_data')
def api_static_data():
    return jsonify({
        "trazas": TRAZAS_GEO,
        "paradas": PARADAS_CLUSTERIZADAS,
        "paradas_raw": PARADAS_RAW,
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
    #map { height: 460px; width: 100%; background: #11111B; }
    .map-badge { position: absolute; top: 10px; left: 10px; z-index: 1000; background: rgba(24, 24, 37, 0.92); backdrop-filter: blur(6px); padding: 6px 12px; border-radius: 8px; font-size: 12px; color: #CDD6F4; border: 1px solid #313244; font-weight: 700; }
    .map-controls { position: absolute; bottom: 10px; right: 10px; z-index: 1000; display: flex; gap: 6px; }
    .btn-map-control { background: rgba(24, 24, 37, 0.92); border: 1px solid #45475A; color: #CDD6F4; font-size: 11px; font-weight: 700; padding: 6px 10px; border-radius: 8px; cursor: pointer; }
    
    .bar-fixed { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(24, 24, 37, 0.96); backdrop-filter: blur(10px); padding: 12px 16px; border-top: 1px solid #313244; display: flex; justify-content: space-between; align-items: center; z-index: 2000; }
    .btn-refresh { background: #89B4FA; color: #11111B; border: none; font-size: 14px; font-weight: 700; padding: 10px 18px; border-radius: 10px; cursor: pointer; }
    .status-text { font-size: 12px; color: #A6ADC8; }

    .bus-marker { display: flex; align-items: center; justify-content: center; gap: 4px; padding: 3px 7px; border-radius: 8px; font-size: 11px; font-weight: 800; white-space: nowrap; box-shadow: 0 2px 6px rgba(0,0,0,0.65); position: relative; }
    .bus-marker-50a { background: #1E3A24; border: 2px solid #A6E3A1; color: #A6E3A1; }
    .bus-marker-50b { background: #1E2D42; border: 2px solid #89B4FA; color: #89B4FA; }
    .bus-marker-50r { background: #38243E; border: 2px solid #CBA6F7; color: #CBA6F7; }
    .bus-marker-52 { background: #3E2824; border: 2px solid #FAB387; color: #FAB387; }

    /* Estados visuales de señal / avería / cabecera */
    .bus-weak { border-style: dashed !important; border-color: #F9E2AF !important; animation: pulseWeak 1.6s infinite; }
    .bus-stalled { background: #3B1D26 !important; border-color: #F38BA8 !important; color: #F38BA8 !important; opacity: 0.88; }
    .bus-cabecera { opacity: 0.72; border-style: dotted !important; }

    @keyframes pulseWeak {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.6; }
    }

    .stop-popup-title { font-size: 13px; font-weight: 800; color: #111; }
    .stop-popup-desc { font-size: 11px; color: #555; margin-bottom: 6px; }
    .btn-query-stop { background: #1E1E2E; color: #CDD6F4; border: 1px solid #45475A; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; cursor: pointer; margin: 2px; }
    .result-box { margin-top: 6px; padding-top: 4px; border-top: 1px solid #ccc; font-size: 11px; color: #111; }

    /* ---------- Paradas clave resaltadas ---------- */
    .k-50a { --c: #A6E3A1; }
    .k-50b { --c: #89B4FA; }
    .k-50r { --c: #CBA6F7; }
    .stop-key { position: relative; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; }
    .stop-key-ring { position: absolute; top: 2px; left: 2px; right: 2px; bottom: 2px; border-radius: 50%; border: 2px solid var(--c); animation: ringKey 2.2s ease-out infinite; }
    .stop-key-core { position: relative; width: 26px; height: 26px; border-radius: 50%; background: var(--c); color: #11111B; font-size: 15px; font-weight: 900; line-height: 1; display: flex; align-items: center; justify-content: center; border: 3px solid #11111B; box-shadow: 0 0 0 2px var(--c), 0 3px 8px rgba(0,0,0,0.7); }
    .stop-key-tag { position: absolute; top: 100%; left: 50%; transform: translateX(-50%); margin-top: -5px; background: #11111B; color: var(--c); border: 1px solid var(--c); border-radius: 6px; font-size: 10px; font-weight: 800; padding: 1px 5px; white-space: nowrap; box-shadow: 0 2px 5px rgba(0,0,0,0.6); }
    @keyframes ringKey {
      0% { transform: scale(0.7); opacity: 0.9; }
      100% { transform: scale(1.35); opacity: 0; }
    }

    #panel-clave { margin-bottom: 16px; background: #1E1E2E; border: 1px solid #313244; border-radius: 14px; padding: 12px; }
    .panel-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
    .panel-title { font-size: 15px; font-weight: 800; color: #F9E2AF; }
    .chips { display: flex; gap: 6px; flex-wrap: wrap; }
    .chip { background: #181825; color: #CDD6F4; border: 1px solid #45475A; border-radius: 999px; font-size: 11px; font-weight: 700; padding: 4px 10px; cursor: pointer; }
    .chip.active { background: #89B4FA; color: #11111B; border-color: #89B4FA; }
    #lista-clave { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 8px; }
    .card-clave { background: #181825; border: 1px solid #313244; border-left: 4px solid var(--c); border-radius: 10px; padding: 8px 10px; cursor: pointer; }
    .card-top { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
    .badge-lin { background: var(--c); color: #11111B; font-weight: 800; font-size: 11px; padding: 1px 8px; border-radius: 6px; }
    .btn-card-refresh { background: transparent; border: 1px solid #45475A; color: #A6ADC8; border-radius: 6px; font-size: 11px; font-weight: 700; padding: 1px 7px; cursor: pointer; }
    .card-cruce { font-size: 12px; font-weight: 700; color: #CDD6F4; margin-top: 4px; }
    .card-meta { font-size: 10px; color: #A6ADC8; margin-top: 1px; }
    .arr-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 7px; }
    .arr-chip { font-size: 11px; font-weight: 800; padding: 2px 9px; border-radius: 999px; background: #313244; color: #CDD6F4; }
    .arr-chip small { font-weight: 600; opacity: 0.8; margin-left: 3px; }
    .arr-chip.ya { background: #A6E3A1; color: #11111B; }
    .arr-chip.pronto { background: #F9E2AF; color: #11111B; }
    .arr-vacio { font-size: 11px; color: #A6ADC8; margin-top: 7px; }

    /* ---------- Trazado de ruta ---------- */
    .ruta-flecha { background: none; border: none; pointer-events: none; }
    .tip-ruta { background: #11111B; color: #CDD6F4; border: 1px solid #45475A; font-size: 10px; font-weight: 800; padding: 1px 6px; box-shadow: none; }
  </style>
</head>
<body>
  <header>
    <h1>Transporte Plottier</h1>
    <p class="sub">GPS en vivo y predicción continua (50A, 50B, 50R y 52)</p>
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
    <div class="panel-head">
      <div class="panel-title">⭐ Paradas clave · próximos arribos</div>
      <div class="chips" id="filtro-clave">
        <button class="chip active" data-filtro="TODAS">Todas</button>
        <button class="chip" data-filtro="50A">50A</button>
        <button class="chip" data-filtro="50B">50B</button>
        <button class="chip" data-filtro="50R">50R</button>
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
    let busesSim = {}; // id -> estado físico y marcador en tiempo real
    let RUTAS_GEO = {};
    let RUTAS_DENSAS = {};
    let PARADAS_LISTA = [];
    let PARADAS_CLAVE = [];
    let CLAVE_POR_COD = {};
    let marcadoresClave = {};   // codigo -> marcador
    let arribosClave = {};      // codigo -> { arribos, edad }
    let filtroClave = 'TODAS';
    let mostrandoParadas = true;

    const COLORES_LINEA = {
      "50A": "#A6E3A1",
      "50B": "#89B4FA",
      "50R": "#CBA6F7",
      "52 CENTRO": "#FAB387",
      "52 UNION": "#FAB387"
    };
    function colorLinea(l) { return COLORES_LINEA[l] || "#89B4FA"; }
    function claseLineaClave(l) { return l === "50A" ? "k-50a" : (l === "50R" ? "k-50r" : "k-50b"); }

    function esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    // Semáforos reales del corredor Plottier - Ruta 22 - Av. Mosconi - Neuquén
    const SEMAFOROS = [
      [-38.9444, -68.2304], // Av. del Trabajo y Favaloro
      [-38.9444, -68.2257], // Av. San Martín y Av. del Trabajo
      [-38.9506, -68.2258], // Av. San Martín y Alberdi
      [-38.9559, -68.2378], // Av. Zabaleta y Libertad
      [-38.9569, -68.2263], // Buenos Aires Norte y Riavitz
      [-38.9557, -68.2180], // Cruce Terminal ETOP
      [-38.9556, -68.1969], // Autovía y Constituyentes
      [-38.9558, -68.1845], // Autovía y Piscicultura
      [-38.9564, -68.1676], // Río Colorado (límite Plottier-Nqn)
      [-38.9578, -68.1552], // B° Valentina Sur
      [-38.9579, -68.1401], // Solalique / ETON
      [-38.9581, -68.1281], // Bejarano
      [-38.9591, -68.1172], // Anaya / Ignacio Rivas
      [-38.9592, -68.1065], // Gatica
      [-38.9594, -68.0935], // El Cholar
      [-38.9595, -68.0843], // Jumbo / Lastra
      [-38.9582, -68.0748], // Soldado Desconocido / Alcorta
      [-38.9582, -68.0665], // Lainez
      [-38.9582, -68.0590], // Av. Olascoaga
      [-38.9508, -68.0561]  // Av. Argentina / Centro
    ];

    function distMts(lat1, lon1, lat2, lon2) {
      const dLat = (lat1 - lat2) * 111139;
      const dLon = (lon1 - lon2) * 111139 * 0.777;
      return Math.sqrt(dLat * dLat + dLon * dLon);
    }

    // Subdividir polilíneas cada 20 metros para que el seguimiento de ruta sea suave y exacto
    function densificarPolilinea(pts) {
      if (!pts || pts.length < 2) return pts || [];
      const res = [pts[0]];
      for (let i = 0; i < pts.length - 1; i++) {
        const p1 = pts[i], p2 = pts[i + 1];
        const d = distMts(p1[0], p1[1], p2[0], p2[1]);
        const pasos = Math.max(1, Math.ceil(d / 20));
        for (let s = 1; s <= pasos; s++) {
          const t = s / pasos;
          res.push([p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t]);
        }
      }
      return res;
    }

    function encontrarIndiceMasCercano(lat, lon, pts) {
      let minIdx = 0, minDist = Infinity;
      for (let i = 0; i < pts.length; i++) {
        const d = distMts(lat, lon, pts[i][0], pts[i][1]);
        if (d < minDist) {
          minDist = d;
          minIdx = i;
        }
      }
      return { index: minIdx, dist: minDist };
    }

    function calcularProximaParada(bus) {
      let mejorNombre = null;
      let menorDist = Infinity;
      const haciaNqn = bus.sentido_code === "HACIA_NEUQUEN";

      for (const p of PARADAS_LISTA) {
        const idP = p[0], latP = p[1], lonP = p[2], descP = p[3], lineasP = p[4];
        if (!lineasP || !lineasP[bus.linea]) continue;

        // Filtrar paradas que estén hacia adelante según el sentido de avance
        const deltaLon = lonP - bus.simLon;
        if (haciaNqn && deltaLon < -0.0005) continue;
        if (!haciaNqn && deltaLon > 0.0005) continue;

        const d = distMts(bus.simLat, bus.simLon, latP, lonP);
        if (d > 25 && d < menorDist) {
          menorDist = d;
          const clave = CLAVE_POR_COD[idP];
          if (clave) {
            mejorNombre = `⭐ ${clave.cruce} (${idP})`;
          } else {
            mejorNombre = (descP && descP !== idP) ? `${descP} (${idP})` : idP;
          }
        }
      }

      if (!mejorNombre || menorDist > 3500) return "Avanzando en recorrido";
      const velMps = Math.max(4.5, (bus.vel_kmh || 32) / 3.6);
      const minEst = Math.max(1, Math.round((menorDist / velMps) / 60));
      return `${mejorNombre} (~${minEst} min)`;
    }

    async function init() {
      map = L.map('map', { zoomControl: false }).setView([-38.955, -68.18], 12);
      L.control.zoom({ position: 'topright' }).addTo(map);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18 }).addTo(map);

      // Panel propio para el recorrido: queda por debajo de los marcadores
      map.createPane('rutaPane');
      map.getPane('rutaPane').style.zIndex = 450;

      capaRuta = L.layerGroup().addTo(map);
      capaParadas = L.layerGroup().addTo(map);
      capaClave = L.layerGroup().addTo(map);

      const r = await fetch('/api/static_data');
      const staticData = await r.json();
      RUTAS_GEO = staticData.trazas;
      PARADAS_LISTA = staticData.paradas_raw || [];
      PARADAS_CLAVE = staticData.paradas_clave || [];
      PARADAS_CLAVE.forEach(p => { CLAVE_POR_COD[p.codigo] = p; });

      for (const [lin, sentidos] of Object.entries(RUTAS_GEO)) {
        RUTAS_DENSAS[lin] = {};
        for (const [sent, pts] of Object.entries(sentidos)) {
          RUTAS_DENSAS[lin][sent] = densificarPolilinea(pts);
        }
      }

      dibujarParadas(staticData.paradas);
      dibujarParadasClave();
      configurarPanelClave();
      renderPanelClave();
      cargarArribosClave();
      setInterval(cargarArribosClave, 9000);

      pedirDatos();
      setInterval(pedirDatos, 9000);
      requestAnimationFrame(loopFisicoContinuo);
    }

    function dibujarParadas(paradas) {
      capaParadas.clearLayers();
      paradas.forEach(p => {
        const id = p[0], lat = p[1], lon = p[2], desc = p[3], lineas = p[4];
        let botones = '';
        for (const [linNom, info] of Object.entries(lineas)) {
          botones += `<button class="btn-query-stop" onclick="consultarParada('${info.parada}', '${info.cod}', '${linNom}')">⏱️️ ${linNom}</button>`;
        }

        const marker = L.circleMarker([lat, lon], {
          radius: 4.5, color: '#FAB387', fillColor: '#F9E2AF', fillOpacity: 0.85, weight: 1.5
        });

        marker.bindPopup(`
          <div style="min-width:165px;">
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
        iconSize: [44, 44],
        iconAnchor: [22, 22],
        popupAnchor: [0, -20]
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
        const visible = filtroClave === 'TODAS' || p.linea === filtroClave;
        if (visible && !capaClave.hasLayer(m)) capaClave.addLayer(m);
        if (!visible && capaClave.hasLayer(m)) capaClave.removeLayer(m);
      });
      document.querySelectorAll('#filtro-clave .chip').forEach(c => {
        c.classList.toggle('active', c.dataset.filtro === filtroClave);
      });
      renderPanelClave();
    }

    function renderPanelClave() {
      const cont = document.getElementById('lista-clave');
      const lista = PARADAS_CLAVE.filter(p => filtroClave === 'TODAS' || p.linea === filtroClave);
      if (lista.length === 0) {
        cont.innerHTML = '<div class="arr-vacio">No hay paradas clave para mostrar.</div>';
        return;
      }
      cont.innerHTML = lista.map(p => {
        const ar = arribosClave[p.codigo];
        let cuerpo;
        if (!ar || ar.arribos == null) {
          cuerpo = '<div class="arr-vacio">Consultando arribos…</div>';
        } else {
          const ord = ordenarArribos(ar.arribos).slice(0, 3);
          if (ord.length === 0) {
            cuerpo = '<div class="arr-vacio">Sin arribos próximos</div>';
          } else {
            cuerpo = '<div class="arr-row">' + ord.map(a => {
              const cls = a._min === 0 ? 'ya' : (a._min <= 5 ? 'pronto' : '');
              return `<span class="arr-chip ${cls}" title="${esc(a.ramal)}">${esc(a.tiempo)}<small>${esc(sentidoCorto(a.sentido))}</small></span>`;
            }).join('') + '</div>';
          }
        }
        const edad = ar && ar.edad != null ? textoEdad(ar.edad) : '';
        return `
          <div class="card-clave ${claseLineaClave(p.linea)}" data-cod="${esc(p.codigo)}">
            <div class="card-top">
              <span class="badge-lin">${esc(p.linea)}</span>
              <button class="btn-card-refresh" data-refresh="${esc(p.codigo)}">↻</button>
            </div>
            <div class="card-cruce">${esc(p.cruce)}</div>
            <div class="card-meta">Parada ${esc(p.codigo)}${edad ? ' · ' + edad : ''}</div>
            ${cuerpo}
          </div>
        `;
      }).join('');
    }

    function configurarPanelClave() {
      document.getElementById('filtro-clave').addEventListener('click', e => {
        const b = e.target.closest('.chip');
        if (!b) return;
        filtroClave = b.dataset.filtro;
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
          ordenarArribos(data.arribos).forEach(a => {
            h += `<div style="margin-bottom:3px;"><strong>${esc(a.ramal)}</strong> (${esc(a.sentido)})<br>Arribo: <span style="color:#27ae60; font-weight:bold;">${esc(a.tiempo)}</span></div>`;
          });
          if (box) box.innerHTML = h;
          pedirDatos();
        }
      } catch (e) {
        if (box) box.innerHTML = 'Error al consultar parada.';
      }
    }

    function generarHTMLPopup(bus) {
      const colorSentido = bus.sentido_code === 'HACIA_NEUQUEN' ? '#FAB387' : '#A6E3A1';
      let estadoHTML = '';

      if (bus.cabecera && bus.edad_senal > 45) {
        estadoHTML = `<div style="color:#F9E2AF; font-weight:700; margin-top:4px;">⏸️ En ${bus.cabecera}<br><span style="font-weight:normal; font-size:11px; color:#555;">Unidad aguardando horario de salida</span></div>`;
      } else if (bus.edad_senal > 180) {
        const minSin = Math.floor(bus.edad_senal / 60);
        estadoHTML = `<div style="color:#c0392b; font-weight:700; margin-top:4px;">🚨 Sin señal (${minSin} min)<br><span style="font-weight:normal; font-size:11px; color:#555;">Posible unidad averiada o detenida</span></div>`;
      } else if (bus.edad_senal >= 45) {
        estadoHTML = `<div style="color:#d35400; font-weight:700; margin-top:4px;">⚠️ Señal débil (hace ${bus.edad_senal}s)<br><span style="font-weight:normal; font-size:11px; color:#555;">Avance estimado sobre el recorrido</span></div>`;
      } else if (bus.enSemaforo) {
        estadoHTML = `<div style="color:#e67e22; font-weight:700; margin-top:4px;">🚦 Detenido en semáforo</div>`;
      } else {
        estadoHTML = `<div style="color:#27ae60; font-weight:700; margin-top:4px;">🟢 En movimiento (~${Math.round(bus.vel_kmh || 34)} km/h)</div>`;
      }

      const desvioHTML = bus.enDesvioMosconi
        ? `<div style="color:#8e44ad; font-size:11px; font-weight:700; margin-top:2px;">🚧 Desvío obras Av. Mosconi (traza alternativa)</div>`
        : '';

      const proxParada = calcularProximaParada(bus);

      return `
        <div style="font-family:sans-serif; font-size:12px; min-width:175px;">
          <strong style="font-size:14px;">Línea ${bus.linea}</strong>
          <span style="color:#666; font-size:11px;">(${bus.ramal})</span><br>
          <span style="color:${colorSentido}; font-weight:800;">${bus.sentido}</span>
          ${estadoHTML}
          ${desvioHTML}
          <div style="margin-top:5px; padding-top:4px; border-top:1px solid #ddd; font-size:11px; color:#222;">
            📍 <strong>Próxima:</strong> ${proxParada}
          </div>
        </div>
      `;
    }

    function construirIcono(bus) {
      let clase = "bus-marker-50b";
      if (bus.linea === "50A") clase = "bus-marker-50a";
      if (bus.linea === "50R") clase = "bus-marker-50r";
      if (bus.linea.includes("52")) clase = "bus-marker-52";

      let estadoClase = "";
      let iconoPrefijo = "🚌";
      if (bus.cabecera && bus.edad_senal > 45) {
        estadoClase = "bus-cabecera";
        iconoPrefijo = "⏸️";
      } else if (bus.edad_senal > 180) {
        estadoClase = "bus-stalled";
        iconoPrefijo = "🚨";
      } else if (bus.edad_senal >= 45) {
        estadoClase = "bus-weak";
        iconoPrefijo = "📡";
      }

      return L.divIcon({
        className: 'custom-icon',
        html: `<div class="bus-marker ${clase} ${estadoClase}">${iconoPrefijo} ${bus.linea}</div>`,
        iconSize: [72, 24],
        iconAnchor: [36, 12]
      });
    }

    // Motor de simulación continua a 60 FPS
    let ultimoFrame = performance.now();
    function loopFisicoContinuo(ahoraMs) {
      const dt = Math.min((ahoraMs - ultimoFrame) / 1000.0, 0.25);
      ultimoFrame = ahoraMs;

      for (const id in busesSim) {
        const b = busesSim[id];

        // Incrementar suavemente la edad de señal entre consultas
        b.edad_senal += dt;

        // 1. Si está en cabecera sin señal o supera los 3 minutos sin señal, no avanza
        if ((b.cabecera && b.edad_senal > 45) || b.edad_senal > 180) {
          continue;
        }

        // 2. Verificar pausa en semáforos
        if (b.pausaHastaMs && ahoraMs < b.pausaHastaMs) {
          b.enSemaforo = true;
          continue;
        } else {
          b.enSemaforo = false;
        }

        for (let s = 0; s < SEMAFOROS.length; s++) {
          const sem = SEMAFOROS[s];
          if (distMts(b.simLat, b.simLon, sem[0], sem[1]) < 20) {
            if (b.ultimoSemaforoIdx !== s) {
              b.ultimoSemaforoIdx = s;
              // 45% de probabilidad de encontrar el semáforo en rojo (pausa de 7 a 13 seg)
              if (Math.random() < 0.45) {
                b.pausaHastaMs = ahoraMs + (7000 + Math.random() * 6000);
                b.enSemaforo = true;
                break;
              }
            }
          }
        }
        if (b.enSemaforo) continue;

        // 3. Calcular velocidad de avance (más conservadora si tiene señal débil)
        const factorSenal = b.edad_senal >= 45 ? 0.65 : 0.92;
        const velMps = ((b.vel_kmh || 32) / 3.6) * factorSenal;
        let avanceMts = velMps * dt;

        const traza = (RUTAS_DENSAS[b.linea] && RUTAS_DENSAS[b.linea][b.sentido_code]) || [];

        // 4. Detectar si está en la zona de desvío de Av. Mosconi (al Este de Jumbo: lon > -68.088)
        const infoCercana = traza.length > 0 ? encontrarIndiceMasCercano(b.simLat, b.simLon, traza) : { index: 0, dist: 999 };
        b.enDesvioMosconi = (b.simLon > -68.088 && infoCercana.dist > 45);

        if (traza.length > 1 && !b.enDesvioMosconi && infoCercana.dist < 180) {
          // Seguir la polilínea real del recorrido punto por punto
          let idx = infoCercana.index;
          while (avanceMts > 0 && idx < traza.length - 1) {
            const sig = traza[idx + 1];
            const dSeg = distMts(b.simLat, b.simLon, sig[0], sig[1]);
            if (dSeg <= avanceMts) {
              b.simLat = sig[0];
              b.simLon = sig[1];
              avanceMts -= dSeg;
              idx++;
            } else {
              const frac = avanceMts / dSeg;
              b.simLat += (sig[0] - b.simLat) * frac;
              b.simLon += (sig[1] - b.simLon) * frac;
              avanceMts = 0;
            }
          }
        } else {
          // En zona de desvío de Mosconi (post-Jumbo), avanzar por la grilla de calles paralelas
          const dirLon = b.sentido_code === "HACIA_NEUQUEN" ? 1 : -1;
          const dLonGrados = (avanceMts / (111139 * 0.777)) * dirLon;
          b.simLon += dLonGrados;
        }

        // Corrección suave hacia el último punto GPS real si acaba de actualizarse
        if (b.edad_senal < 12) {
          const err = distMts(b.simLat, b.simLon, b.gpsLat, b.gpsLon);
          if (err > 15 && err < 600) {
            b.simLat += (b.gpsLat - b.simLat) * 0.04;
            b.simLon += (b.gpsLon - b.simLon) * 0.04;
          }
        }

        b.marker.setLatLng([b.simLat, b.simLon]);
      }

      requestAnimationFrame(loopFisicoContinuo);
    }

    // ---------- Trazado de ruta mejorado ----------
    function bearingGrados(a, b) {
      const dLat = b[0] - a[0];
      const dLon = (b[1] - a[1]) * Math.cos(a[0] * Math.PI / 180);
      return (Math.atan2(dLon, dLat) * 180 / Math.PI + 360) % 360;
    }

    function difAngulo(a, b) {
      const d = Math.abs(a - b) % 360;
      return d > 180 ? 360 - d : d;
    }

    function iconoFlecha(color, ang) {
      return L.divIcon({
        className: 'ruta-flecha',
        html: `<svg width="16" height="16" viewBox="0 0 16 16" style="transform:rotate(${ang}deg); display:block;"><path d="M8 1 L14 14 L8 11 L2 14 Z" fill="${color}" stroke="#11111B" stroke-width="1.5" stroke-linejoin="round"/></svg>`,
        iconSize: [16, 16],
        iconAnchor: [8, 8]
      });
    }

    function mostrarRuta(linea, sentidoCode) {
      capaRuta.clearLayers();
      const pts = RUTAS_GEO[linea] && RUTAS_GEO[linea][sentidoCode];
      if (!pts || pts.length < 2) return;

      const color = colorLinea(linea);

      // Borde oscuro + línea de color: el recorrido se distingue sobre cualquier fondo del mapa
      capaRuta.addLayer(L.polyline(pts, {
        pane: 'rutaPane', color: '#11111B', weight: 9, opacity: 0.8,
        lineJoin: 'round', lineCap: 'round', interactive: false
      }));
      capaRuta.addLayer(L.polyline(pts, {
        pane: 'rutaPane', color: color, weight: 5, opacity: 0.95,
        lineJoin: 'round', lineCap: 'round', interactive: false
      }));

      // Flechas de sentido cada ~600 m (evitando esquinas donde la orientación sería ambigua)
      const densa = (RUTAS_DENSAS[linea] && RUTAS_DENSAS[linea][sentidoCode]) || [];
      for (let i = 15; i < densa.length - 3; i += 30) {
        const prev = i >= 2 ? bearingGrados(densa[i - 2], densa[i]) : bearingGrados(densa[i], densa[i + 2]);
        const next = bearingGrados(densa[i], densa[i + 2]);
        if (difAngulo(prev, next) > 35) continue;
        capaRuta.addLayer(L.marker(densa[i], {
          icon: iconoFlecha(color, next),
          interactive: false,
          keyboard: false,
          zIndexOffset: -500
        }));
      }

      // Inicio y fin del recorrido
      const ini = pts[0], fin = pts[pts.length - 1];
      [[ini, 'Inicio'], [fin, 'Fin']].forEach(([p, txt]) => {
        const c = L.circleMarker(p, {
          pane: 'rutaPane', radius: 7, color: '#11111B', weight: 3,
          fillColor: color, fillOpacity: 1, interactive: false
        });
        c.bindTooltip(txt, { permanent: true, direction: 'top', offset: [0, -6], className: 'tip-ruta' });
        capaRuta.addLayer(c);
      });

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
        sincronizarBuses(d.buses);
      } catch (e) {}
    }

    function sincronizarBuses(buses) {
      const label = document.getElementById('bus-count');
      const enVivo = buses.filter(b => b.edad_senal <= 180).length;
      label.innerText = buses.length > 0
        ? `🚌 ${enVivo} activos en recorrido (${buses.length} en radar)`
        : "Buscando unidades en recorrido...";

      const idsActivos = new Set();

      buses.forEach(b => {
        idsActivos.add(b.id);

        if (busesSim[b.id]) {
          const sim = busesSim[b.id];
          const saltoGps = distMts(sim.gpsLat, sim.gpsLon, b.lat, b.lon);

          sim.gpsLat = b.lat;
          sim.gpsLon = b.lon;
          sim.vel_kmh = b.vel_kmh || sim.vel_kmh;
          sim.sentido = b.sentido;
          sim.sentido_code = b.sentido_code;
          sim.ramal = b.ramal;
          sim.cabecera = b.cabecera;
          sim.edad_senal = b.edad_senal;

          // Si llegó un paquete GPS nuevo y el colectivo ya cruzó el semáforo, liberar pausa
          if (saltoGps > 25) {
            sim.pausaHastaMs = 0;
            // Si la diferencia con la simulación es grande, reacomodar suavemente
            if (distMts(sim.simLat, sim.simLon, b.lat, b.lon) > 220) {
              sim.simLat = b.lat;
              sim.simLon = b.lon;
            }
          }

          sim.marker.setIcon(construirIcono(sim));
          sim.marker.getPopup().setContent(generarHTMLPopup(sim));
          sim.marker.off('click').on('click', () => {
            sim.marker.getPopup().setContent(generarHTMLPopup(sim));
            mostrarRuta(sim.linea, sim.sentido_code);
          });
        } else {
          const nuevoSim = {
            ...b,
            simLat: b.lat,
            simLon: b.lon,
            gpsLat: b.lat,
            gpsLon: b.lon,
            enSemaforo: false,
            enDesvioMosconi: false,
            pausaHastaMs: 0,
            ultimoSemaforoIdx: -1
          };
          const m = L.marker([b.lat, b.lon], { icon: construirIcono(nuevoSim), zIndexOffset: 1000 });
          m.bindPopup(generarHTMLPopup(nuevoSim));
          m.on('click', () => {
            m.getPopup().setContent(generarHTMLPopup(nuevoSim));
            mostrarRuta(nuevoSim.linea, nuevoSim.sentido_code);
          });
          m.addTo(map);
          nuevoSim.marker = m;
          busesSim[b.id] = nuevoSim;
        }
      });

      for (let id in busesSim) {
        if (!idsActivos.has(id)) {
          map.removeLayer(busesSim[id].marker);
          delete busesSim[id];
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
