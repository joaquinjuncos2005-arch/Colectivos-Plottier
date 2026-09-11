import os
import re
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, render_template_string
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

URL_PAGINA = "https://cuandollega.smartmovepro.net/indalo/recorridos"
URL_API = f"{URL_PAGINA}?handler=Arribos"

# =========================================================
# PARADAS / CONSULTAS
# =========================================================

CONSULTAS = [

    # -----------------------------------------------------
    # TUS PARADAS FAVORITAS
    # -----------------------------------------------------

    {
        "seccion": "MIS FAVORITOS",
        "parada": "1260",
        "calle": "Av. Belgrano y Pulmari",
        "lat": -38.9533476,
        "lon": -68.2375682,
        "linea": "50B",
        "cod": "1014",
        "mostrar": True,
        "mapa": True
    },

    {
        "seccion": "MIS FAVORITOS",
        "parada": "NV1311",
        "calle": "Santa Cruz y Riavitz",
        "lat": -38.957381,
        "lon": -68.226198,
        "linea": "50B",
        "cod": "1014",
        "mostrar": True,
        "mapa": True
    },

    {
        "seccion": "MIS FAVORITOS",
        "parada": "NV2316",
        "calle": "Martellota y Las Lajas",
        "lat": -38.9508355,
        "lon": -68.228275,
        "linea": "50B",
        "cod": "1014",
        "mostrar": True,
        "mapa": True
    },

    # -----------------------------------------------------
    # DETECCIÓN GPS 50A
    # Se consulta la misma red de paradas estratégicas
    # que el 50B, pero sin crear tarjetas duplicadas.
    # -----------------------------------------------------

    {
        "seccion": "GPS 50A",
        "parada": "1260",
        "calle": "Av. Belgrano y Pulmari",
        "lat": -38.9533476,
        "lon": -68.2375682,
        "linea": "50A",
        "cod": "1013",
        "mostrar": False,
        "mapa": False
    },

    {
        "seccion": "GPS 50A",
        "parada": "NV1311",
        "calle": "Santa Cruz y Riavitz",
        "lat": -38.957381,
        "lon": -68.226198,
        "linea": "50A",
        "cod": "1013",
        "mostrar": False,
        "mapa": False
    },

    {
        "seccion": "GPS 50A",
        "parada": "NV2316",
        "calle": "Martellota y Las Lajas",
        "lat": -38.9508355,
        "lon": -68.228275,
        "linea": "50A",
        "cod": "1013",
        "mostrar": False,
        "mapa": False
    },

    {
        "seccion": "GPS 50A",
        "parada": "NV5019",
        "calle": "Ruta 22 ETOP",
        "linea": "50A",
        "cod": "1013",
        "mostrar": False,
        "mapa": False
    },

    # -----------------------------------------------------
    # CABECERAS
    # -----------------------------------------------------

    {
        "seccion": "CABECERA",
        "parada": "NV2000",
        "calle": "Cabecera Plottier",
        "linea": "50B",
        "cod": "1014",
        "mostrar": True,
        "mapa": True,
        "cabecera": True
    },

    {
        "seccion": "CABECERA",
        "parada": "NV1014",
        "calle": "Cabecera Neuquén",
        "lat": -38.946678,
        "lon": -68.0576064,
        "linea": "50A",
        "cod": "1013",
        "mostrar": True,
        "mapa": True,
        "cabecera": True
    },

    # -----------------------------------------------------
    # BARRIDO GPS
    # -----------------------------------------------------

    {
        "seccion": "BARRIDO",
        "parada": "NV1259",
        "calle": "",
        "linea": "50B",
        "cod": "1014",
        "mostrar": False,
        "mapa": False
    },

    {
        "seccion": "BARRIDO GPS 50A",
        "parada": "NV1259",
        "calle": "",
        "linea": "50A",
        "cod": "1013",
        "mostrar": False,
        "mapa": False
    },

    {
        "seccion": "BARRIDO",
        "parada": "NV1058",
        "calle": "",
        "linea": "50B",
        "cod": "1014",
        "mostrar": False,
        "mapa": False
    },

    {
        "seccion": "BARRIDO",
        "parada": "NV1058",
        "calle": "",
        "linea": "50A",
        "cod": "1013",
        "mostrar": False,
        "mapa": False
    },
]


# =========================================================
# SESIÓN HTTP
# =========================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-419,es;q=0.9,en;q=0.8",
})

csrf_token = None


# =========================================================
# CACHE
# =========================================================

CACHE_TTL = 20
MAX_STALE_TTL = 180

ultimo_cache = {
    "timestamp": 0,
    "hora": "--:--:--",
    "items": [],
    "buses": [],
    "paradas": [],
    "rutas": []
}


# =========================================================
# CACHE DE COORDENADAS DE PARADAS
# =========================================================

cache_paradas_mapa = {}


def obtener_hora_arg():
    tz_arg = timezone(timedelta(hours=-3))
    return datetime.now(tz_arg).strftime("%H:%M:%S")


# =========================================================
# TOKEN CSRF
# =========================================================

def renovar_token():
    global csrf_token, session

    session.cookies.clear()

    resp = session.get(
        URL_PAGINA,
        verify=False,
        timeout=10
    )

    resp.raise_for_status()

    tokens = re.findall(
        r'CfDJ8[A-Za-z0-9_\-]{80,}',
        resp.text
    )

    if tokens:
        csrf_token = tokens[0]
    else:

        m = re.search(
            r'__RequestVerificationToken.*?value=["\']([^"\']+)["\']',
            resp.text,
            re.IGNORECASE
        )

        csrf_token = m.group(1) if m else None

    if not csrf_token:
        raise Exception("No se pudo obtener el token CSRF.")


# =========================================================
# CONSULTA DE ARRIBOS
# =========================================================

def consultar_post(payload):

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

    resp = session.post(
        URL_API,
        json=payload,
        headers=headers,
        verify=False,
        timeout=8
    )

    if resp.status_code in (400, 401, 403, 500):

        time.sleep(0.3)

        renovar_token()

        headers["RequestVerificationToken"] = csrf_token

        resp = session.post(
            URL_API,
            json=payload,
            headers=headers,
            verify=False,
            timeout=8
        )

    resp.raise_for_status()

    data = resp.json()

    return data.get("arribos", [])


