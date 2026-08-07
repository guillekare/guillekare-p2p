"""
Backend del Comparador P2P USDT/VES
=====================================
Centraliza las consultas a Binance P2P, OKX P2P, BingX P2P (via P2P.Army) y
Bybit P2P (via Playwright, porque Bybit bloquea peticiones directas con
Akamai) y expone un unico endpoint sencillo para el frontend.

Ejecutar localmente:
    pip install fastapi uvicorn requests playwright
    playwright install chromium
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Desplegar gratis (recomendado para que la app funcione desde cualquier lado,
no solo en tu red local): Render.com, Railway.app o Fly.io. Sube esta carpeta
"backend" a un repo de GitHub y conectalo a cualquiera de esos servicios.

IMPORTANTE sobre Bybit + Playwright en produccion:
    Playwright necesita el binario de Chromium instalado en el servidor, no
    solo la libreria de Python. En Render, en el "Build Command" tenes que
    poner:
        pip install -r requirements.txt && playwright install --with-deps chromium
    Esto hace el build mas lento y pesado (puede tardar varios minutos y usa
    mas RAM en runtime). Si tu plan gratuito de Render/Railway anda muy justo
    de memoria, Bybit puede fallar por falta de recursos aunque el resto
    funcione bien - en ese caso, lo mas facil es comentar la fuente Bybit en
    "fuentes" del endpoint /api/precios y listo, el resto sigue funcionando.

Endpoint principal:
    GET /api/precios  -> JSON con mejor compra, mejor venta y spread
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
import requests
import time
import traceback

# ------------------------------------------------------------------
# Playwright: un solo navegador vivo durante toda la vida del server,
# para no pagar el costo de abrir/cerrar Chrome en cada request.
# ------------------------------------------------------------------
_pw = None
_browser = None
_bybit_page = None


async def iniciar_bybit_browser():
    global _pw, _browser, _bybit_page
    try:
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(headless=True)
        _bybit_page = await _browser.new_page()
        await _bybit_page.goto("https://www.bybit.com/es-AR/p2p/buy/USDT/VES")
        await _bybit_page.wait_for_timeout(4000)
        print("[Bybit] Navegador Playwright listo.")
    except Exception:
        print("[Bybit] No se pudo iniciar Playwright, esta fuente quedara vacia:")
        traceback.print_exc()
        _bybit_page = None


async def cerrar_bybit_browser():
    global _pw, _browser
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await iniciar_bybit_browser()
    yield
    await cerrar_bybit_browser()


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


def obtener_bingx_precios():
    """
    Trae precios de BingX P2P a traves de la API oficial de P2P.Army
    (agregador legitimo que si tiene acceso autorizado a BingX).
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


async def obtener_bybit_precios():
    """
    Trae precios de Bybit P2P usando el navegador Playwright que dejamos
    abierto en el arranque del server (asi Akamai lo ve como un navegador
    real y no bloquea la peticion con 403).
    Devuelve una tupla (lista_comprar, lista_vender).
    """
    global _bybit_page
    if _bybit_page is None:
        return [], []

    async def pedir(side: str):
        try:
            result = await _bybit_page.evaluate(f"""
                async () => {{
                    const res = await fetch("https://www.bybit.com/x-api/fiat/otc/item/online", {{
                        method: "POST",
                        headers: {{"Content-Type": "application/json;charset=UTF-8"}},
                        body: JSON.stringify({{
                            tokenId: "{ASSET}", currencyId: "{FIAT}",
                            payment: [], side: "{side}", size: "{ROWS}", page: "1",
                            amount: "", authMaker: false, bulkMaker: false,
                            canTrade: false, countryCode: "", itemRegion: 1,
                            paymentPeriod: [], sortType: "OVERALL_RANKING",
                            tradeWith: false, vaMaker: true, verificationFilter: 0
                        }}),
                        credentials: "include"
                    }});
                    return await res.json();
                }}
            """)
            items = result.get("result", {}).get("items", [])
            precios = []
            for item in items:
                precio = float(item.get("price", 0))
                if precio <= 0:
                    continue
                precios.append({
                    "precio": precio,
                    "comerciante": item.get("nickName", "desconocido"),
                    "min": float(item.get("minAmount", 0) or 0),
                    "max": float(item.get("maxAmount", 0) or 0),
                })
            return precios
        except Exception:
            traceback.print_exc()
            return []

    # side "1" = anuncios de venta de USDT (el usuario compra) -> nuestra "comprar"
    # side "0" = anuncios de compra de USDT (el usuario vende)  -> nuestra "vender"
    compra = await pedir("1")
    venta = await pedir("0")
    return compra, venta


def mejor(precios, modo):
    if not precios:
        return None
    return min(precios, key=lambda x: x["precio"]) if modo == "comprar" else max(precios, key=lambda x: x["precio"])


async def construir_respuesta():
    bingx_compra, bingx_venta = obtener_bingx_precios()
    bybit_compra, bybit_venta = await obtener_bybit_precios()

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
async def precios():
    now = time.time()
    if _cache["data"] and (now - _cache["ts"] < CACHE_SEGUNDOS):
        return _cache["data"]
    data = await construir_respuesta()
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
def debug_binance():
    """Devuelve la respuesta cruda de Binance tal cual, sin procesarla, para diagnostico."""
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
async def debug_bybit():
    """Devuelve compra/venta crudos de Bybit tal cual, sin procesar, para diagnostico."""
    try:
        compra, venta = await obtener_bybit_precios()
        return {"compra": compra, "venta": venta}
    except Exception as e:
        return {"error": str(e)}
