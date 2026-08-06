"""
Backend del Comparador P2P — Multi-país
=====================================
Centraliza las consultas a Binance P2P, OKX P2P y BingX P2P (esta última vía
la API oficial de P2P.Army, ya que BingX no tiene API pública propia) y
expone un único endpoint sencillo para el frontend.

CONFIGURAR PARA OTRO PAÍS:
    Este backend ya no está fijo a Venezuela. La moneda (fiat) se controla
    con la variable de entorno FIAT_DEFAULT. Para adaptarlo a otro país:

    En Railway: ve a tu servicio -> pestaña "Variables" -> Agrega:
        FIAT_DEFAULT = COP   (o ARS, MXN, PEN, BRL, etc. — el código de
                               moneda que uses en Binance/OKX/BingX P2P)

    No hace falta tocar ni una línea de este archivo. Railway reinicia el
    servicio solo y ya queda funcionando para el país nuevo.

    Bonus: también puedes pedir un país puntual sin cambiar la variable,
    llamando al endpoint con ?fiat=COP, ej:
        /api/precios?fiat=COP

Ejecutar localmente:
    pip install fastapi uvicorn requests
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Endpoint principal:
    GET /api/precios              -> usa el país configurado por defecto
    GET /api/precios?fiat=COP     -> fuerza un país puntual
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import time
import traceback
import os

app = FastAPI(title="Comparador P2P Multi-país")

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

# ── Configuración de país/moneda ─────────────────────────────────────────
# FIAT_DEFAULT se lee de la variable de entorno del hosting (Railway).
# Si no está configurada, usa "VES" como respaldo.
FIAT_DEFAULT = os.environ.get("FIAT_DEFAULT", "VES")
ASSET = "USDT"
ROWS = 10
DEBUG = True

# API key de P2P.Army (plan gratuito). Es más seguro moverla a variable de
# entorno también (Railway: Variables -> P2P_ARMY_API_KEY).
P2P_ARMY_API_KEY = os.environ.get("P2P_ARMY_API_KEY", "GLJS5BWI-BEBVK7VO")

# Cache simple en memoria, separado por país, para no golpear las APIs de
# origen en cada request.
_cache = {}
CACHE_SEGUNDOS = 45


def obtener_binance(trade_type: str, fiat: str):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": ASSET, "fiat": fiat, "tradeType": trade_type,
        "page": 1, "rows": ROWS, "payTypes": [], "publisherType": None,
    }
    try:
        r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        precios = []
        for item in data:
            adv = item["adv"]
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


def obtener_okx(side: str, fiat: str):
    url = "https://www.okx.com/v3/c2c/tradingOrders/books"
    params = {
        "t": int(time.time() * 1000),
        "quoteCurrency": fiat.lower(),
        "baseCurrency": ASSET.lower(),
        "side": side,
        "paymentMethod": "all",
        "userType": "all",
        "showTrade": "false",
        "isAbleFilter": "false",
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


def obtener_bingx_precios(fiat: str):
    """
    Trae precios de BingX P2P a través de la API oficial de P2P.Army
    (agregador legítimo que sí tiene acceso autorizado a BingX).
    Devuelve una tupla (lista_comprar, lista_vender).
    """
    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {"X-APIKEY": P2P_ARMY_API_KEY, "Content-Type": "application/json"}
    payload = {"market": "bingx", "fiat": fiat, "asset": ASSET, "limit": ROWS}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if DEBUG:
            print(f"  [BingX/P2PArmy][DEBUG] status={r.status_code}")
            print(f"  [BingX/P2PArmy][DEBUG] respuesta cruda (primeros 500 chars):\n{r.text[:500]}\n")
        r.raise_for_status()
        body = r.json()
        if body.get("status") != 1:
            print(f"  [BingX/P2PArmy] Respuesta con error: {body}")
            return [], []

        compra, venta = [], []
        for entry in body.get("prices", []):
            metodo = entry.get("payment_method", "")
            for precio_str in entry.get("prices_BUY", []):
                try:
                    compra.append({"precio": float(precio_str), "comerciante": metodo, "min": 0, "max": 0})
                except (TypeError, ValueError):
                    continue
            for precio_str in entry.get("prices_SELL", []):
                try:
                    venta.append({"precio": float(precio_str), "comerciante": metodo, "min": 0, "max": 0})
                except (TypeError, ValueError):
                    continue
        return compra, venta
    except Exception:
        traceback.print_exc()
        return [], []


def mejor(precios, modo):
    if not precios:
        return None
    return min(precios, key=lambda x: x["precio"]) if modo == "comprar" else max(precios, key=lambda x: x["precio"])


UMBRAL_ATIPICO = 0.05  # descarta anuncios que se desvían más de 5% del precio de referencia del mercado


def filtrar_atipicos(precios, precio_referencia):
    if precio_referencia is None:
        return precios
    return [p for p in precios if abs(p["precio"] - precio_referencia) / precio_referencia <= UMBRAL_ATIPICO]


def mediana(valores):
    if not valores:
        return None
    valores = sorted(valores)
    n = len(valores)
    return valores[n // 2] if n % 2 else (valores[n // 2 - 1] + valores[n // 2]) / 2


def construir_respuesta(fiat: str):
    bingx_compra, bingx_venta = obtener_bingx_precios(fiat)
    fuentes = {
        "Binance": {"comprar": obtener_binance("BUY", fiat), "vender": obtener_binance("SELL", fiat)},
        "OKX": {"comprar": obtener_okx("buy", fiat), "vender": obtener_okx("sell", fiat)},
        "BingX": {"comprar": bingx_compra, "vender": bingx_venta},
    }

    todos = [p["precio"] for lados in fuentes.values() for p in lados["comprar"] + lados["vender"]]
    precio_referencia = mediana(todos)

    compra, venta = [], []
    for plataforma, lados in fuentes.items():
        c_validos = filtrar_atipicos(lados["comprar"], precio_referencia)
        v_validos = filtrar_atipicos(lados["vender"], precio_referencia)
        mc, mv = mejor(c_validos, "comprar"), mejor(v_validos, "vender")
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
        "par": f"{ASSET}/{fiat}",
        "mejor_compra": compra[:5],
        "mejor_venta": venta[:5],
        "spread": spread,
        "spread_pct": spread_pct,
    }


@app.get("/api/precios")
def precios(fiat: str = None):
    fiat_usado = (fiat or FIAT_DEFAULT).upper()
    now = time.time()
    cacheado = _cache.get(fiat_usado)
    if cacheado and (now - cacheado["ts"] < CACHE_SEGUNDOS):
        return cacheado["data"]
    data = construir_respuesta(fiat_usado)
    _cache[fiat_usado] = {"data": data, "ts": now}
    return data


@app.get("/")
def health():
    return {"status": "ok", "servicio": "Comparador P2P Multi-país", "fiat_por_defecto": FIAT_DEFAULT}


@app.get("/api/debug/okx")
def debug_okx(fiat: str = None):
    fiat_usado = (fiat or FIAT_DEFAULT).upper()
    url = "https://www.okx.com/v3/c2c/tradingOrders/books"
    params = {
        "t": int(time.time() * 1000),
        "quoteCurrency": fiat_usado.lower(),
        "baseCurrency": ASSET.lower(),
        "side": "buy",
        "paymentMethod": "all",
        "userType": "all",
        "showTrade": "false",
        "isAbleFilter": "false",
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        return {
            "status_code": r.status_code,
            "url_llamada": r.url,
            "respuesta_cruda": r.text[:2000],
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/binance")
def debug_binance(fiat: str = None):
    fiat_usado = (fiat or FIAT_DEFAULT).upper()
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": ASSET, "fiat": fiat_usado, "tradeType": "SELL",
        "page": 1, "rows": ROWS, "payTypes": [], "publisherType": None,
    }
    try:
        r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        return {
            "status_code": r.status_code,
            "respuesta_cruda": r.text[:2000],
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/bingx")
def debug_bingx(fiat: str = None):
    fiat_usado = (fiat or FIAT_DEFAULT).upper()
    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {"X-APIKEY": P2P_ARMY_API_KEY, "Content-Type": "application/json"}
    payload = {"market": "bingx", "fiat": fiat_usado, "asset": ASSET, "limit": ROWS}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        return {
            "status_code": r.status_code,
            "respuesta_cruda": r.text[:2000],
        }
    except Exception as e:
        return {"error": str(e)}
