"""
Backend del Comparador P2P USDT/VES
=====================================
Centraliza las consultas a Binance P2P, OKX P2P y BingX P2P (esta última vía
la API oficial de P2P.Army, ya que BingX no tiene API pública propia) y
expone un único endpoint sencillo para el frontend.

Ejecutar localmente:
    pip install fastapi uvicorn requests
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Desplegar gratis (recomendado para que la app funcione desde cualquier lado,
no solo en tu red local): Render.com, Railway.app o Fly.io. Sube esta carpeta
"backend" a un repo de GitHub y conéctalo a cualquiera de esos servicios;
todos detectan FastAPI/uvicorn automáticamente si agregas un Procfile
(incluido abajo) o usan el comando de start.

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
# es más seguro moverla a una variable de entorno en vez de dejarla escrita aquí
# (Render: Settings -> Environment -> Add Variable, y luego
# P2P_ARMY_API_KEY = os.environ.get("P2P_ARMY_API_KEY")).
P2P_ARMY_API_KEY = "GLJS5BWI-BEBVK7VO"

# Cache simple en memoria para no golpear las APIs de origen en cada request
_cache = {"data": None, "ts": 0}
CACHE_SEGUNDOS = 45


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
        OKX_MIN_OPERACIONES = 100
        OKX_MIN_TASA_EXITO = 0.95
        for item in data[:ROWS]:
            precio = float(item.get("price", 0))
            if precio <= 0:
                continue
            operaciones = item.get("completedOrderQuantity", 0) or 0
            try:
                tasa = float(item.get("completedRate", 0) or 0)
            except (TypeError, ValueError):
                tasa = 0
            if operaciones < OKX_MIN_OPERACIONES or tasa < OKX_MIN_TASA_EXITO:
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


def obtener_bingx_precios():
    """
    Trae precios de BingX P2P a través de la API oficial de P2P.Army
    (agregador legítimo que sí tiene acceso autorizado a BingX).
    Devuelve una tupla (lista_comprar, lista_vender).
    """
    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {"X-APIKEY": P2P_ARMY_API_KEY, "Content-Type": "application/json"}
    payload = {"market": "bingx", "fiat": FIAT, "asset": ASSET, "limit": ROWS}
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


UMBRAL_ATIPICO = 0.08  # descarta anuncios que se desvían más de 8% del precio de referencia del mercado


def filtrar_atipicos(precios, precio_referencia):
    """
    Elimina anuncios cuyo precio se desvía demasiado del precio de referencia
    del mercado (ej. comerciantes con condiciones raras, errores de precio,
    o trampas). Si no hay suficientes datos para calcular una referencia
    confiable, no filtra nada.
    """
    if precio_referencia is None:
        return precios
    return [p for p in precios if abs(p["precio"] - precio_referencia) / precio_referencia <= UMBRAL_ATIPICO]


def mediana(valores):
    if not valores:
        return None
    valores = sorted(valores)
    n = len(valores)
    return valores[n // 2] if n % 2 else (valores[n // 2 - 1] + valores[n // 2]) / 2


def construir_respuesta():
    bingx_compra, bingx_venta = obtener_bingx_precios()
    fuentes = {
        "Binance": {"comprar": obtener_binance("BUY"), "vender": obtener_binance("SELL")},
        "OKX": {"comprar": obtener_okx("sell"), "vender": obtener_okx("buy")},
        "BingX": {"comprar": bingx_compra, "vender": bingx_venta},
    }

    # El precio de referencia del mercado se calcula con la mediana de TODOS
    # los anuncios (compra + venta, de las tres plataformas juntas). Usar el
    # mercado completo como referencia -en vez de comparar "venta contra venta"
    # únicamente- evita que un grupo pequeño de anuncios de venta ya inflados
    # se validen entre sí como si fueran "normales".
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
    """Devuelve la respuesta cruda de OKX tal cual, sin procesarla, para diagnóstico."""
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
def debug_binance():
    """Devuelve la respuesta cruda de Binance tal cual, sin procesarla, para diagnóstico."""
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": ASSET, "fiat": FIAT, "tradeType": "SELL",
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
def debug_bingx():
    """Devuelve la respuesta cruda de P2P.Army (BingX) tal cual, sin procesarla, para diagnóstico."""
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