def consultar_arribos(item):

    parada = item["parada"]
    cod_linea = str(item["cod"])

    # -----------------------------------------------
    # PRIMER INTENTO: parada + línea
    # -----------------------------------------------

    payload = {
        "IdentificadorParada": parada,
        "CodigoLinea": cod_linea
    }

    arribos = consultar_post(payload)

    if arribos:
        return arribos

    # -----------------------------------------------
    # SEGUNDO INTENTO PARA CABECERAS
    #
    # Algunas cabeceras pueden devolver resultados
    # cuando no se especifica CodigoLinea.
    # -----------------------------------------------

    if item.get("cabecera"):

        payload_cabecera = {
            "IdentificadorParada": parada
        }

        try:
            arribos = consultar_post(payload_cabecera)

            if arribos:

                # Filtrar por línea
                filtrados = []

                for c in arribos:

                    descripcion = str(
                        c.get(
                            "descripcionBandera",
                            ""
                        )
                    ).upper()

                    descripcion_normalizada = re.sub(
                        r"[^A-Z0-9]",
                        "",
                        descripcion
                    )

                    linea_normalizada = re.sub(
                        r"[^A-Z0-9]",
                        "",
                        item["linea"].upper()
                    )

                    if linea_normalizada in descripcion_normalizada:
                        filtrados.append(c)

                if filtrados:
                    return filtrados

                # Si no se pudo identificar la línea,
                # devolvemos lo que respondió la cabecera.
                return arribos

        except Exception:
            pass

    # Algunos puntos pueden responder correctamente sin CodigoLinea.
    # En ese caso hacemos un segundo intento y nos quedamos solo con la
    # línea solicitada. Esto ayuda especialmente con 50A.
    try:
        arribos_sin_linea = consultar_post({
            "IdentificadorParada": parada
        })

        linea_normalizada = re.sub(
            r"[^A-Z0-9]",
            "",
            item["linea"].upper()
        )

        filtrados = []
        for c in arribos_sin_linea:
            descripcion = str(
                c.get("descripcionBandera", "")
            ).upper()
            descripcion_normalizada = re.sub(
                r"[^A-Z0-9]",
                "",
                descripcion
            )

            if linea_normalizada in descripcion_normalizada:
                filtrados.append(c)

        if filtrados:
            return filtrados

    except Exception:
        pass

    return []


# =========================================================
# GEOCODIFICACIÓN DE PARADAS
# =========================================================

def geocodificar_parada(item):

    clave = f'{item["parada"]}_{item["linea"]}'

    if clave in cache_paradas_mapa:
        return cache_paradas_mapa[clave]

    # Si la parada tiene coordenadas exactas cargadas manualmente,
    # usarlas antes que la geocodificación por dirección.
    if item.get("lat") is not None and item.get("lon") is not None:
        try:
            resultado = {
                "lat": float(item["lat"]),
                "lon": float(item["lon"])
            }
            cache_paradas_mapa[clave] = resultado
            return resultado
        except (ValueError, TypeError):
            pass

    direccion = item.get("calle", "").strip()

    if not direccion:
        return None

    query = f"{direccion}, Plottier, Neuquén, Argentina"

    try:

        headers = {
            "User-Agent": "TransportePlottier/1.0"
        }

        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": query,
                "format": "json",
                "limit": 1
            },
            headers=headers,
            timeout=8
        )

        resp.raise_for_status()

        resultados = resp.json()

        if resultados:

            lat = float(resultados[0]["lat"])
            lon = float(resultados[0]["lon"])

            resultado = {
                "lat": lat,
                "lon": lon
            }

            cache_paradas_mapa[clave] = resultado

            return resultado

    except Exception:
        pass

    return None


def obtener_paradas_mapa():

    resultado = []

    for item in CONSULTAS:

        if not item.get("mapa"):
            continue

        coords = geocodificar_parada(item)

        if not coords:
            continue

        resultado.append({
            "parada": item["parada"],
            "calle": item["calle"],
            "linea": item["linea"],
            "lat": coords["lat"],
            "lon": coords["lon"],
            "seccion": item["seccion"]
        })

    return resultado


# =========================================================
# RECORRIDOS OFICIALES 50A / 50B
# =========================================================

# Fuente oficial: Municipalidad de Plottier. Las secuencias de calles
# publicadas en sus páginas de las líneas 50A y 50B se usan como base.
# Separamos cada recorrido en IDA y VUELTA para poder mostrarlos con
# colores diferentes. La geometría final se calcula sobre las calles reales
# mediante OSRM, usando intersecciones como puntos de control.

RUTA_CACHE_VERSION = "2026-09-11-v4"

