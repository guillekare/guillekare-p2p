"""
Backend del Comparador P2P USDT/VES
=====================================
Centraliza las consultas a Binance P2P, OKX P2P, BingX P2P y Bybit P2P (estas
dos ultimas via la API oficial de P2P.Army, ya que ni BingX ni Bybit tienen
API publica accesible directamente sin bloqueos) y expone un unico endpoint
sencillo para el frontend.

Ejecutar localmente:
    pip install fastapi uvicorn requests
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Desplegar gratis (recomendado para que la app funcione desde cualquier lado,
no solo en tu red local): Render.com, Railway.app o Fly.io. Sube esta carpeta
"backend" a un repo de GitHub y conectalo a cualquiera de esos servicios;
todos detectan FastAPI/uvicorn automaticamente si agregas un Procfile
(incluido abajo) o usan el comando de start.

Nota sobre Bybit: probamos scrapear directo el endpoint de Bybit (con
requests y tambien con un navegador Playwright real), pero el servidor de
Bybit bloquea las conexiones que vienen de IPs de datacenter/nube (como las
de Railway/Render) a nivel de red, incluso antes de llegar a cualquier
proteccion tipo Akamai. Por eso Bybit se resuelve igual que BingX: via
P2P.Army, que ya tiene acceso autorizado a ambas exchanges.

Endpoint principal:
    GET /api/precios  -> JSON con mejor compra, mejor venta y spread

Pantalla de alerta:
    GET /alerta  -> pagina HTML simple, pensada para abrirse desde el
    celular al tocar la notificacion push. Muestra el spread, los precios,
    y un boton para copiar la direccion de deposito correspondiente sin
    tener que ir a buscarla a mano.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
import requests
import time
import traceback
import asyncio
import os

# ------------------------------------------------------------------
# Configuracion de alertas automaticas y direcciones de transferencia
# rapida entre Binance y OKX (via red TRC20, la de menor comision para
# USDT). Estos 3 valores conviene moverlos a variables de entorno en
# Railway (Settings -> Variables) en vez de dejarlos escritos aqui, ya
# que son datos personales tuyos aunque no sean secretos como una
# contrasena.
# ------------------------------------------------------------------
DIRECCION_BINANCE = os.environ.get("DIRECCION_BINANCE", "")
DIRECCION_OKX = os.environ.get("DIRECCION_OKX", "")
RED_TRANSFERENCIA = os.environ.get("RED_TRANSFERENCIA", "BEP20")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "guillekare-p2p-arbitraje")
UMBRAL_SPREAD_PCT = float(os.environ.get("UMBRAL_SPREAD_PCT", "1.0"))
INTERVALO_CHEQUEO_SEGUNDOS = 60
COOLDOWN_ALERTA_SEGUNDOS = 300  # no repetir la misma alerta antes de 5 min

_ultima_alerta = {"ts": 0, "clave": None}

# Guarda los datos completos de la ultima oportunidad detectada, para que
# la pantalla /alerta pueda mostrarlos cuando el usuario toca la
# notificacion en su celular.
_ultima_oportunidad_completa = {}


def calcular_spread_binance_okx():
    """
    Calcula las 2 direcciones posibles de arbitraje entre Binance y OKX
    (comprar en Binance y vender en OKX, o al reves) y devuelve la mejor
    opcion disponible en este momento, con el spread en porcentaje.
    """
    binance_compra = obtener_binance("BUY")
    binance_venta = obtener_binance("SELL")
    okx_compra = obtener_okx("sell")
    okx_venta = obtener_okx("buy")

    mejor_binance_compra = mejor(binance_compra, "comprar")
    mejor_binance_venta = mejor(binance_venta, "vender")
    mejor_okx_compra = mejor(okx_compra, "comprar")
    mejor_okx_venta = mejor(okx_venta, "vender")

    opciones = []
    if mejor_binance_compra and mejor_okx_venta:
        spread = mejor_okx_venta["precio"] - mejor_binance_compra["precio"]
        pct = round((spread / mejor_binance_compra["precio"]) * 100, 2)
        opciones.append({
            "comprar_en": "Binance", "precio_compra": mejor_binance_compra["precio"],
            "vender_en": "OKX", "precio_venta": mejor_okx_venta["precio"],
            "spread_pct": pct,
        })
    if mejor_okx_compra and mejor_binance_venta:
        spread = mejor_binance_venta["precio"] - mejor_okx_compra["precio"]
        pct = round((spread / mejor_okx_compra["precio"]) * 100, 2)
        opciones.append({
            "comprar_en": "OKX", "precio_compra": mejor_okx_compra["precio"],
            "vender_en": "Binance", "precio_venta": mejor_binance_venta["precio"],
            "spread_pct": pct,
        })

    if not opciones:
        return None
    return max(opciones, key=lambda o: o["spread_pct"])


def enviar_push_ntfy(titulo: str, mensaje: str, click_url: str | None = None):
    """
    Manda la notificacion push via ntfy.sh. Si se pasa click_url, la
    notificacion queda configurada para que, al tocarla en el celular, se
    abra directamente esa pagina (la pantalla /alerta) en el navegador,
    sin tener que buscar nada a mano.
    """
    try:
        headers = {
            "Title": titulo.encode("utf-8"),
            "Priority": "high",
            "Tags": "moneybag",
        }
        if click_url:
            headers["Click"] = click_url
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=mensaje.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
    except Exception:
        traceback.print_exc()


def _url_publica_actual() -> str:
    """
    Intenta armar la URL publica de este mismo servicio (para el boton de
    la notificacion), usando la variable que Railway agrega
    automaticamente. Si no la encuentra, devuelve vacio y simplemente no
    se agrega el boton de "abrir pantalla".
    """
    dominio = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    return f"https://{dominio}" if dominio else ""


async def loop_vigilancia_spread():
    while True:
        try:
            oportunidad = calcular_spread_binance_okx()
            if oportunidad and oportunidad["spread_pct"] >= UMBRAL_SPREAD_PCT:
                clave = f"{oportunidad['comprar_en']}->{oportunidad['vender_en']}"
                ahora = time.time()
                ya_avisado_hace_poco = (
                    _ultima_alerta["clave"] == clave
                    and (ahora - _ultima_alerta["ts"]) < COOLDOWN_ALERTA_SEGUNDOS
                )
                if not ya_avisado_hace_poco:
                    direccion_destino = (
                        DIRECCION_OKX if oportunidad["vender_en"] == "OKX" else DIRECCION_BINANCE
                    )
                    mensaje = (
                        f"Comprar en {oportunidad['comprar_en']} a {oportunidad['precio_compra']} VES\n"
                        f"Vender en {oportunidad['vender_en']} a {oportunidad['precio_venta']} VES\n"
                        f"Spread: {oportunidad['spread_pct']}%\n"
                        f"Enviar USDT ({RED_TRANSFERENCIA}) a: {direccion_destino or 'no configurada'}"
                    )

                    _ultima_oportunidad_completa.update({
                        "comprar_en": oportunidad["comprar_en"],
                        "precio_compra": oportunidad["precio_compra"],
                        "vender_en": oportunidad["vender_en"],
                        "precio_venta": oportunidad["precio_venta"],
                        "spread_pct": oportunidad["spread_pct"],
                        "direccion": direccion_destino,
                        "red": RED_TRANSFERENCIA,
                        "actualizado": int(ahora),
                    })

                    base_url = _url_publica_actual()
                    click_url = f"{base_url}/alerta" if base_url else None

                    enviar_push_ntfy(
                        f"Oportunidad P2P: {oportunidad['spread_pct']}%",
                        mensaje,
                        click_url=click_url,
                    )
                    _ultima_alerta["ts"] = ahora
                    _ultima_alerta["clave"] = clave
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(INTERVALO_CHEQUEO_SEGUNDOS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tarea = asyncio.create_task(loop_vigilancia_spread())
    yield
    tarea.cancel()


app = FastAPI(title="Comparador P2P USDT/VES", lifespan=lifespan)

# Permite que la PWA (desde cualquier origen) consuma esta API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

FIAT = "VES"
ASSET = "USDT"
ROWS = 20
DEBUG = True

# API key de P2P.Army (plan gratuito). Cuando despliegues esto en Render/Railway,
# es mas seguro moverla a una variable de entorno en vez de dejarla escrita aqui
# (Render: Settings -> Environment -> Add Variable, y luego
# P2P_ARMY_API_KEY = os.environ.get("P2P_ARMY_API_KEY")).
P2P_ARMY_API_KEY = "GLJS5BWI-BEBVK7VO"

# Cache simple en memoria para no golpear las APIs de origen en cada request
_cache = {"data": None, "ts": 0}
CACHE_SEGUNDOS = 45


# Metodos de pago que no son transferencia bancaria/pago movil normal (por
# ejemplo recargas de saldo telefonico, tarjetas de regalo, etc.) y por eso
# suelen tener precios fuera de mercado que no son comparables con las demas
# plataformas. Se excluyen de Binance para que la comparacion sea justa.
METODOS_PAGO_EXCLUIDOS = {
    "recarga pines", "recarga de saldo", "recargas de saldo",
    "gift card", "tarjeta de regalo", "efectivo", "cash",
}


def es_metodo_pago_valido(nombre_metodo: str) -> bool:
    if not nombre_metodo:
        return True
    return nombre_metodo.strip().lower() not in METODOS_PAGO_EXCLUIDOS


def obtener_binance(trade_type: str):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": ASSET, "fiat": FIAT, "tradeType": trade_type,
        "page": 1, "rows": ROWS, "payTypes": [], "publisherType": None,
    }
    try:
        r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        precios = []
        for item in data:
            adv = item["adv"]
            # Anuncios que exigen verificacion KYC adicional al comprador no
            # son operables para un usuario comun (Binance los oculta o los
            # muestra distinto en su propia app/web segun el nivel de cuenta
            # de cada quien). Los descartamos para reflejar el precio real
            # disponible, igual que ya se hace con "verificationRequired" en
            # OKX.
            if adv.get("takerAdditionalKycRequired") == 1:
                continue
            if adv.get("isTradable") is False:
                continue
            # Anuncios con muy poca cantidad disponible (ej: 1-2 USDT) suelen
            # ser "trampa" con precios irreales que en la practica nadie
            # puede operar de forma significativa. Los descartamos.
            try:
                if float(adv.get("surplusAmount", 0)) < 10:
                    continue
            except (TypeError, ValueError):
                pass
            metodos_pago = adv.get("tradeMethods", [])
            nombre_metodo = metodos_pago[0].get("tradeMethodName") if metodos_pago else None
            if not es_metodo_pago_valido(nombre_metodo):
                continue
            precios.append({
                "precio": float(adv["price"]),
                "comerciante": item["advertiser"]["nickName"],
                "min": float(adv.get("minSingleTransAmount", 0)),
                "max": float(adv.get("dynamicMaxSingleTransAmount", 0)),
            })
        return precios
    except Exception:
        traceback.print_exc()
        return []


def obtener_okx(side: str):
    url = "https://www.okx.com/v3/c2c/tradingOrders/books"
    params = {
        "t": int(time.time() * 1000),
        "quoteCurrency": FIAT,
        "baseCurrency": ASSET,
        "side": side,
        "paymentMethod": "all",
        "userType": "all",
        "showTrade": "false",
        "isAbleFilter": "false",
        "hideOverseasVerificationAds": "false",
        "acceptsVoucher": "false",
        "showFollow": "false",
        "showAlreadyTraded": "false",
        "amount": "10000",
        # OKX ordena de menor a mayor cuando side=sell, y de mayor a menor
        # cuando side=buy (asi es como lo pide la propia web al cambiar de
        # pestana). Replicamos ese mismo comportamiento aqui.
        "sortType": "price_asc" if side == "sell" else "price_desc",
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        body = r.json()
        raw = body.get("data", {})
        if isinstance(raw, dict):
            data = raw.get(side, []) or []
        elif isinstance(raw, list):
            data = raw
        else:
            data = []
        precios = []
        for item in data[:ROWS]:
            precio = float(item.get("price", 0))
            if precio <= 0:
                continue
            precios.append({
                "precio": precio,
                "comerciante": item.get("nickName", "desconocido"),
                "min": float(item.get("quoteMinAmountPerOrder", 0)),
                "max": float(item.get("quoteMaxAmountPerOrder", 0)),
            })
        return precios
    except Exception:
        traceback.print_exc()
        return []


def obtener_p2p_army(market: str, nombre_debug: str):
    """
    Trae precios de una exchange P2P a traves de la API oficial de P2P.Army
    (agregador legitimo que tiene acceso autorizado a estas exchanges).
    Sirve tanto para BingX como para Bybit (y cualquier otra que soporte
    P2P.Army: binance, bybit, huobi, okx, bitget, bingx, kucoin, mexc).
    Devuelve una tupla (lista_comprar, lista_vender).

    Nota: P2P.Army no entrega cantidad disponible ni limites de transaccion
    por anuncio, asi que no podemos filtrar anuncios "inelegibles" (poca
    cantidad vs. limite minimo alto) de forma exacta como se hace con
    Binance. En su lugar, descartamos precios que se alejan demasiado de la
    MEDIANA de TODO el mercado (todos los metodos de pago juntos, no solo el
    propio metodo del anuncio) ya que comparar contra el promedio de su
    propio metodo de pago falla cuando ese metodo tiene pocos anuncios y el
    propio outlier sesga su promedio hacia abajo/arriba.
    """
    import statistics

    MARGEN_OUTLIER = 0.015  # 1.5% respecto a la mediana global del mercado
    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {"X-APIKEY": P2P_ARMY_API_KEY, "Content-Type": "application/json"}
    payload = {"market": market, "fiat": FIAT, "asset": ASSET, "limit": ROWS}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if DEBUG:
            print(f"  [{nombre_debug}/P2PArmy][DEBUG] status={r.status_code}")
            print(f"  [{nombre_debug}/P2PArmy][DEBUG] respuesta cruda (primeros 500 chars):\n{r.text[:500]}\n")
        r.raise_for_status()
        body = r.json()
        if body.get("status") != 1:
            print(f"  [{nombre_debug}/P2PArmy] Respuesta con error: {body}")
            return [], []

        # Primera pasada: juntar TODOS los precios crudos (de todos los
        # metodos de pago) para calcular la mediana global del mercado.
        raw_buy, raw_sell = [], []
        entradas = []
        for entry in body.get("prices", []):
            metodo = entry.get("payment_method", "")
            precios_buy = []
            for precio_str in entry.get("prices_BUY", []):
                try:
                    precios_buy.append(float(precio_str))
                except (TypeError, ValueError):
                    continue
            precios_sell = []
            for precio_str in entry.get("prices_SELL", []):
                try:
                    precios_sell.append(float(precio_str))
                except (TypeError, ValueError):
                    continue
            raw_buy.extend(precios_buy)
            raw_sell.extend(precios_sell)
            entradas.append((metodo, precios_buy, precios_sell))

        mediana_buy = statistics.median(raw_buy) if raw_buy else None
        mediana_sell = statistics.median(raw_sell) if raw_sell else None

        # Segunda pasada: filtrar contra la mediana global.
        compra, venta = [], []
        for metodo, precios_buy, precios_sell in entradas:
            for precio in precios_buy:
                if mediana_buy and precio < mediana_buy * (1 - MARGEN_OUTLIER):
                    continue
                compra.append({"precio": precio, "comerciante": metodo, "min": 0, "max": 0})
            for precio in precios_sell:
                if mediana_sell and precio > mediana_sell * (1 + MARGEN_OUTLIER):
                    continue
                venta.append({"precio": precio, "comerciante": metodo, "min": 0, "max": 0})
        return compra, venta
    except Exception:
        traceback.print_exc()
        return [], []


def obtener_bingx_precios():
    return obtener_p2p_army("bingx", "BingX")


def obtener_bybit_precios():
    return obtener_p2p_army("bybit", "Bybit")


def mejor(precios, modo):
    if not precios:
        return None
    return min(precios, key=lambda x: x["precio"]) if modo == "comprar" else max(precios, key=lambda x: x["precio"])


PLATAFORMAS_CONFIABLES = {"Binance", "OKX"}


def construir_respuesta():
    bingx_compra, bingx_venta = obtener_bingx_precios()
    bybit_compra, bybit_venta = obtener_bybit_precios()

    fuentes = {
        "Binance": {"comprar": obtener_binance("BUY"), "vender": obtener_binance("SELL")},
        "OKX": {"comprar": obtener_okx("sell"), "vender": obtener_okx("buy")},
        "BingX": {"comprar": bingx_compra, "vender": bingx_venta},
        "Bybit": {"comprar": bybit_compra, "vender": bybit_venta},
    }

    compra, venta = [], []
    for plataforma, lados in fuentes.items():
        mc, mv = mejor(lados["comprar"], "comprar"), mejor(lados["vender"], "vender")
        confiable = plataforma in PLATAFORMAS_CONFIABLES
        if mc:
            compra.append({"plataforma": plataforma, "confiable": confiable, **mc})
        if mv:
            venta.append({"plataforma": plataforma, "confiable": confiable, **mv})

    compra.sort(key=lambda x: x["precio"])
    venta.sort(key=lambda x: x["precio"], reverse=True)

    spread = spread_pct = None
    if compra and venta:
        spread = round(venta[0]["precio"] - compra[0]["precio"], 4)
        spread_pct = round((spread / compra[0]["precio"]) * 100, 2)

    return {
        "actualizado": int(time.time()),
        "par": f"{ASSET}/{FIAT}",
        "mejor_compra": compra[:5],
        "mejor_venta": venta[:5],
        "spread": spread,
        "spread_pct": spread_pct,
    }


@app.get("/api/precios")
def precios():
    now = time.time()
    if _cache["data"] and (now - _cache["ts"] < CACHE_SEGUNDOS):
        return _cache["data"]
    data = construir_respuesta()
    _cache["data"] = data
    _cache["ts"] = now
    return data


@app.get("/")
def health():
    return {"status": "ok", "servicio": "Comparador P2P USDT/VES"}


@app.get("/api/direcciones")
def direcciones():
    """
    Direcciones de deposito USDT guardadas para transferencia rapida entre
    Binance y OKX. Configuralas en Railway -> Variables:
    DIRECCION_BINANCE, DIRECCION_OKX y RED_TRANSFERENCIA (ej: BEP20, TRC20).
    """
    return {
        "red": RED_TRANSFERENCIA,
        "binance": DIRECCION_BINANCE or None,
        "okx": DIRECCION_OKX or None,
    }


@app.get("/api/spread-binance-okx")
def spread_binance_okx():
    """
    Calcula en vivo la mejor oportunidad de arbitraje entre Binance y OKX
    (las 2 unicas fuentes con datos confiables en tiempo real), en ambas
    direcciones, y devuelve la mejor.
    """
    oportunidad = calcular_spread_binance_okx()
    if not oportunidad:
        return {"disponible": False}
    return {"disponible": True, **oportunidad, "umbral_configurado_pct": UMBRAL_SPREAD_PCT}


@app.get("/alerta", response_class=HTMLResponse)
def alerta():
    """
    Pantalla pensada para abrirse desde el celular al tocar la notificacion
    push. Muestra la ultima oportunidad detectada en grande, con un boton
    para copiar la direccion de deposito correspondiente sin tener que
    buscarla a mano en ningun lado.
    """
    o = _ultima_oportunidad_completa
    if not o:
        return """
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
        <body style="font-family:-apple-system,sans-serif;background:#0f172a;color:white;
        text-align:center;padding-top:35vh;margin:0;">
        <h2>Todavia no hay ninguna alerta registrada</h2>
        <p style="color:#94a3b8">En cuanto aparezca una oportunidad, esta pantalla se va a
        actualizar sola.</p>
        </body></html>
        """
    direccion = o.get("direccion") or "no configurada"
    return f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Oportunidad P2P</title>
      <style>
        body {{
          font-family: -apple-system, BlinkMacSystemFont, sans-serif;
          background: #0f172a; color: white; text-align: center;
          margin: 0; padding: 32px 20px; min-height: 100vh; box-sizing: border-box;
        }}
        .spread {{ font-size: 56px; color: #22c55e; font-weight: 800; margin: 8px 0 24px; }}
        .fila {{ font-size: 20px; margin: 10px 0; color: #e2e8f0; }}
        .fila b {{ color: white; }}
        .direccion {{
          background: #1e293b; padding: 20px; border-radius: 16px;
          margin: 28px 0 16px; word-break: break-all; font-size: 17px;
          font-family: monospace; border: 1px solid #334155;
        }}
        .red {{ color: #94a3b8; font-size: 14px; margin-bottom: 6px; }}
        button {{
          background: #22c55e; color: #0f172a; border: none;
          padding: 16px 28px; border-radius: 12px; font-size: 18px;
          font-weight: 700; width: 100%; max-width: 320px; box-sizing: border-box;
        }}
        .actualizado {{ color: #64748b; font-size: 13px; margin-top: 24px; }}
      </style>
    </head>
    <body>
      <h2 style="margin-bottom:0">Oportunidad de arbitraje</h2>
      <div class="spread">{o['spread_pct']}%</div>
      <div class="fila">Comprar en <b>{o['comprar_en']}</b> a <b>{o['precio_compra']}</b> VES</div>
      <div class="fila">Vender en <b>{o['vender_en']}</b> a <b>{o['precio_venta']}</b> VES</div>
      <div class="direccion">
        <div class="red">Enviar USDT ({o['red']}) a esta direccion:</div>
        <b id="dir">{direccion}</b>
      </div>
      <button onclick="navigator.clipboard.writeText('{direccion}').then(()=>{{this.innerText='Direccion copiada';}})">
        Copiar direccion
      </button>
      <div class="actualizado">Actualizado hace instantes</div>
    </body>
    </html>
    """


