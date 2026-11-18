import threading, time, json, logging
from decimal import Decimal
import httpx

log = logging.getLogger("prices")
BINANCE = "https://data-api.binance.vision"
PRICES = {}   # e.g. BTCUSDT -> {last, open, high, low, vol, qvol, change}
WATCH = set(["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "TRXUSDT"])
_kl_cache = {}
_lock = threading.Lock()


def _fetch_binance(symbols):
    r = httpx.get(f"{BINANCE}/api/v3/ticker/24hr", params={"symbols": json.dumps(sorted(symbols), separators=(",", ":"))}, timeout=8)
    r.raise_for_status()
    out = {}
    for t in r.json():
        out[t["symbol"]] = {"last": Decimal(t["lastPrice"]), "open": Decimal(t["openPrice"]), "high": Decimal(t["highPrice"]),
                            "low": Decimal(t["lowPrice"]), "vol": Decimal(t["volume"]), "qvol": Decimal(t["quoteVolume"]),
                            "change": Decimal(t["priceChangePercent"]), "ts": time.time()}
    return out


def _fetch_okx(symbols):
    out = {}
    r = httpx.get("https://www.okx.com/api/v5/market/tickers", params={"instType": "SPOT"}, timeout=8)
    data = {d["instId"]: d for d in r.json().get("data", [])}
    for s in symbols:
        d = data.get(s.replace("USDT", "-USDT"))
        if not d:
            continue
        last, op = Decimal(d["last"]), Decimal(d["open24h"])
        out[s] = {"last": last, "open": op, "high": Decimal(d["high24h"]), "low": Decimal(d["low24h"]), "vol": Decimal(d["vol24h"]),
                  "qvol": Decimal(d["volCcy24h"]), "change": ((last - op) / op * 100) if op else Decimal(0), "ts": time.time()}
    return out


def refresh():
    syms = set(WATCH)
    try:
        data = _fetch_binance(syms)
    except Exception as e:
        log.warning("binance price failed %s, using okx", e)
        try:
            data = _fetch_okx(syms)
        except Exception as e2:
            log.warning("okx failed %s", e2)
            return
    with _lock:
        PRICES.update(data)


def loop():
    while True:
        refresh()
        time.sleep(3)


def start():
    refresh()
    threading.Thread(target=loop, daemon=True).start()


def ext_price(symbol):
    p = PRICES.get(symbol)
    if p and time.time() - p["ts"] < 120:
        return p["last"]
    return None


def ext_klines(symbol, interval="15m", limit=300):
    key = (symbol, interval, limit)
    c = _kl_cache.get(key)
    if c and time.time() - c[0] < 10:
        return c[1]
    r = httpx.get(f"{BINANCE}/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=8)
    r.raise_for_status()
    rows = [[int(k[0] / 1000), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in r.json()]
    _kl_cache[key] = (time.time(), rows)
    return rows


def ext_depth(symbol, limit=20):
    key = ("d", symbol)
    c = _kl_cache.get(key)
    if c and time.time() - c[0] < 2:
        return c[1]
    r = httpx.get(f"{BINANCE}/api/v3/depth", params={"symbol": symbol, "limit": limit}, timeout=8)
    r.raise_for_status()
    d = r.json()
    _kl_cache[key] = (time.time(), d)
    return d