RUTA_PUNTOS = {
    "50A": {
        "ida": [
            ("Buenos Aires y San Juan, Neuquén, Argentina"),
            ("San Juan y Alderete, Neuquén, Argentina"),
            ("Alderete y Tucumán, Neuquén, Argentina"),
            ("Tucumán y Tierra del Fuego, Neuquén, Argentina"),
            ("Tierra del Fuego y Mitre, Neuquén, Argentina"),
            ("Mitre y Sarmiento, Neuquén, Argentina"),
            ("Sarmiento y La Pampa, Neuquén, Argentina"),
            ("La Pampa y Av. Mosconi, Neuquén, Argentina"),
            ("Av. Mosconi y O'Connor, Neuquén, Argentina"),
            ("O'Connor y Río Colorado, Plottier, Neuquén, Argentina"),
            ("Río Colorado y Av. Constituyentes, Plottier, Neuquén, Argentina"),
            ("Av. Constituyentes y Lugones, Plottier, Neuquén, Argentina"),
            ("Lugones y Santa Fe Norte, Plottier, Neuquén, Argentina"),
            ("Santa Fe Norte y Riavitz, Plottier, Neuquén, Argentina"),
            ("Riavitz y Santa Cruz, Plottier, Neuquén, Argentina"),
            ("Santa Cruz y Zabaleta, Plottier, Neuquén, Argentina"),
            ("Zabaleta y Mosconi, Plottier, Neuquén, Argentina"),
            ("Mosconi y Batilana, Plottier, Neuquén, Argentina"),
            ("Batilana y Santa Cruz, Plottier, Neuquén, Argentina"),
            ("Santa Cruz y Arrayanes, Plottier, Neuquén, Argentina"),
            ("Arrayanes y Las Mutisias, Plottier, Neuquén, Argentina"),
            ("Las Mutisias y Batilana, Plottier, Neuquén, Argentina"),
            ("Batilana y Santiago del Estero, Plottier, Neuquén, Argentina"),
            ("Santiago del Estero y Zabaleta, Plottier, Neuquén, Argentina"),
            ("Zabaleta y Av. Roca, Plottier, Neuquén, Argentina"),
            ("Av. Roca y Belgrano, Plottier, Neuquén, Argentina"),
            ("Belgrano y Quillén, Plottier, Neuquén, Argentina"),
            ("Quillén y Masilla, Plottier, Neuquén, Argentina"),
            ("Masilla y Martellota, Plottier, Neuquén, Argentina"),
            ("Martellota y Av. San Martín, Plottier, Neuquén, Argentina"),
        ],
        "vuelta": [
            ("Av. San Martín y Buratovich, Plottier, Neuquén, Argentina"),
            ("Buratovich y Favaloro, Plottier, Neuquén, Argentina"),
            ("Favaloro y Belisle, Plottier, Neuquén, Argentina"),
            ("Belisle y Av. Roca, Plottier, Neuquén, Argentina"),
            ("Av. Roca y Godoy, Plottier, Neuquén, Argentina"),
            ("Godoy y Picasso, Plottier, Neuquén, Argentina"),
            ("Picasso y Av. Roca, Plottier, Neuquén, Argentina"),
            ("Av. Roca y Aristóbulo del Valle, Plottier, Neuquén, Argentina"),
            ("Aristóbulo del Valle y Picasso, Plottier, Neuquén, Argentina"),
            ("Picasso y Av. Godoy, Plottier, Neuquén, Argentina"),
            ("Av. Godoy y Av. San Martín, Plottier, Neuquén, Argentina"),
            ("Av. San Martín y Av. del Trabajo, Plottier, Neuquén, Argentina"),
            ("Av. del Trabajo y Realico, Plottier, Neuquén, Argentina"),
            ("Realico y Olivos, Plottier, Neuquén, Argentina"),
            ("Olivos y Palermo, Plottier, Neuquén, Argentina"),
            ("Palermo y Río Colorado, Plottier, Neuquén, Argentina"),
            ("Río Colorado y San Martín, Plottier, Neuquén, Argentina"),
            ("San Martín y Ruta 22, Plottier, Neuquén, Argentina"),
            ("Ruta 22 y J.J. Lastra, Neuquén, Argentina"),
            ("J.J. Lastra y Lainez, Neuquén, Argentina"),
            ("Lainez y Alcorta, Neuquén, Argentina"),
            ("Alcorta y Av. Olascoaga, Neuquén, Argentina"),
            ("Av. Olascoaga e Independencia, Neuquén, Argentina"),
            ("Independencia y Santa Fe, Neuquén, Argentina"),
            ("Santa Fe y Pinar, Neuquén, Argentina"),
            ("Pinar y Buenos Aires, Neuquén, Argentina"),
            ("Buenos Aires y San Juan, Neuquén, Argentina"),
        ],
    },
    "50B": {
        "ida": [
            ("Buenos Aires y San Juan, Neuquén, Argentina"),
            ("San Juan y Alderete, Neuquén, Argentina"),
            ("Alderete y Tucumán, Neuquén, Argentina"),
            ("Tucumán y Tierra del Fuego, Neuquén, Argentina"),
            ("Tierra del Fuego y Mitre, Neuquén, Argentina"),
            ("Mitre y Sarmiento, Neuquén, Argentina"),
            ("Sarmiento y La Pampa, Neuquén, Argentina"),
            ("La Pampa y Av. Mosconi, Neuquén, Argentina"),
            ("Av. Mosconi y O'Connor, Neuquén, Argentina"),
            ("O'Connor y Río Colorado, Plottier, Neuquén, Argentina"),
            ("Río Colorado y Palermo, Plottier, Neuquén, Argentina"),
            ("Palermo y Olivos, Plottier, Neuquén, Argentina"),
            ("Olivos y Realico, Plottier, Neuquén, Argentina"),
            ("Realico y Av. del Trabajo, Plottier, Neuquén, Argentina"),
            ("Av. del Trabajo y Av. San Martín, Plottier, Neuquén, Argentina"),
            ("Av. San Martín y Godoy, Plottier, Neuquén, Argentina"),
            ("Godoy y Picasso, Plottier, Neuquén, Argentina"),
            ("Picasso y Aristóbulo del Valle, Plottier, Neuquén, Argentina"),
            ("Aristóbulo del Valle y Rudecindo Roca, Plottier, Neuquén, Argentina"),
            ("Rudecindo Roca y Picasso, Plottier, Neuquén, Argentina"),
            ("Picasso y Av. Godoy, Plottier, Neuquén, Argentina"),
            ("Godoy y Av. Roca, Plottier, Neuquén, Argentina"),
            ("Av. Roca y Belisle, Plottier, Neuquén, Argentina"),
            ("Belisle y Favaloro, Plottier, Neuquén, Argentina"),
            ("Favaloro y Buratovich, Plottier, Neuquén, Argentina"),
            ("Buratovich y Av. San Martín, Plottier, Neuquén, Argentina"),
            ("Av. San Martín y Martellota, Plottier, Neuquén, Argentina"),
            ("Martellota y Masnilla, Plottier, Neuquén, Argentina"),
            ("Masnilla y Quillén, Plottier, Neuquén, Argentina"),
            ("Quillén y Belgrano, Plottier, Neuquén, Argentina"),
            ("Belgrano y Av. Roca, Plottier, Neuquén, Argentina"),
        ],
        "vuelta": [
            ("Av. Roca y Av. Sabaleta, Plottier, Neuquén, Argentina"),
            ("Av. Sabaleta y Santiago del Estero, Plottier, Neuquén, Argentina"),
            ("Santiago del Estero y Batilana, Plottier, Neuquén, Argentina"),
            ("Batilana y Las Mutisias, Plottier, Neuquén, Argentina"),
            ("Las Mutisias y Cafayate, Plottier, Neuquén, Argentina"),
            ("Cafayate y Arrayanes, Plottier, Neuquén, Argentina"),
            ("Arrayanes y Batilana, Plottier, Neuquén, Argentina"),
            ("Batilana y Mosconi, Plottier, Neuquén, Argentina"),
            ("Mosconi y Av. Sabaleta, Plottier, Neuquén, Argentina"),
            ("Av. Sabaleta y Santa Cruz, Plottier, Neuquén, Argentina"),
            ("Santa Cruz y Riavitz, Plottier, Neuquén, Argentina"),
            ("Riavitz y Santa Fe Norte, Plottier, Neuquén, Argentina"),
            ("Santa Fe Norte y Ruta 22, Plottier, Neuquén, Argentina"),
            ("Ruta 22 y J.J. Lastra, Neuquén, Argentina"),
            ("J.J. Lastra y Lainez, Neuquén, Argentina"),
            ("Lainez y Alcorta, Neuquén, Argentina"),
            ("Alcorta y Av. Olascoaga, Neuquén, Argentina"),
            ("Av. Olascoaga e Independencia, Neuquén, Argentina"),
            ("Independencia y Santa Fe, Neuquén, Argentina"),
            ("Santa Fe y Pinar, Neuquén, Argentina"),
            ("Pinar y Buenos Aires, Neuquén, Argentina"),
            ("Buenos Aires y San Juan, Neuquén, Argentina"),
        ],
    },
}

