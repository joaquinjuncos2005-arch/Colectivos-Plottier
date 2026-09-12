import os
import re
import time
import math
import urllib3
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

URL_PAGINA = "https://cuandollega.smartmovepro.net/indalo/recorridos"
URL_API = f"{URL_PAGINA}?handler=Arribos"


# Paradas y cabecera con ubicación fija tomadas de main.py.
# Se mantienen separadas de las consultas GPS para no alterar la lógica de flota.
PARADAS_MAPA = [
    {
        "parada": "1260",
        "calle": "Av. Belgrano y Pulmari",
        "lat": -38.9533476,
        "lon": -68.2375682,
        "linea": "50B",
        "tipo": "PARADA",
        "icono": "📍"
    },
    {
        "parada": "NV1311",
        "calle": "Santa Cruz y Riavitz",
        "lat": -38.957381,
        "lon": -68.226198,
        "linea": "50B",
        "tipo": "PARADA",
        "icono": "📍"
    },
    {
        "parada": "NV2316",
        "calle": "Martellota y Las Lajas",
        "lat": -38.9508355,
        "lon": -68.228275,
        "linea": "50B",
        "tipo": "PARADA",
        "icono": "📍"
    },
    {
        "parada": "NV1014",
        "calle": "Cabecera Neuquén",
        "lat": -38.946678,
        "lon": -68.0576064,
        "linea": "50A",
        "tipo": "CABECERA",
        "icono": "🏁"
    }
]

# Tandas rotativas para cubrir paradas aledañas sin saturar el servidor (50A: 1013, 50B: 1014, 50R: 1015)
GRUPOS_CONSULTAS = [
    # Tanda A: Cabeceras oficiales + Barrido principal
    [
        {"seccion": "CABECERA", "parada": "NV2000", "linea": "50B", "cod": "1014", "mostrar": True},
        {"seccion": "CABECERA", "parada": "NV1014", "linea": "50A", "cod": "1013", "mostrar": True},
        {"seccion": "BARRIDO",  "parada": "NV1058", "linea": "50B", "cod": "1014", "mostrar": False},
        {"seccion": "BARRIDO",  "parada": "NV1032", "linea": "50A", "cod": "1013", "mostrar": False},
    ],
    # Tanda B: Paradas aledañas estratégicas + Línea 50R
    [
        {"seccion": "BARRIDO",  "parada": "NV1032", "linea": "50B", "cod": "1014", "mostrar": False},
        {"seccion": "BARRIDO",  "parada": "NV1244", "linea": "50A", "cod": "1013", "mostrar": False},
        {"seccion": "BARRIDO",  "parada": "NV1244", "linea": "50B", "cod": "1014", "mostrar": False},
        {"seccion": "BARRIDO",  "parada": "NV1032", "linea": "50R", "cod": "1015", "mostrar": False},
        {"seccion": "BARRIDO",  "parada": "NV1244", "linea": "50R", "cod": "1015", "mostrar": False},
    ]
]

indice_grupo_actual = 0

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-419,es;q=0.9,en;q=0.8",
})
csrf_token = None

CACHE_TTL = 20
TIEMPO_PERDIDO = 75
TIEMPO_EXPIRAR = 200

flota_memoria = {}
cabeceras_memoria = {
    ("NV2000", "50B"): {"seccion": "CABECERA", "parada": "NV2000", "linea": "50B", "arribos": []},
    ("NV1014", "50A"): {"seccion": "CABECERA", "parada": "NV1014", "linea": "50A", "arribos": []},
}
ultima_hora_sync = "--:--:--"
ultimo_escaneo_ts = 0

def obtener_hora_arg():
    tz_arg = timezone(timedelta(hours=-3))
    return datetime.now(tz_arg).strftime("%H:%M:%S")

def calcular_distancia(lat1, lon1, lat2, lon2):
    return math.sqrt((lat1 - lat2)**2 + (lon1 - lon2)**2) * 111.0

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
        raise Exception("No se pudo obtener token CSRF.")