@app.get("/api/debug/okx")
def debug_okx():
    """Devuelve la respuesta cruda de OKX tal cual, sin procesarla, para diagnostico."""
    url = "https://www.okx.com/v3/c2c/tradingOrders/books"
    params = {
        "t": int(time.time() * 1000),
        "quoteCurrency": FIAT,
        "baseCurrency": ASSET,
        "side": "sell",
        "paymentMethod": "all",
        "userType": "all",
        "showTrade": "false",
        "isAbleFilter": "false",
        "hideOverseasVerificationAds": "false",
        "acceptsVoucher": "false",
        "showFollow": "false",
        "showAlreadyTraded": "false",
        "amount": "10000",
        "sortType": "price_asc",
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        body = r.json()
        raw = body.get("data", {})
        data = raw.get("buy", []) if isinstance(raw, dict) else raw
        resumen = [
            {"precio": item.get("price"), "comerciante": item.get("nickName"),
             "operaciones_completadas": item.get("completedOrderQuantity"),
             "tasa_completado": item.get("completedRate"),
             "verificationRequired": item.get("verificationRequired")}
            for item in data[:10]
        ]
        return {
            "status_code": r.status_code,
            "url_llamada": r.url,
            "resumen_primeros_10": resumen,
            "respuesta_cruda": r.text[:1500],
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/binance")
def debug_binance(trade_type: str = "BUY"):
    """
    Devuelve un resumen legible de los anuncios de Binance (precio,
    comerciante, cantidad disponible, y si requieren KYC adicional o no son
    operables), para diagnosticar diferencias contra lo que muestra la
    app/web oficial. Usar ?trade_type=SELL en la URL para ver el lado de
    "vender" (los compradores que le pagan al usuario).
    """
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": ASSET, "fiat": FIAT, "tradeType": trade_type.upper(),
        "page": 1, "rows": ROWS, "payTypes": [], "publisherType": None,
    }
    try:
        r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        data = r.json().get("data", [])
        resumen = [
            {
                "precio": adv.get("price"),
                "comerciante": item.get("advertiser", {}).get("nickName"),
                "cantidad_disponible": adv.get("surplusAmount"),
                "kyc_adicional_requerido": adv.get("takerAdditionalKycRequired"),
                "es_operable": adv.get("isTradable"),
                "metodo_pago": adv.get("tradeMethods", [{}])[0].get("tradeMethodName") if adv.get("tradeMethods") else None,
            }
            for item in data
            for adv in [item.get("adv", {})]
        ]
        return {
            "status_code": r.status_code,
            "trade_type": trade_type.upper(),
            "cantidad_anuncios": len(resumen),
            "anuncios": resumen,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/bingx")
def debug_bingx():
    """Devuelve la respuesta cruda de P2P.Army (BingX) tal cual, sin procesarla, para diagnostico."""
    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {"X-APIKEY": P2P_ARMY_API_KEY, "Content-Type": "application/json"}
    payload = {"market": "bingx", "fiat": FIAT, "asset": ASSET, "limit": ROWS}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        return {
            "status_code": r.status_code,
            "respuesta_cruda": r.text[:2000],
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/bybit")
def debug_bybit():
    """
    Devuelve un resumen legible de los anuncios de Bybit por metodo de pago
    (precio minimo/maximo de compra y venta, y el timestamp de actualizacion
    que entrega P2P.Army), mas la mediana global usada para filtrar
    outliers, para diagnosticar el filtrado.
    """
    import statistics
    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {"X-APIKEY": P2P_ARMY_API_KEY, "Content-Type": "application/json"}
    payload = {"market": "bybit", "fiat": FIAT, "asset": ASSET, "limit": ROWS}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        body = r.json()
        resumen = []
        raw_buy, raw_sell = [], []
        for entry in body.get("prices", []):
            precios_buy = [float(p) for p in entry.get("prices_BUY", []) if p]
            precios_sell = [float(p) for p in entry.get("prices_SELL", []) if p]
            raw_buy.extend(precios_buy)
            raw_sell.extend(precios_sell)
            resumen.append({
                "metodo_pago": entry.get("payment_method"),
                "updated_BUY": entry.get("updated_BUY"),
                "updated_SELL": entry.get("updated_SELL"),
                "precio_min_BUY": min(precios_buy, default=None),
                "precio_min_SELL": min(precios_sell, default=None),
            })
        return {
            "status_code": r.status_code,
            "mediana_BUY_mercado": statistics.median(raw_buy) if raw_buy else None,
            "mediana_SELL_mercado": statistics.median(raw_sell) if raw_sell else None,
            "cantidad_metodos_pago": len(resumen),
            "metodos": resumen,
        }
    except Exception as e:
        return {"error": str(e)}