RUTA_COLORS = {
    ("50A", "ida"): "#E6008D",
    ("50A", "vuelta"): "#F59E0B",
    ("50B", "ida"): "#14B8A6",
    ("50B", "vuelta"): "#7C3AED",
}

RUTA_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "rutas_cache.json"
)

rutas_mapa_cache = []
rutas_mapa_lock = threading.Lock()
rutas_mapa_estado = {
    "estado": "pendiente",
    "mensaje": "Calculando recorridos..."
}


def cargar_rutas_cache():
    global rutas_mapa_cache
    try:
        with open(RUTA_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if (
            isinstance(data, dict)
            and data.get("version") == RUTA_CACHE_VERSION
            and isinstance(data.get("rutas"), list)
            and data.get("rutas")
        ):
            rutas_mapa_cache = data["rutas"]
            rutas_mapa_estado["estado"] = "ok"
            rutas_mapa_estado["mensaje"] = "Recorridos listos"
    except Exception:
        pass


def guardar_rutas_cache():
    try:
        tmp = RUTA_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"version": RUTA_CACHE_VERSION, "rutas": rutas_mapa_cache},
                f,
                ensure_ascii=False
            )
        os.replace(tmp, RUTA_CACHE_FILE)
    except Exception:
        pass


def _coord_en_zona_nqn_plottier(lat, lon):
    # Evita resultados del geocodificador en otras ciudades de la región.
    return (
        -39.12 <= lat <= -38.82
        and -68.38 <= lon <= -67.95
    )


def geocodificar_referencia_ruta(texto):
    headers = {
        "User-Agent": "TransportePlottier/1.0 (mapa de recorridos)"
    }

    # Primero Photon: rápido para la preparación inicial.
    try:
        resp = requests.get(
            "https://photon.komoot.io/api/",
            params={
                "q": texto,
                "limit": 3,
                "bbox": "-68.38,-39.12,-67.95,-38.82"
            },
            headers=headers,
            timeout=7
        )
        resp.raise_for_status()
        data = resp.json()

        for feature in data.get("features", []):
            coords = feature.get("geometry", {}).get("coordinates", [])
            props = feature.get("properties", {}) or {}
            if len(coords) >= 2:
                lon, lat = float(coords[0]), float(coords[1])
                ciudad = str(
                    props.get("city")
                    or props.get("town")
                    or props.get("municipality")
                    or ""
                ).lower()

                # Preferimos resultados explícitamente de Neuquén/Plottier,
                # aunque también aceptamos cualquier punto dentro del bbox.
                if _coord_en_zona_nqn_plottier(lat, lon):
                    if (
                        "neuquen" in ciudad
                        or "neuquén" in ciudad
                        or "plottier" in ciudad
                        or not ciudad
                    ):
                        return [lat, lon]
    except Exception:
        pass

    # Fallback más estricto: Nominatim, acotado a la zona de trabajo.
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": texto,
                "format": "jsonv2",
                "limit": 3,
                "countrycodes": "ar",
                "viewbox": "-68.38,-38.82,-67.95,-39.12",
                "bounded": 1,
            },
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()
        resultados = resp.json()

        for resultado in resultados:
            lat = float(resultado["lat"])
            lon = float(resultado["lon"])
            if _coord_en_zona_nqn_plottier(lat, lon):
                return [lat, lon]
    except Exception:
        pass

    return None