def consultar_arribos(parada, cod_linea):
    global csrf_token
    if not csrf_token:
        renovar_token()
    
    headers = {
        "Accept": "/",
        "Content-Type": "application/json",
        "Origin": "https://cuandollega.smartmovepro.net",
        "Referer": URL_PAGINA,
        "RequestVerificationToken": csrf_token,
        "X-Requested-With": "XMLHttpRequest"
    }
    payload = {"IdentificadorParada": parada, "CodigoLinea": str(cod_linea)}

    resp = session.post(URL_API, json=payload, headers=headers, verify=False, timeout=9)
    if resp.status_code in (400, 401, 403, 500):
        time.sleep(0.4)
        renovar_token()
        headers["RequestVerificationToken"] = csrf_token
        resp = session.post(URL_API, json=payload, headers=headers, verify=False, timeout=9)

    resp.raise_for_status()
    return resp.json().get("arribos", [])

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-CE85R9NET5"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){dataLayer.push(arguments);}
    gtag('js', new Date());

    gtag('config', 'G-CE85R9NET5');
  </script>

  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Líneas 50A / 50B / 50R - Plottier</title>
  
  <meta name="theme-color" content="#181825">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="manifest" href="/manifest.json">
  <link rel="apple-touch-icon" href="https://cdn-icons-png.flaticon.com/512/1048/1048314.png">
  <link rel="icon" type="image/png" href="https://cdn-icons-png.flaticon.com/512/1048/1048314.png">

  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />

  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background-color: #181825; color: #CDD6F4; padding: 16px 14px 95px; }
    header { margin-bottom: 12px; }
    h1 { font-size: 22px; color: #89B4FA; font-weight: 800; }
    .sub { font-size: 13px; color: #A6ADC8; margin-top: 2px; }
    
    #map-container {
      position: relative;
      margin-bottom: 16px;
      border-radius: 14px;
      overflow: hidden;
      border: 1px solid #313244;
      box-shadow: 0 4px 14px rgba(0,0,0,0.4);
    }
    #map {
      height: 310px;
      width: 100%;
      background: #11111B;
    }
    .map-badge {
      position: absolute;
      top: 10px;
      left: 10px;
      z-index: 1000;
      background: rgba(24, 24, 37, 0.9);
      backdrop-filter: blur(6px);
      padding: 5px 12px;
      border-radius: 8px;
      font-size: 11px;
      color: #CDD6F4;
      border: 1px solid #313244;
      font-weight: 600;
    }
    .btn-clear-route {
      position: absolute;
      bottom: 10px;
      right: 10px;
      z-index: 1000;
      background: rgba(24, 24, 37, 0.9);
      border: 1px solid #45475A;
      color: #CDD6F4;
      font-size: 11px;
      font-weight: 700;
      padding: 6px 12px;
      border-radius: 8px;
      cursor: pointer;
      display: none;
    }

    .section-title { font-size: 14px; color: #F5E0DC; text-transform: uppercase; letter-spacing: 1px; margin: 14px 0 8px; font-weight: 700; }
    .card { background: #1E1E2E; border: 1px solid #313244; border-radius: 14px; padding: 14px 16px; margin-bottom: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.25); }
    .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
    .line-tag { background: #313244; font-size: 12px; font-weight: 700; padding: 4px 8px; border-radius: 6px; }
    .line-50b { color: #89B4FA; }
    .line-50a { color: #A6E3A1; }
    .line-50r { color: #FAB387; }
    .stop-tag { font-size: 12px; color: #6C7086; }
    
    .arrival-row { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-top: 1px solid #2A2B3D; }
    .arrival-row:first-of-type { border-top: none; }
    
    .branch-row { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
    .branch { font-size: 13px; color: #CDD6F4; font-weight: 600; }
    
    .badge-status {
      font-size: 11px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 6px;
      display: inline-block;
    }
    .badge-plottier {
      background: rgba(166, 227, 161, 0.15);
      color: #A6E3A1;
      border: 1px solid rgba(166, 227, 161, 0.3);
    }
    .badge-neuquen {
      background: rgba(250, 179, 135, 0.15);
      color: #FAB387;
      border: 1px solid rgba(250, 179, 135, 0.3);
    }

    .time-label { font-size: 12px; color: #A6ADC8; }
    .time-val { font-size: 15px; font-weight: 700; color: #A6E3A1; }
    
    .btn-action { background: #313244; color: #CDD6F4; border: 1px solid #45475A; font-size: 11px; font-weight: 600; padding: 6px 12px; border-radius: 8px; cursor: pointer; }

    .stop-marker {
      background: #F5E0DC;
      border: 2px solid #89B4FA;
      color: #181825;
      width: 28px;
      height: 28px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      box-shadow: 0 2px 7px rgba(0,0,0,0.55);
    }

    .stop-marker-header {
      background: #FFF3CD;
      border: 2px solid #FAB387;
      color: #181825;
      width: 30px;
      height: 30px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 15px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.55);
    }

    .empty { font-size: 13px; color: #A6ADC8; font-style: italic; padding: 4px 0; }
    
    .credits {
      text-align: center;
      margin-top: 24px;
      padding-top: 16px;
      border-top: 1px solid #313244;
      font-size: 12px;
      color: #6C7086;
      letter-spacing: 0.3px;
    }
    .credits .author { color: #CDD6F4; font-weight: 600; }
    .credits .alias-badge {
      background: #313244;
      color: #89B4FA;
      font-size: 10px;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 6px;
      margin-left: 4px;
      display: inline-block;
    }

    .bar-fixed { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(24, 24, 37, 0.96); backdrop-filter: blur(10px); padding: 12px 16px; border-top: 1px solid #313244; display: flex; justify-content: space-between; align-items: center; z-index: 2000; }
    .btn-refresh { background: #89B4FA; color: #11111B; border: none; font-size: 15px; font-weight: 700; padding: 12px 20px; border-radius: 12px; cursor: pointer; min-width: 125px; }
    .btn-refresh:disabled { opacity: 0.6; cursor: not-allowed; }
    .status-box { text-align: right; }
    .status-text { font-size: 12px; color: #A6ADC8; font-weight: 500; }
    .cached-hint { font-size: 10px; color: #A6E3A1; }

    .leaflet-marker-icon {
      transition: transform 1.2s ease-in-out;
      cursor: pointer;
    }

    .bus-marker {
      display: flex;
      align-items: center;
      gap: 4px;
      padding: 3px 7px;
      border-radius: 8px;
      font-size: 11px;
      font-weight: 800;
      white-space: nowrap;
      box-shadow: 0 2px 8px rgba(0,0,0,0.6);
    }
    .bus-marker-50b { background-color: #1E2D42; border: 2px solid #89B4FA; color: #89B4FA; }
    .bus-marker-50a { background-color: #1E3A24; border: 2px solid #A6E3A1; color: #A6E3A1; }
    .bus-marker-50r { background-color: #382418; border: 2px solid #FAB387; color: #FAB387; }
    
    .bus-marker-lost {
      background-color: #381A22 !important;
      border: 2px solid #F38BA8 !important;
      color: #F38BA8 !important;
      animation: pulse-lost 1.5s infinite;
    }
    @keyframes pulse-lost {
      0% { opacity: 1; }
      50% { opacity: 0.55; }
      100% { opacity: 1; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Transporte Plottier</h1>
    <p class="sub">GPS y recorridos oficiales (50A, 50B y 50R)</p>
  </header>

  <div id="map-container">
    <div class="map-badge" id="bus-count">Sincronizando flota...</div>
    <button class="btn-clear-route" id="btn-clear" onclick="limpiarRuta()">✕ Quitar recorrido</button>
    <div id="map"></div>
  </div>

  <div id="contenido">Cargando cabeceras...</div>

  <div class="credits">
    Desarrollado por <span class="author">Ramiro Alzogaray</span>
    <span class="alias-badge">KaiLoos</span>
  </div>

  <div class="bar-fixed">
    <button id="btn" class="btn-refresh" onclick="pedirDatos()">Actualizar</button>
    <div class="status-box">
      <div id="status" class="status-text">Iniciando...</div>
      <div id="hint" class="cached-hint">Auto-refresco: Activo (30s)</div>
    </div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

  <script>
    const TRAZAS_METRICAS = {
      "50B": {
        "IDA": [[-7576123.127717475,-4713953.225682237],[-7576127.135219142,-4713960.668571745],[-7576140.270919057,-4714625.541017488],[-7575523.672259552,-4714622.5350375585],[-7575533.913652705,-4715537.968661558],[-7578334.266763101,-4715525.657356469],[-7578412.190406656,-4715517.9270097455],[-7578498.129053549,-4715523.939501133],[-7578494.566829843,-4715600.956967883],[-7579108.493821568,-4715589.933986044],[-7579115.72958847,-4715842.177166955],[-7583422.0127703175,-4715796.366361502],[-7583523.090867958,-4715778.757888199],[-7583546.913238986,-4715779.473679201],[-7583639.5310553275,-4715757.141023744],[-7583717.454698882,-4715733.949472043],[-7583945.325696536,-4715679.120273622],[-7584124.772715694,-4715657.360465009],[-7584513.611697037,-4715650.345799814],[-7584585.078810125,-4715654.067866457],[-7584684.821073875,-4715642.042732994],[-7586734.21289938,-4715629.015521161],[-7586895.51484154,-4715621.7145636035],[-7587297.155564321,-4715619.280912253],[-7588442.187846622,-4715419.723488214],[-7588664.270230753,-4715194.25981618],[-7590284.970697214,-4713564.914808015],[-7590591.87853333,-4714429.581617774],[-7590632.510147468,-4714417.701074614],[-7590624.4951441325,-4713756.849877528],[-7594854.635794276,-4713713.624809864],[-7594851.518848535,-4712383.6136334315],[-7596173.103843232,-4712371.878595528],[-7596505.837801212,-4712371.306155002],[-7596507.062315612,-4712007.097415341],[-7596821.09459914,-4712010.675040777],[-7596824.768142336,-4711714.451925238],[-7597492.351128625,-4711704.291768824],[-7597495.690713347,-4711684.114586941],[-7597586.861376307,-4711683.685285635],[-7597684.154611261,-4711690.840309779],[-7597727.45789318,-4711685.545591426],[-7597782.672360612,-4711685.259390514],[-7597844.788636475,-4711680.966377784],[-7597998.52085326,-4711678.390571015],[-7598051.731569858,-4711679.249173199],[-7598168.394396211,-4711676.244065874],[-7598169.618910611,-4711685.545591426],[-7598514.820651559,-4711685.974892813],[-7598512.816900725,-4712308.0516757555],[-7598463.279727323,-4712301.611762196],[-7598446.470484212,-4712292.166563034],[-7598173.626412278,-4712256.103156181],[-7598125.425072765,-4712247.65975801],[-7598058.85601727,-4712240.361233048],[-7597865.271422781,-4712214.744883052],[-7597575.506788245,-4712170.810846718],[-7597547.676915548,-4712165.9451996675],[-7597528.752602113,-4712163.798591416],[-7597513.947109837,-4712164.943449095],[-7597508.492454789,-4712161.938197968],[-7597509.049052242,-4712127.8787473785],[-7597488.900224409,-4712119.864775614],[-7597477.5456363475,-4712111.850810179],[-7597471.200425373,-4712047.309997799],[-7596829.220921968,-4712055.46701795],[-7596829.220921968,-4712382.75497166],[-7596173.437801706,-4712373.16658683],[-7596170.654814434,-4713035.3588711675],[-7595380.954346748,-4713041.083652457],[-7595378.505317951,-4713714.054197047],[-7594863.763992522,-4713715.342358708],[-7594864.097950994,-4714610.94055188],[-7595153.1946685845,-4714615.807371415],[-7595602.257494444,-4714856.431565999],[-7595629.085491726,-4714867.740041546],[-7595630.866603578,-4714928.720139591],[-7595924.750059273,-4714929.292724408],[-7596190.469683797,-4714997.287401224],[-7596213.178859917,-4715768.307345327],[-7596826.437934698,-4715759.288392326],[-7596826.437934698,-4715656.644682625],[-7597004.326480986,-4715716.341107085],[-7597005.550995384,-4715740.248406765],[-7597262.2537411535,-4715780.332628469]],
        "VUELTA": [[-7597262.2537411535,-4715780.332628469],[-7597261.585824208,-4715793.78950984],[-7597226.854143082,-4716016.403276067],[-7597064.327686524,-4715986.482556415],[-7596989.4096692195,-4715979.467663623],[-7596961.802435502,-4715989.202618192],[-7596826.66057368,-4715988.773134706],[-7596820.649321177,-4716600.519214987],[-7596475.558899717,-4716534.374985973],[-7596227.093796266,-4716495.71946742],[-7596221.97309969,-4716454.200741217],[-7596218.633514967,-4716119.909604224],[-7596222.084419181,-4715995.358550169],[-7594918.31054301,-4715996.933324063],[-7594907.623871894,-4715503.468414098],[-7594600.827355268,-4715459.233828643],[-7594473.700496783,-4715407.8417855],[-7594294.253477624,-4715367.759035632],[-7594121.374308422,-4715348.004024316],[-7593282.804584275,-4715345.999894883],[-7593272.340552142,-4715328.678506993],[-7588935.667149306,-4715362.032941436],[-7588411.018389199,-4715449.356229109],[-7587310.959181179,-4715648.771079722],[-7584045.624557742,-4715682.126566638],[-7583538.452957686,-4715799.945323227],[-7580364.734275171,-4715848.762487246],[-7580187.402326336,-4715861.64682192],[-7580145.65751729,-4715862.362618769],[-7580125.286050473,-4715872.240620467],[-7579728.988663251,-4715861.074184476],[-7579426.756245745,-4715880.1143968245],[-7579110.608891894,-4715874.388013413],[-7579107.046668188,-4715591.079230352],[-7578495.012107806,-4715600.098033803],[-7578493.787593408,-4715677.688705831],[-7577255.692216804,-4715681.840252981],[-7577108.193891504,-4715689.284410742],[-7576301.684180708,-4715700.021186369],[-7576299.3464714,-4715234.198730027],[-7575988.765092087,-4715237.920643987],[-7575978.078420971,-4713612.4330702275],[-7576128.916330996,-4713616.011261632],[-7576123.127717475,-4713953.225682237]]
      },
      "50A": {
        "IDA": [[-7576123.127717475,-4713953.225682237],[-7576126.801260672,-4713962.24302984],[-7576139.269043639,-4714629.119566186],[-7575523.672259552,-4714622.248753801],[-7575532.243860344,-4715533.817173289],[-7576644.770851331,-4715532.09931657],[-7576650.893423325,-4715841.890848778],[-7577570.503736769,-4715836.450804959],[-7577572.618807093,-4715854.775174725],[-7583421.567492354,-4715798.943213817],[-7583560.828175335,-4715775.751566543],[-7583874.860458863,-4715693.006491674],[-7583955.121811725,-4715674.968727586],[-7584108.9653480025,-4715654.497335772],[-7584605.895554903,-4715650.202643431],[-7584680.479613734,-4715639.609076765],[-7587213.443307246,-4715620.139847957],[-7587287.6934076045,-4715615.98832606],[-7587825.1439091535,-4715524.941583376],[-7587911.973111974,-4715501.464253952],[-7588335.989052405,-4715432.607277967],[-7588433.282287358,-4715422.8728575315],[-7588473.2459845515,-4715410.847998125],[-7588863.4207997825,-4715348.719784924],[-7588928.320062916,-4715342.134789236],[-7589020.158642819,-4715337.553925193],[-7589548.258307143,-4715331.3983923895],[-7589632.638481165,-4715333.688822764],[-7590323.70988001,-4715321.807220816],[-7590518.518988898,-4715328.964810683],[-7591577.835263286,-4715320.089399999],[-7591637.613829843,-4715316.796910914],[-7593305.179801925,-4715308.350965536],[-7593380.877055665,-4715311.929755093],[-7594183.935862247,-4715296.898847442],[-7594201.413022301,-4715297.041998837],[-7594240.597483061,-4715293.320063189],[-7594253.176585521,-4715294.322122653],[-7594704.6884401785,-4715455.082372595],[-7594728.6221306985,-4715460.235904495],[-7594785.729029476,-4715461.524287879],[-7594910.963456619,-4715483.999446573],[-7594907.623871894,-4715503.468414098],[-7594908.40310833,-4715525.084737992],[-7594910.852137129,-4715545.985333369],[-7594911.742693054,-4715715.195848635],[-7594915.8615142135,-4715756.997865855],[-7594920.870891299,-4715997.07648534],[-7594941.242358114,-4715995.501711421],[-7596194.922463427,-4715989.202618192],[-7596224.422128488,-4715996.074356453],[-7596222.195738672,-4716125.492963153],[-7596224.978725942,-4716307.168549865],[-7596222.752336126,-4716466.083669633],[-7596229.542825065,-4716492.999269811],[-7596813.413554275,-4716592.501709711],[-7596817.643694925,-4716592.215370353],[-7596825.770017753,-4715985.480428577],[-7596942.3215246145,-4715989.345779359],[-7596959.576045686,-4715989.202618192],[-7596967.591049024,-4715980.040307751],[-7596987.851196348,-4715976.747604458],[-7597026.145101181,-4715979.038180549],[-7597225.518309192,-4716011.535784564],[-7597259.582073375,-4715789.065283496],[-7597261.140546246,-4715786.917908611]],
        "VUELTA": [[-7597250.119916658,-4715779.759995615],[-7597007.220787747,-4715742.538929453],[-7597004.103842004,-4715719.920040578],[-7596845.584887113,-4715664.947761397],[-7596828.553005023,-4715661.798316925],[-7596830.111477895,-4715759.002076489],[-7596504.168008851,-4715759.002076489],[-7596297.113755976,-4715770.884190514],[-7596210.395872649,-4715769.738925908],[-7596208.948719267,-4715766.875764949],[-7596206.611009962,-4715726.362124044],[-7596207.056287925,-4715632.451267713],[-7596197.705450697,-4715475.410208072],[-7596190.803642267,-4715001.009228269],[-7596188.688571943,-4714998.862020193],[-7595976.06834453,-4714941.603305795],[-7595930.761311775,-4714931.153625288],[-7595916.289777972,-4714929.722163043],[-7595639.772162842,-4714928.720139591],[-7595630.866603578,-4714922.135416516],[-7595635.3193832105,-4714889.35501089],[-7595631.9797984855,-4714870.602948748],[-7595618.064862136,-4714861.298503298],[-7595474.462719014,-4714812.915524512],[-7595452.421459837,-4714803.897420843],[-7595426.372698991,-4714789.439842796],[-7595328.745505566,-4714729.462585574],[-7595255.6086001145,-4714677.7879537875],[-7595245.36720696,-4714667.767972549],[-7595192.04517087,-4714632.984400197],[-7595172.564259982,-4714622.391895678],[-7595141.283483069,-4714613.230819607],[-7595096.755686752,-4714609.938559915],[-7594860.090449327,-4714611.226835318],[-7594858.865934927,-4714425.573722142],[-7594861.203644233,-4714366.600583722],[-7594859.867810343,-4713727.508338012],[-7594861.426283215,-4713711.764132275],[-7594881.797750031,-4713708.901552039],[-7595029.073436349,-4713712.193519382],[-7595138.945773764,-4713710.046584037],[-7595198.167742864,-4713705.8958436595],[-7595380.843027256,-4713707.470262223],[-7595379.061915404,-4713042.944207072],[-7595412.903040605,-4713040.797413317],[-7596165.088839895,-4713036.074468653],[-7596175.218913556,-4713035.6451101545],[-7596171.656689852,-4712581.966448992],[-7596175.441552539,-4712540.893072966],[-7596172.881204251,-4712379.320325308],[-7596175.107594066,-4712373.452807142],[-7596197.482811715,-4712371.592375263],[-7596824.545503356,-4712377.889223004],[-7596822.541752521,-4712057.756708993],[-7596881.095804678,-4712054.608383941],[-7597470.75514741,-4712047.309997799],[-7597479.660706673,-4712111.850810179],[-7597506.600023445,-4712129.166707716],[-7597508.381135297,-4712165.9451996675],[-7597617.028958312,-4712176.678247733],[-7598443.798816433,-4712290.449255041],[-7598466.507992555,-4712302.470417102],[-7598512.14898378,-4712306.191255862],[-7598514.709332068,-4711685.11629006],[-7598174.071690242,-4711687.4058975605],[-7598167.8377987575,-4711688.24747066],[-7598050.173096989,-4711681.681879779],[-7598003.752869328,-4711673.382059729],[-7597996.628421916,-4711680.250875837],[-7597786.123264827,-4711683.11288392],[-7597676.250927414,-4711692.700616881],[-7597591.202836447,-4711686.976596114],[-7597495.802032838,-4711686.976596114],[-7597490.681336261,-4711704.577970274],[-7597171.528356157,-4711708.727892203],[-7597137.909869937,-4711711.160605849],[-7596828.886963496,-4711713.593320077],[-7596821.985155067,-4711712.73471499],[-7596823.209669465,-4712007.526730327],[-7596508.06419103,-4712008.099150336],[-7596506.839676631,-4712376.028790309],[-7596181.11884657,-4712375.456349549],[-7596140.153273959,-4712379.320325308],[-7594850.628292607,-4712383.041192242],[-7594857.1961425645,-4712652.664565672],[-7594854.301835804,-4712803.221710374],[-7594862.09420016,-4713066.272728497],[-7594856.52822562,-4713349.654142329],[-7594859.867810343,-4713652.652014227],[-7594857.864059509,-4713715.485487791],[-7594743.650261955,-4713720.495006986],[-7594475.815567107,-4713722.785073727],[-7594427.614227593,-4713725.9339163415],[-7593642.366539538,-4713726.649562525],[-7593624.5554210115,-4713727.365208761],[-7593504.775648918,-4713719.922490382],[-7590854.25857313,-4713736.954873182],[-7590688.83780981,-4713749.407137522],[-7590621.266878899,-4713750.55217409],[-7590628.057367837,-4714427.434530633],[-7590588.427629116,-4714428.8659220105],[-7590470.428968875,-4714075.60465476],[-7590274.840623551,-4713565.201061941],[-7589298.012091841,-4714546.383843045],[-7589235.339218523,-4714600.92063666],[-7589116.338682866,-4714738.194336053],[-7588664.270230753,-4715194.25981618],[-7588577.774986408,-4715295.037879472],[-7588495.509882711,-4715372.196760856],[-7588405.118456188,-4715449.069922021],[-7587364.949134214,-4715630.590238186],[-7587261.533327267,-4715639.609076765],[-7585866.366149155,-4715643.044826904],[-7584155.385575662,-4715668.669833398],[-7583959.240632885,-4715693.435962638],[-7583560.048938901,-4715785.056850747],[-7583538.452957686,-4715799.945323227],[-7580226.475467606,-4715852.0551489955],[-7580186.51177041,-4715861.64682192],[-7580147.438629141,-4715861.64682192],[-7580123.282299641,-4715871.811141933],[-7579722.754771766,-4715863.078415671],[-7579662.419607755,-4715869.520590055],[-7579114.39375458,-4715874.388013413],[-7579106.935348698,-4715591.508697],[-7578491.561203592,-4715601.10012357],[-7578492.897037482,-4715678.977116833],[-7576303.242653578,-4715692.004392827],[-7576297.231401076,-4715693.292805648],[-7576297.342720565,-4715231.19256975],[-7576244.799920911,-4715237.204891196],[-7576147.72932494,-4715244.219270719],[-7575989.433009031,-4715241.499408697],[-7575987.87453616,-4715235.9165362995],[-7575991.548079357,-4715061.990127367],[-7575984.757590419,-4714937.595207889],[-7575987.095299726,-4714799.316799827],[-7575985.759465835,-4714749.932110833],[-7575975.963350645,-4714629.548992114],[-7575981.195366712,-4714455.9192570355],[-7575977.187865044,-4714218.453556359],[-7575975.963350645,-4714202.136045013],[-7575973.291682867,-4713941.345696835],[-7575971.176612541,-4713898.97863323],[-7575975.963350645,-4713611.00179402],[-7576126.022024234,-4713610.429283593],[-7576127.580497106,-4713825.266089471],[-7576125.910704744,-4713913.005546861],[-7576123.127717475,-4713953.225682237]]
      }
    };

    function mercatorALatLon(x, y) {
      const lon = (x / 20037508.34) * 180;
      let lat = (y / 20037508.34) * 180;
      lat = 180 / Math.PI * (2 * Math.atan(Math.exp(lat * Math.PI / 180)) - Math.PI / 2);
      return [lat, lon];
    }

    function distanciaMinima(puntos, lat, lon) {
      let minDist = Infinity;
      for (let p of puntos) {
        let d = Math.hypot(p[0] - lat, p[1] - lon);
        if (d < minDist) minDist = d;
      }
      return minDist;
    }

    let map;
    let capaRuta = null;
    let marcadores = {};
    let paradaMarcadores = {};
    let cooldownTimer = null;

    function initMap() {
      map = L.map('map', { zoomControl: false }).setView([-38.955, -68.16], 12);
      L.control.zoom({ position: 'topright' }).addTo(map);

      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 18,
        attribution: '&copy; OpenStreetMap'
      }).addTo(map);

      capaRuta = L.layerGroup().addTo(map);
    }

    function mostrarRuta(linea, latBus, lonBus) {
      capaRuta.clearLayers();
      if (!TRAZAS_METRICAS[linea]) return;

      const puntosIda = TRAZAS_METRICAS[linea]["IDA"].map(p => mercatorALatLon(p[0], p[1]));
      const puntosVuelta = TRAZAS_METRICAS[linea]["VUELTA"].map(p => mercatorALatLon(p[0], p[1]));

      let destacaIda = true;
      if (latBus && lonBus) {
        const dIda = distanciaMinima(puntosIda, latBus, lonBus);
        const dVuelta = distanciaMinima(puntosVuelta, latBus, lonBus);
        destacaIda = (dIda <= dVuelta);
      }

      const colorBase = (linea === "50A") ? "#2ECC71" : "#3B82F6";
      const colorSecundario = (linea === "50A") ? "#16A085" : "#1D4ED8";

      capaRuta.addLayer(L.polyline(puntosIda, {
        color: destacaIda ? colorBase : colorSecundario,
        weight: destacaIda ? 6 : 3.5,
        opacity: destacaIda ? 0.95 : 0.75,
        lineJoin: 'round'
      }));

      capaRuta.addLayer(L.polyline(puntosVuelta, {
        color: !destacaIda ? colorBase : colorSecundario,
        weight: !destacaIda ? 6 : 3.5,
        opacity: !destacaIda ? 0.95 : 0.75,
        lineJoin: 'round'
      }));

      document.getElementById('btn-clear').style.display = 'block';
    }

    function limpiarRuta() {
      capaRuta.clearLayers();
      document.getElementById('btn-clear').style.display = 'none';
    }

    function centrarEn(lat, lon, linea) {
      map.setView([lat, lon], 15, { animate: true });
      mostrarRuta(linea, lat, lon);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function iniciarCooldown(segundos) {
      const btn = document.getElementById('btn');
      btn.disabled = true;
      let restante = segundos;
      
      clearInterval(cooldownTimer);
      cooldownTimer = setInterval(() => {
        restante--;
        if (restante <= 0) {
          clearInterval(cooldownTimer);
          btn.disabled = false;
          btn.innerText = "Actualizar";
        } else {
          btn.innerText = `Espera (${restante}s)`;
        }
      }, 1000);
    }

     function dibujarParadas(paradas) {
       if (!Array.isArray(paradas)) return;

       const idsRecibidos = new Set();

       paradas.forEach(p => {
         if (!p || p.lat == null || p.lon == null) return;

         const id = `${p.tipo}-${p.parada}-${p.linea}`;
         idsRecibidos.add(id);

         const esCabecera = p.tipo === 'CABECERA';
         const icon = L.divIcon({
           className: esCabecera ? 'custom-stop-icon-header' : 'custom-stop-icon',
           html: `<div class="${esCabecera ? 'stop-marker-header' : 'stop-marker'}">${p.icono || (esCabecera ? '🏁' : '📍')}</div>`,
           iconSize: [esCabecera ? 30 : 28, esCabecera ? 30 : 28],
           iconAnchor: [esCabecera ? 15 : 14, esCabecera ? 15 : 14],
           popupAnchor: [0, esCabecera ? -13 : -12]
         });

         const titulo = p.calle || `${esCabecera ? 'Cabecera' : 'Parada'} ${p.parada}`;
         const tipoTexto = esCabecera ? 'Cabecera' : 'Parada';

         const popup = `
           <div style="font-family:sans-serif;font-size:13px;line-height:1.4;min-width:180px;">
             <strong style="color:#111;font-size:14px;">${tipoTexto} ${p.parada}</strong><br>
             <span style="color:#555;">${titulo}</span><br>
             <span style="color:#555;font-size:12px;">Línea ${p.linea}</span>
           </div>
         `;

         if (paradaMarcadores[id]) {
           paradaMarcadores[id].setLatLng([p.lat, p.lon]);
           paradaMarcadores[id].setIcon(icon);
           paradaMarcadores[id].getPopup().setContent(popup);
         } else {
           const marker = L.marker([p.lat, p.lon], { icon });
           marker.bindPopup(popup);
           marker.on('click', () => {
             map.setView([p.lat, p.lon], 16, { animate: true });
           });
           marker.addTo(map);
           paradaMarcadores[id] = marker;
         }
       });

       Object.keys(paradaMarcadores).forEach(id => {
         if (!idsRecibidos.has(id)) {
           map.removeLayer(paradaMarcadores[id]);
           delete paradaMarcadores[id];
         }
       });
     }

    async function pedirDatos() {
      const status = document.getElementById('status');

      try {
        const res = await fetch('/api/arribos');
        const data = await res.json();
        
        renderTarjetas(data.items);
        dibujarParadas(data.paradas || PARADAS_MAPA);
        actualizarMapa(data.buses);

        status.innerText = "Actualizado: " + data.hora;
        iniciarCooldown(6);
      } catch (err) {
        status.innerText = "Reintentando sincronización...";
      }
    }

    function actualizarMapa(buses) {
      const countLabel = document.getElementById('bus-count');
      if (!buses || buses.length === 0) {
        countLabel.innerText = "Sin unidades reportando";
        return;
      }

      const activos = buses.filter(b => b.estado === 'activo').length;
      const perdidos = buses.filter(b => b.estado === 'perdido').length;
      countLabel.innerText = `🚌 ${activos} en camino${perdidos > 0 ? ' | ⚠️ ' + perdidos + ' con señal débil' : ''}`;

      const idsRecibidos = new Set();

      buses.forEach(b => {
        idsRecibidos.add(b.id);
        const esPerdido = (b.estado === 'perdido');
        
        let claseCss = "bus-marker-50b";
        if (b.linea === "50A") claseCss = "bus-marker-50a";
        else if (b.linea === "50R") claseCss = "bus-marker-50r";
        
        if (esPerdido) claseCss = "bus-marker-lost";

        const iconoHtml = `<div class="bus-marker ${claseCss}">${esPerdido ? '⚠️' : '🚌'} ${b.linea}</div>`;
        const icon = L.divIcon({
          className: 'custom-icon',
          html: iconoHtml,
          iconSize: [58, 24],
          iconAnchor: [29, 12]
        });

        let sentidoTexto = (b.sentido_code === 'IDA') ? '🟢 Hacia Plottier' : '🟠 Hacia Neuquén';
        if (TRAZAS_METRICAS[b.linea]) {
          const pIda = TRAZAS_METRICAS[b.linea]["IDA"].map(p => mercatorALatLon(p[0], p[1]));
          const pVta = TRAZAS_METRICAS[b.linea]["VUELTA"].map(p => mercatorALatLon(p[0], p[1]));
          const dI = distanciaMinima(pIda, b.lat, b.lon);
          const dV = distanciaMinima(pVta, b.lat, b.lon);
          sentidoTexto = (dI <= dV) ? '🟢 Hacia Plottier' : '🟠 Hacia Neuquén';
        }

        const estadoColor = esPerdido ? '#e74c3c' : (sentidoTexto.includes('Plottier') ? '#2ecc71' : '#e67e22');
        const tiempoTexto = b.tiempo_cabecera 
          ? `<div style="color: #27ae60; font-weight: bold; margin-top: 5px;">⏱️ Arribo: ${b.tiempo_cabecera}</div>` 
          : `<div style="color: #7f8c8d; font-style: italic; margin-top: 5px;">📍 En trayecto intermedio</div>`;

        const contenidoPopup = `
          <div style="font-family: sans-serif; font-size: 13px; line-height: 1.4; min-width: 170px;">
            <strong style="color: #111; font-size: 14px;">Línea ${b.linea}</strong><br>
            <span style="color: ${estadoColor}; font-weight: 700;">${sentidoTexto}</span><br>
            <span style="color: #555; font-size: 12px;">Ramal: ${b.ramal}</span>
            ${tiempoTexto}
          </div>
        `;

        if (marcadores[b.id]) {
          marcadores[b.id].setLatLng([b.lat, b.lon]);
          marcadores[b.id].setIcon(icon);
          marcadores[b.id].getPopup().setContent(contenidoPopup);
        } else {
          const marker = L.marker([b.lat, b.lon], { icon: icon });
          marker.bindPopup(contenidoPopup);
          
          marker.on('click', () => {
            mostrarRuta(b.linea, b.lat, b.lon);
          });

          marker.addTo(map);
          marcadores[b.id] = marker;
        }
      });

      for (let id in marcadores) {
        if (!idsRecibidos.has(id)) {
          map.removeLayer(marcadores[id]);
          delete marcadores[id];
        }
      }
    }

    function renderTarjetas(items) {
      let html = '';
      let secActual = '';

      items.forEach(item => {
        if (item.seccion !== secActual) {
          secActual = item.seccion;
          html += `<div class="section-title">📍 ${secActual}</div>`;
        }

        let tagClase = "line-50b";
        if (item.linea === "50A") tagClase = "line-50a";
        else if (item.linea === "50R") tagClase = "line-50r";

        html += `<div class="card">
          <div class="card-header">
            <span class="line-tag ${tagClase}">Línea ${item.linea}</span>
            <span class="stop-tag">Cabecera ${item.parada}</span>
          </div>`;

        if (!item.arribos || item.arribos.length === 0) {
          html += `<div class="empty">Sin unidades reportando hacia cabecera</div>`;
        } else {
          item.arribos.forEach(c => {
            const botonVer = (c.lat && c.lon) 
              ? `<button class="btn-action" onclick="centrarEn(${c.lat}, ${c.lon}, '${item.linea}')">Ver en Mapa</button>` 
              : '';

            const badgeClass = c.sentido_code === 'IDA' ? 'badge-plottier' : 'badge-neuquen';
            const badgeIcon = c.sentido_code === 'IDA' ? '🟢' : '🟠';

            html += `<div class="arrival-row">
              <div>
                <div class="branch-row">
                  <span class="branch">${c.ramal}</span>
                  <span class="badge-status ${badgeClass}">${badgeIcon} ${c.sentido}</span>
                </div>
                <div class="time-label">A Cabecera: <span class="time-val">${c.tiempo}</span></div>
              </div>
              <div>
                ${botonVer}
              </div>
            </div>`;
          });
        }
        html += `</div>`;
      });

      document.getElementById('contenido').innerHTML = html;
    }

    initMap();
    pedirDatos();

    setInterval(pedirDatos, 30000);
  </script>
</body>
</html>
"""

@app.route('/ping')
def ping():
    return "OK", 200

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Colectivos Plottier",
        "short_name": "Bondis 50",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#181825",
        "theme_color": "#181825",
        "icons": [{
            "src": "https://cdn-icons-png.flaticon.com/512/1048/1048314.png",
            "sizes": "512x512",
            "type": "image/png"
        }]
    })

@app.route('/api/arribos')
def api_arribos():
    global flota_memoria, cabeceras_memoria, ultima_hora_sync, ultimo_escaneo_ts, indice_grupo_actual
    ahora = time.time()

    if (ahora - ultimo_escaneo_ts) < CACHE_TTL and cabeceras_memoria:
        return jsonify({
            "hora": ultima_hora_sync,
            "items": list(cabeceras_memoria.values()),
            "paradas": PARADAS_MAPA,
            "buses": serializar_flota(ahora),
            "from_cache": True
        })

    consultas_a_ejecutar = GRUPOS_CONSULTAS[indice_grupo_actual]
    indice_grupo_actual = (indice_grupo_actual + 1) % len(GRUPOS_CONSULTAS)

    consultas_ok = 0

    for item in consultas_a_ejecutar:
        try:
            arribos_raw = consultar_arribos(item["parada"], item["cod"])
            arribos_limpios = []
            es_cabecera = (item["seccion"] == "CABECERA")

            for c in arribos_raw:
                lat = c.get("latitud")
                lon = c.get("longitud")
                tiempo = c.get("tiempoRestanteArribo", "Sin datos")
                ramal = c.get("descripcionBandera", item["linea"])

                ramal_upper = str(ramal).upper()
                if "IDA" in ramal_upper:
                    sentido = "Hacia Plottier"
                    sentido_code = "IDA"
                elif "VUELTA" in ramal_upper:
                    sentido = "Hacia Neuquén"
                    sentido_code = "VUELTA"
                else:
                    sentido = "Hacia Plottier" if es_cabecera else "Hacia Neuquén"
                    sentido_code = "IDA" if es_cabecera else "VUELTA"

                if item["mostrar"]:
                    arribos_limpios.append({
                        "tiempo": tiempo,
                        "ramal": ramal,
                        "sentido": sentido,
                        "sentido_code": sentido_code,
                        "lat": lat,
                        "lon": lon
                    })

                if lat and lon and str(lat).strip() and str(lon).strip():
                    try:
                        f_lat = float(lat)
                        f_lon = float(lon)
                        vincular_o_crear_colectivo(item["linea"], ramal, sentido, sentido_code, tiempo, es_cabecera, f_lat, f_lon, ahora)
                    except ValueError:
                        pass

            if item["mostrar"]:
                clave_cab = (item["parada"], item["linea"])
                cabeceras_memoria[clave_cab] = {
                    "seccion": "CABECERA",
                    "parada": item["parada"],
                    "linea": item["linea"],
                    "arribos": arribos_limpios
                }

            consultas_ok += 1
            time.sleep(0.4)
        except Exception:
            pass

    flota_memoria = {k: v for k, v in flota_memoria.items() if (ahora - v["last_seen"]) < TIEMPO_EXPIRAR}

    if consultas_ok > 0 or not cabeceras_memoria:
        ultima_hora_sync = obtener_hora_arg()
        ultimo_escaneo_ts = ahora

    return jsonify({
        "hora": ultima_hora_sync,
        "items": list(cabeceras_memoria.values()),
        "paradas": PARADAS_MAPA,
        "buses": serializar_flota(ahora),
        "from_cache": False
    })

def vincular_o_crear_colectivo(linea, ramal, sentido, sentido_code, tiempo, es_cabecera, lat, lon, ahora):
    bus_existente_id = None
    for bus_id, datos in flota_memoria.items():
        if datos["linea"] == linea:
            dist = calcular_distancia(datos["lat"], datos["lon"], lat, lon)
            if dist < 2.5:
                bus_existente_id = bus_id
                break

    if bus_existente_id:
        flota_memoria[bus_existente_id]["lat"] = lat
        flota_memoria[bus_existente_id]["lon"] = lon
        flota_memoria[bus_existente_id]["last_seen"] = ahora
        flota_memoria[bus_existente_id]["ramal"] = ramal
        flota_memoria[bus_existente_id]["sentido"] = sentido
        flota_memoria[bus_existente_id]["sentido_code"] = sentido_code
        if es_cabecera:
            flota_memoria[bus_existente_id]["tiempo_cabecera"] = tiempo
    else:
        nuevo_id = f"{linea}{round(lat, 3)}{round(lon, 3)}"
        flota_memoria[nuevo_id] = {
            "id": nuevo_id,
            "linea": linea,
            "ramal": ramal,
            "sentido": sentido,
            "sentido_code": sentido_code,
            "tiempo_cabecera": tiempo if es_cabecera else None,
            "lat": lat,
            "lon": lon,
            "last_seen": ahora
        }

def serializar_flota(ahora):
    lista = []
    for bus_id, datos in flota_memoria.items():
        inactivo = ahora - datos["last_seen"]
        estado = "perdido" if inactivo > TIEMPO_PERDIDO else "activo"
        lista.append({
            "id": datos["id"],
            "linea": datos["linea"],
            "ramal": datos["ramal"],
            "sentido": datos["sentido"],
            "sentido_code": datos["sentido_code"],
            "tiempo_cabecera": datos["tiempo_cabecera"],
            "lat": datos["lat"],
            "lon": datos["lon"],
            "estado": estado
        })
    return lista

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
