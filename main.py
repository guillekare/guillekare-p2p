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
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import time
import traceback

app = FastAPI(title="Comparador P2P USDT/VES")

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
    Binance. En su lugar, descartamos precios que se alejan demasiado
    (mas de MARGEN_OUTLIER) del promedio que la propia API entrega por
    metodo de pago, ya que ese tipo de anuncios trampa suelen ser valores
    bien atipicos.
    """
    MARGEN_OUTLIER = 0.03  # 3%
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

        compra, venta = [], []
        for entry in body.get("prices", []):
            metodo = entry.get("payment_method", "")
            avg_buy = entry.get("avg_price_BUY")
            avg_sell = entry.get("avg_price_SELL")
            for precio_str in entry.get("prices_BUY", []):
                try:
                    precio = float(precio_str)
                except (TypeError, ValueError):
                    continue
                # Para "comprar" buscamos el precio mas BAJO, asi que
                # descartamos los que esten demasiado por DEBAJO del
                # promedio (sospechosos de ser ofertas trampa).
                if avg_buy and precio < avg_buy * (1 - MARGEN_OUTLIER):
                    continue
                compra.append({"precio": precio, "comerciante": metodo, "min": 0, "max": 0})
            for precio_str in entry.get("prices_SELL", []):
                try:
                    precio = float(precio_str)
                except (TypeError, ValueError):
                    continue
                # Para "vender" buscamos el precio mas ALTO, asi que
                # descartamos los que esten demasiado por ENCIMA del
                # promedio.
                if avg_sell and precio > avg_sell * (1 + MARGEN_OUTLIER):
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
        if mc:
            compra.append({"plataforma": plataforma, **mc})
        if mv:
            venta.append({"plataforma": plataforma, **mv})

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
    que entrega P2P.Army), para diagnosticar si algun precio quedo "pegado".
    """
    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {"X-APIKEY": P2P_ARMY_API_KEY, "Content-Type": "application/json"}
    payload = {"market": "bybit", "fiat": FIAT, "asset": ASSET, "limit": ROWS}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        body = r.json()
        resumen = []
        for entry in body.get("prices", []):
            resumen.append({
                "metodo_pago": entry.get("payment_method"),
                "updated_BUY": entry.get("updated_BUY"),
                "updated_SELL": entry.get("updated_SELL"),
                "precio_min_BUY": min((float(p) for p in entry.get("prices_BUY", [])), default=None),
                "precio_min_SELL": min((float(p) for p in entry.get("prices_SELL", [])), default=None),
            })
        return {
            "status_code": r.status_code,
            "cantidad_metodos_pago": len(resumen),
            "metodos": resumen,
        }
    except Exception as e:
        return {"error": str(e)}