def obtener_geometria_osrm(puntos):
    if len(puntos) < 2:
        return None

    # OSRM admite muchos waypoints, pero algunos servidores limitan la URL.
    # Dividimos el trazado en bloques solapados y los unimos.
    bloques = []
    paso = 16
    i = 0
    while i < len(puntos) - 1:
        bloque = puntos[i:i + paso]
        if i > 0:
            bloque = [puntos[i - 1]] + bloque
        if len(bloque) >= 2:
            bloques.append(bloque)
        i += paso - 1

    geometria_total = []

    for bloque in bloques:
        coordenadas = ";".join(
            f"{lon},{lat}"
            for lat, lon in bloque
        )
        url = (
            "https://router.project-osrm.org/route/v1/driving/"
            f"{coordenadas}"
        )

        try:
            resp = requests.get(
                url,
                params={
                    "overview": "full",
                    "geometries": "geojson",
                    "steps": "false",
                },
                headers={"User-Agent": "TransportePlottier/1.0"},
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok":
                return None

            routes = data.get("routes", [])
            if not routes:
                return None

            coordinates = routes[0].get("geometry", {}).get("coordinates", [])
            parte = [[float(lat), float(lon)] for lon, lat in coordinates]
            if len(parte) < 2:
                return None

            if geometria_total and parte:
                # Evita duplicar el punto de unión.
                parte = parte[1:]
            geometria_total.extend(parte)
        except Exception:
            return None

    return geometria_total if len(geometria_total) >= 2 else None


def construir_rutas_en_segundo_plano():
    global rutas_mapa_cache

    with rutas_mapa_lock:
        if rutas_mapa_estado["estado"] == "procesando":
            return
        if rutas_mapa_cache:
            rutas_mapa_estado["estado"] = "ok"
            rutas_mapa_estado["mensaje"] = "Recorridos listos"
            return
        rutas_mapa_estado["estado"] = "procesando"
        rutas_mapa_estado["mensaje"] = "Calculando recorridos..."

    try:
        nuevas_rutas = []

        for linea, sentidos in RUTA_PUNTOS.items():
            for sentido, referencias in sentidos.items():
                puntos_por_indice = {}

                with ThreadPoolExecutor(max_workers=5) as executor:
                    futuros = {
                        executor.submit(
                            geocodificar_referencia_ruta,
                            referencia
                        ): indice
                        for indice, referencia in enumerate(referencias)
                    }

                    for futuro in as_completed(futuros):
                        indice = futuros[futuro]
                        try:
                            punto = futuro.result()
                        except Exception:
                            punto = None
                        if punto:
                            puntos_por_indice[indice] = punto

                # Mantenemos estrictamente el orden publicado.
                puntos = [
                    puntos_por_indice[i]
                    for i in sorted(puntos_por_indice)
                ]

                # Menos de 70% de referencias válidas implica que el resultado
                # puede estar deformado: no lo dibujamos como si fuera oficial.
                if len(puntos) < max(3, int(len(referencias) * 0.70)):
                    continue

                geometria = obtener_geometria_osrm(puntos)
                if not geometria:
                    # Último respaldo: unir los puntos válidos, pero marcado como
                    # "respaldo" para que el usuario sepa que OSRM no estuvo disponible.
                    geometria = puntos
                    tipo = "respaldo"
                else:
                    tipo = "calle"

                nuevas_rutas.append({
                    "linea": linea,
                    "sentido": sentido,
                    "tipo": tipo,
                    "puntos": geometria,
                })

        with rutas_mapa_lock:
            rutas_mapa_cache = nuevas_rutas
            if nuevas_rutas:
                rutas_mapa_estado["estado"] = "ok"
                rutas_mapa_estado["mensaje"] = "Recorridos listos"
            else:
                rutas_mapa_estado["estado"] = "error"
                rutas_mapa_estado["mensaje"] = "No se pudieron calcular los recorridos"

        if nuevas_rutas:
            guardar_rutas_cache()

    except Exception as exc:
        with rutas_mapa_lock:
            rutas_mapa_estado["estado"] = "error"
            rutas_mapa_estado["mensaje"] = f"Error calculando recorridos: {exc}"


def iniciar_calculo_rutas():
    cargar_rutas_cache()
    if rutas_mapa_cache:
        return
    hilo = threading.Thread(
        target=construir_rutas_en_segundo_plano,
        name="rutas-map-worker",
        daemon=True
    )
    hilo.start()


# =========================================================
# HTML
# =========================================================

HTML_TEMPLATE = """
<!DOCTYPE html>

<html lang="es">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,
             initial-scale=1.0,
             maximum-scale=1.0,
             user-scalable=no"
>

<title>Líneas 50A / 50B - Plottier</title>

<meta
    name="theme-color"
    content="#181825"
>

<link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<style>

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Roboto,
        sans-serif;
}

body {
    background-color: #181825;
    color: #CDD6F4;
    padding: 16px 14px 95px;
}

header {
    margin-bottom: 12px;
}

h1 {
    font-size: 22px;
    color: #89B4FA;
    font-weight: 800;
}

.sub {
    font-size: 13px;
    color: #A6ADC8;
    margin-top: 2px;
}

#map-container {
    position: relative;
    margin-bottom: 16px;
    border-radius: 14px;
    overflow: hidden;
    border: 1px solid #313244;
}

#map {
    height: 360px;
    width: 100%;
    background: #11111B;
}

.map-badge {
    position: absolute;
    top: 10px;
    left: 10px;
    z-index: 1000;
    background: rgba(24, 24, 37, 0.85);
    padding: 4px 10px;
    border-radius: 8px;
    font-size: 11px;
    font-weight: 600;
}

.route-legend {
    position: absolute;
    right: 10px;
    top: 10px;
    z-index: 1000;
    background: rgba(24, 24, 37, 0.90);
    padding: 8px 10px;
    border-radius: 8px;
    font-size: 10px;
    line-height: 1.55;
    border: 1px solid #313244;
}

.route-legend > div {
    display: flex;
    align-items: center;
    gap: 5px;
}

.legend-line {
    width: 18px;
    height: 4px;
    border-radius: 999px;
    display: inline-block;
}

.legend-line.a-ida { background: #E6008D; }
.legend-line.a-vuelta { background: #F59E0B; }
.legend-line.b-ida { background: #14B8A6; }
.legend-line.b-vuelta { background: #7C3AED; }

.section-title {
    font-size: 14px;
    color: #F5E0DC;
    text-transform: uppercase;
    font-weight: 700;
    margin: 12px 0 8px;
}

.card {
    background: #1E1E2E;
    border: 1px solid #313244;
    border-radius: 14px;
    padding: 14px 16px;
    margin-bottom: 10px;
}

.card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.line-tag {
    background: #313244;
    font-size: 12px;
    font-weight: 700;
    padding: 4px 8px;
    border-radius: 6px;
}

.line-50b {
    color: #89B4FA;
}

.line-50a {
    color: #A6E3A1;
}


.stop-tag {
    font-size: 12px;
    color: #6C7086;
    text-align: right;
}

.arrival-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 0;
    border-top: 1px solid #2A2B3D;
}

.time {
    font-size: 15px;
    font-weight: 700;
    color: #A6E3A1;
}

.branch {
    font-size: 13px;
    color: #CDD6F4;
}

.btn-focus {
    background: #313244;
    color: #CDD6F4;
    border: 1px solid #45475A;
    font-size: 11px;
    padding: 6px 10px;
    border-radius: 8px;
    cursor: pointer;
}

.empty {
    font-size: 13px;
    color: #A6ADC8;
    font-style: italic;
}

.bar-fixed {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    background: rgba(24, 24, 37, 0.96);
    padding: 12px 16px;
    border-top: 1px solid #313244;
    display: flex;
    justify-content: space-between;
    align-items: center;
    z-index: 2000;
}

.btn-refresh {
    background: #89B4FA;
    color: #11111B;
    border: none;
    font-size: 15px;
    font-weight: 700;
    padding: 12px 20px;
    border-radius: 12px;
    cursor: pointer;
}

.status-text {
    font-size: 12px;
    color: #A6ADC8;
}

.bus-marker {
    display: flex;
    align-items: center;
    gap: 3px;
    padding: 3px 6px;
    border-radius: 8px;
    font-size: 11px;
    font-weight: 800;
}

.bus-marker-50b {
    background-color: #1E2D42;
    border: 2px solid #89B4FA;
    color: #89B4FA;
}

.bus-marker-50a {
    background-color: #1E3A24;
    border: 2px solid #A6E3A1;
    color: #A6E3A1;
}


.stop-marker {
    background: #F5E0DC;
    border: 3px solid #CDD6F4;
    width: 15px;
    height: 15px;
    border-radius: 50%;
}

.stop-label {
    background: #181825;
    color: #CDD6F4;
    border: 1px solid #45475A;
    border-radius: 6px;
    padding: 3px 6px;
    font-size: 10px;
    font-weight: 700;
    white-space: nowrap;
}

</style>

</head>

<body>

<header>

<h1>Transporte Plottier</h1>

<p class="sub">
Tus Favoritos + GPS en vivo
</p>

</header>

<div id="map-container">

<div
    class="map-badge"
    id="bus-count"
>
    Buscando coches...
</div>

<div class="route-legend">
    <div><span class="legend-line a-ida"></span> 50A ida</div>
    <div><span class="legend-line a-vuelta"></span> 50A vuelta</div>
    <div><span class="legend-line b-ida"></span> 50B ida</div>
    <div><span class="legend-line b-vuelta"></span> 50B vuelta</div>
</div>

<div id="map"></div>

</div>

<div id="contenido">
Cargando paradas...
</div>

<div class="bar-fixed">

<button
    id="btn"
    class="btn-refresh"
    onclick="pedirDatos()"
>
Actualizar
</button>

<div>

<div
    id="status"
    class="status-text"
>
Iniciando...
</div>

<div
    style="
        font-size:10px;
        color:#FAB387;
    "
>
Actualización auto: 1 min
</div>

</div>

</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<script>

let map;
let busLayer;
let stopLayer;
let routeLayer;
let busMarkers = {};

function initMap() {

    map = L.map(
        'map',
        {
            zoomControl: false
        }
    ).setView(
        [-38.955, -68.16],
        12
    );

    L.tileLayer(
        'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        {
            attribution:
                '&copy; OpenStreetMap'
        }
    ).addTo(map);

    busLayer = L.layerGroup().addTo(map);

    stopLayer = L.layerGroup().addTo(map);

    routeLayer = L.layerGroup().addTo(map);
}


function centrarEn(lat, lon) {

    map.setView(
        [lat, lon],
        15,
        {
            animate: true
        }
    );

    window.scrollTo({
        top: 0,
        behavior: 'smooth'
    });
}


function dibujarRutas(rutas) {

    routeLayer.clearLayers();

    const estilos = {
        "50A-ida": {color: "#E6008D", label: "50A · Ida"},
        "50A-vuelta": {color: "#F59E0B", label: "50A · Vuelta"},
        "50B-ida": {color: "#14B8A6", label: "50B · Ida"},
        "50B-vuelta": {color: "#7C3AED", label: "50B · Vuelta"}
    };

    rutas.forEach(ruta => {
        const key = `${ruta.linea}-${ruta.sentido}`;
        const estilo = estilos[key] || {color: "#FFFFFF", label: key};

        const polyline = L.polyline(
            ruta.puntos,
            {
                color: estilo.color,
                weight: 7,
                opacity: 0.92,
                lineJoin: "round",
                lineCap: "round"
            }
        );

        polyline.bindPopup(
            `<b>${estilo.label}</b><br>` +
            (ruta.tipo === "calle"
                ? "Trayectoria siguiendo las calles"
                : "Geometría de respaldo")
        );

        polyline.addTo(routeLayer);
    });
}


let rutasCargadas = false;
let rutasCargaEnCurso = false;

async function cargarRutas() {

    if (rutasCargadas || rutasCargaEnCurso) {
        return rutasCargadas;
    }

    rutasCargaEnCurso = true;

    try {
        const res = await fetch('/api/rutas', { cache: 'no-store' });

        if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
        }

        const data = await res.json();

        if (Array.isArray(data.rutas) && data.rutas.length > 0) {
            dibujarRutas(data.rutas);
            rutasCargadas = true;
            return true;
        }

        return false;

    } catch (err) {
        console.error('No se pudieron cargar los recorridos:', err);
        return false;
    } finally {
        rutasCargaEnCurso = false;
    }
}


function dibujarParadas(paradas) {

    stopLayer.clearLayers();

    paradas.forEach(p => {

        const icon = L.divIcon({

            className: '',

            html:
                '<div class="stop-marker"></div>',

            iconSize: [15, 15],
            iconAnchor: [7, 7]

        });

        const marker = L.marker(
            [p.lat, p.lon],
            {
                icon: icon,
                zIndexOffset: -100
            }
        );

        marker.bindTooltip(
            p.calle +
            '<br><small>' +
            'Parada ' +
            p.parada +
            ' · Línea ' +
            p.linea +
            '</small>',
            {
                permanent: true,
                direction: 'top',
                offset: [0, -6],
                className: 'stop-label'
            }
        );

        marker.bindPopup(
            '<b>' +
            p.calle +
            '</b><br>' +
            'Parada ' +
            p.parada +
            '<br>Línea ' +
            p.linea
        );

        marker.addTo(stopLayer);

    });

}


async function pedirDatos() {

    const btn =
        document.getElementById('btn');

    const status =
        document.getElementById('status');

    btn.disabled = true;

    status.innerText = "Consultando...";

    try {

        const res =
            await fetch('/api/arribos', {cache: 'no-store'});

        const data =
            await res.json();

        renderTarjetas(data.items);

        actualizarMapa(data.buses);

        dibujarParadas(data.paradas);

        status.innerText =
            "Últ. vez: " + data.hora;

        btn.disabled = false;

    } catch (err) {

        console.error(err);

        status.innerText =
            "Error temporal";

        btn.disabled = false;
    }
}


function actualizarMapa(buses) {

    const actuales = new Set();

    document
        .getElementById('bus-count')
        .innerText =
            buses.length > 0
                ? `🚌 ${buses.length} coche(s)`
                : "Sin coches activos";

    buses.forEach(b => {

        const busKey =
            b.id
            || `${b.linea}-${Math.round(b.lat * 10000)}-${Math.round(b.lon * 10000)}`;

        actuales.add(busKey);

        let claseCss =
            b.linea === "50A"
                ? "bus-marker-50a"
                : "bus-marker-50b";

        const opacity = b.stale ? 0.60 : 1.0;

        const icon = L.divIcon({
            className: '',
            html:
                `<div class="bus-marker ${claseCss}" style="opacity:${opacity}">
                    🚌 ${b.linea}
                </div>`,
            iconSize: [65, 26]
        });

        const popup =
            `<b>Línea ${b.linea}</b>` +
            `<br>${b.ramal || ""}` +
            `<br>${b.tiempo || "Sin datos"}` +
            `<br><small>${
                b.stale
                    ? `Última señal hace ${Math.max(1, Math.round(b.edad_senal))} s`
                    : "Señal recibida ahora"
            }</small>`;

        let marker = busMarkers[busKey];

        if (!marker) {
            marker = L.marker(
                [b.lat, b.lon],
                {icon}
            );
            marker.addTo(busLayer);
            busMarkers[busKey] = marker;
        } else {
            marker.setLatLng([b.lat, b.lon]);
            marker.setIcon(icon);
        }

        marker.bindPopup(popup);
    });

    Object.keys(busMarkers).forEach(key => {
        if (!actuales.has(key)) {
            busLayer.removeLayer(busMarkers[key]);
            delete busMarkers[key];
        }
    });
}


function renderTarjetas(items) {

    let html = '';

    let secActual = '';

    items.forEach(item => {

        if (item.seccion !== secActual) {

            secActual = item.seccion;

            html +=
                `<div class="section-title">
                    📍 ${secActual}
                </div>`;
        }

        const infoCalle =
            item.calle
                ? `<br><b>${item.calle}</b>`
                : '';

        let claseLinea = 'line-50b';

        if (item.linea === '50A') {
            claseLinea = 'line-50a';
        }


        html +=
            `<div class="card">

                <div class="card-header">

                    <span
                        class="line-tag ${claseLinea}"
                    >
                        Línea ${item.linea}
                    </span>

                    <span class="stop-tag">
                        Parada ${item.parada}
                        ${infoCalle}
                    </span>

                </div>`;

        if (
            !item.arribos ||
            item.arribos.length === 0
        ) {

            html +=
                `<div class="empty">
                    Sin unidades reportando
                </div>`;

        } else {

            item.arribos.forEach(c => {

                const btn =
                    (
                        c.lat &&
                        c.lon
                    )
                    ?
                    `<button
                        class="btn-focus"
                        onclick="
                            centrarEn(
                                ${c.lat},
                                ${c.lon}
                            )
                        "
                    >
                        Mapa
                    </button>`
                    :
                    '';

                html +=
                    `<div class="arrival-row">

                        <div>

                            <div class="branch">
                                ${c.ramal}
                            </div>

                            <div class="time">
                                • ${c.tiempo}
                            </div>

                        </div>

                        ${btn}

                    </div>`;
            });
        }

        html += `</div>`;
    });

    document
        .getElementById('contenido')
        .innerHTML = html;
}


initMap();

pedirDatos();

// Los recorridos son estáticos durante la sesión: se cargan una sola vez
// y nunca vuelven a recalcularse junto con los datos GPS.
// Esto evita que una actualización de colectivos borre o retrase el trazado.
(async function cargarRecorridosIniciales() {
    const maxIntentos = 15;

    for (let intento = 0; intento < maxIntentos; intento++) {
        const listos = await cargarRutas();

        if (listos) {
            break;
        }

        await new Promise(resolve => setTimeout(resolve, 2000));
    }
})();

setInterval(
    pedirDatos,
    3000
);

</script>

</body>

</html>
"""


# =========================================================
# RUTA PRINCIPAL
# =========================================================

@app.route('/')
def home():
    return render_template_string(
        HTML_TEMPLATE
    )


# =========================================================
# API PRINCIPAL
# =========================================================

# =========================================================
# ACTUALIZACIÓN EN SEGUNDO PLANO DE ARRIBOS / GPS
# =========================================================

REFRESH_INTERVAL = 3.0
BUS_STALE_TTL = 120.0

refresh_lock = threading.Lock()
refresh_running = False

bus_history = {}
bus_history_lock = threading.Lock()
bus_sequence = 0


def inicializar_cache_visible():
    """Crea las tarjetas inmediatamente, incluso antes del primer refresco."""
    visibles = []
    for item in CONSULTAS:
        if item.get("mostrar"):
            visibles.append({
                **item,
                "arribos": []
            })
    ultimo_cache["items"] = visibles
    ultimo_cache["hora"] = obtener_hora_arg()
    ultimo_cache["paradas"] = []
    ultimo_cache["buses"] = []
    ultimo_cache["rutas"] = []


def distancia_aprox_km(lat1, lon1, lat2, lon2):
    # Haversine sin dependencias externas.
    from math import radians, sin, cos, sqrt, atan2
    r = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp/2)**2 + cos(p1) * cos(p2) * sin(dl/2)**2
    return 2 * r * atan2(sqrt(a), sqrt(1-a))


def obtener_identificador_bus(c, linea, ramal):
    posibles = (
        "idVehiculo", "vehicleId", "idUnidad", "unidadId",
        "codigoUnidad", "codVehiculo", "numeroInterno",
        "interno", "idCoche", "cocheId", "patente", "id"
    )
    for campo in posibles:
        valor = c.get(campo)
        if valor not in (None, ""):
            return f"{linea}:{campo}:{valor}"
    return None


def registrar_bus(linea, ramal, lat, lon, tiempo, raw):
    """Actualiza/encuentra un coche y conserva su última posición si luego se pierde señal."""
    global bus_sequence

    ahora = time.time()
    linea = str(linea).upper()
    ramal = str(ramal or linea)
    identificador = obtener_identificador_bus(raw, linea, ramal)

    with bus_history_lock:
        candidato = None

        if identificador:
            candidato = bus_history.get(identificador)
        else:
            # Sin ID oficial: buscamos el coche más cercano de la misma línea/ramal.
            mejor_distancia = 999.0
            ramal_norm = re.sub(r"[^A-Z0-9]", "", ramal.upper())
            for key, anterior in bus_history.items():
                if anterior["linea"] != linea:
                    continue
                anterior_ramal = re.sub(
                    r"[^A-Z0-9]", "", str(anterior.get("ramal", "")).upper()
                )
                if ramal_norm and anterior_ramal and ramal_norm != anterior_ramal:
                    continue
                d = distancia_aprox_km(
                    lat, lon,
                    anterior["lat"], anterior["lon"]
                )
                if d < mejor_distancia and d <= 4.0:
                    mejor_distancia = d
                    candidato = anterior

        if candidato is None:
            bus_sequence += 1
            key = identificador or f"{linea}:sin-id:{bus_sequence}"
            candidato = {
                "id": key,
                "linea": linea,
                "ramal": ramal,
                "lat": lat,
                "lon": lon,
                "tiempo": tiempo,
                "ultima_senal": ahora,
            }
            bus_history[key] = candidato
        else:
            key = candidato["id"]
            candidato.update({
                "linea": linea,
                "ramal": ramal,
                "lat": lat,
                "lon": lon,
                "tiempo": tiempo,
                "ultima_senal": ahora,
            })

        return key


def obtener_buses_para_mapa():
    ahora = time.time()
    resultado = []

    with bus_history_lock:
        eliminar = []
        for key, bus in bus_history.items():
            edad = ahora - float(bus.get("ultima_senal", ahora))
            if edad <= BUS_STALE_TTL:
                resultado.append({
                    "id": key,
                    "linea": bus["linea"],
                    "ramal": bus["ramal"],
                    "lat": bus["lat"],
                    "lon": bus["lon"],
                    "tiempo": bus["tiempo"],
                    "stale": edad > REFRESH_INTERVAL * 2.5,
                    "edad_senal": edad,
                })
            else:
                eliminar.append(key)

        for key in eliminar:
            bus_history.pop(key, None)

    return resultado


def _consultar_item_en_worker(item):
    try:
        return item, consultar_arribos(item), None
    except Exception as exc:
        return item, [], exc


def refrescar_datos_background():
    global refresh_running, ultimo_cache

    try:
        resultados_raw = []

        # Consultamos todas las paradas en paralelo. Esto evita que una parada
        # lenta bloquee a las demás y es la mejora principal del tiempo de GPS.
        with ThreadPoolExecutor(max_workers=8) as executor:
            futuros = [
                executor.submit(_consultar_item_en_worker, item)
                for item in CONSULTAS
            ]
            for futuro in as_completed(futuros):
                resultados_raw.append(futuro.result())

        # Restituimos el orden original para las tarjetas.
        orden = {id(item): i for i, item in enumerate(CONSULTAS)}
        resultados_raw.sort(key=lambda x: orden.get(id(x[0]), 9999))

        visibles = []
        ahora = time.time()

        # Mantener arribos recientes si una consulta puntual falla.
        anteriores = {
            (x.get("parada"), x.get("linea")): x.get("arribos", [])
            for x in ultimo_cache.get("items", [])
        }

        for item, arribos_raw, error in resultados_raw:
            arribos_limpios = []

            for c in arribos_raw:
                lat = c.get("latitud")
                lon = c.get("longitud")
                tiempo = c.get(
                    "tiempoRestanteArribo",
                    c.get("tiempo", "Sin datos")
                )
                ramal = c.get(
                    "descripcionBandera",
                    item["linea"]
                )

                if item["mostrar"]:
                    arribos_limpios.append({
                        "tiempo": tiempo,
                        "ramal": ramal,
                        "lat": lat,
                        "lon": lon,
                    })

                if lat not in (None, "") and lon not in (None, ""):
                    try:
                        latf = float(lat)
                        lonf = float(lon)
                        linea_detectada = item["linea"]
                        ramal_norm = str(ramal).upper()
                        if "50A" in ramal_norm:
                            linea_detectada = "50A"
                        elif "50B" in ramal_norm:
                            linea_detectada = "50B"

                        if linea_detectada in ("50A", "50B"):
                            registrar_bus(
                                linea_detectada,
                                ramal,
                                latf,
                                lonf,
                                tiempo,
                                c
                            )
                    except (TypeError, ValueError):
                        pass

            if item["mostrar"]:
                if not arribos_limpios and error:
                    arribos_limpios = anteriores.get(
                        (item["parada"], item["linea"]), []
                    )
                visibles.append({
                    **item,
                    "arribos": arribos_limpios,
                })

        lista_buses = obtener_buses_para_mapa()
        hora_actual = obtener_hora_arg()

        # Las paradas son estáticas: solo se geocodifican una vez y después quedan en memoria.
        if not ultimo_cache.get("paradas"):
            lista_paradas = obtener_paradas_mapa()
        else:
            lista_paradas = ultimo_cache["paradas"]

        with rutas_mapa_lock:
            rutas_actuales = list(rutas_mapa_cache)

        ultimo_cache.update({
            "timestamp": ahora,
            "hora": hora_actual,
            "items": visibles,
            "buses": lista_buses,
            "paradas": lista_paradas,
            "rutas": rutas_actuales,
        })

    finally:
        with refresh_lock:
            refresh_running = False


def solicitar_refresh_si_corresponde():
    global refresh_running

    ahora = time.time()
    edad = ahora - ultimo_cache.get("timestamp", 0)

    if edad < REFRESH_INTERVAL:
        return

    with refresh_lock:
        if refresh_running:
            return
        refresh_running = True

    hilo = threading.Thread(
        target=refrescar_datos_background,
        name="gps-refresh-worker",
        daemon=True
    )
    hilo.start()


# Prepara tarjetas vacías al instante y el primer refresco queda en segundo plano.
inicializar_cache_visible()


@app.route('/api/arribos')
def api_arribos():
    solicitar_refresh_si_corresponde()

    with rutas_mapa_lock:
        rutas_actuales = list(rutas_mapa_cache)

    return jsonify({
        "hora": ultimo_cache.get("hora", obtener_hora_arg()),
        "items": ultimo_cache.get("items", []),
        "buses": ultimo_cache.get("buses", []),
        "paradas": ultimo_cache.get("paradas", []),
        # Los recorridos no forman parte del refresco GPS.
        # Se sirven una sola vez mediante /api/rutas.
        "rutas": [],
        "from_cache": True,
        "actualizando": refresh_running,
    })


# =========================================================
# API DE RECORRIDOS
# =========================================================

@app.route('/api/rutas')
def api_rutas():
    with rutas_mapa_lock:
        rutas = list(rutas_mapa_cache)
        estado = rutas_mapa_estado["estado"]
        mensaje = rutas_mapa_estado["mensaje"]

    if not rutas and estado in ("pendiente", "error"):
        iniciar_calculo_rutas()
        with rutas_mapa_lock:
            rutas = list(rutas_mapa_cache)
            estado = rutas_mapa_estado["estado"]
            mensaje = rutas_mapa_estado["mensaje"]

    return jsonify({
        "rutas": rutas,
        "estado": estado,
        "mensaje": mensaje
    })


# Arrancar la preparación de recorridos sin bloquear las peticiones web.
iniciar_calculo_rutas()


# =========================================================
# MAIN
# =========================================================

if __name__ == '__main__':

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host='0.0.0.0',
        port=port
    )
