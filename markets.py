"""Boersenkurse fuer den Chat: suchen, Kurse, Kerzen -- von Yahoo Finance.

Dieselbe Quelle wie ~/Documents/devel/boersenchart: der Chart-Endpunkt v8 liefert
Kerzen und mit ihnen den Kurs (meta), die Suche v1 findet Symbole zu Namen. Der
Kurs-Endpunkt v7 will ein Cookie und bleibt aussen vor. Yahoo antwortet einem
vollen Browser-User-Agent mit 429, einem kurzen nicht. Die Daten sind je nach
Boerse bis zu 15 Minuten verzoegert; jede Antwort traegt ihren Zeitpunkt.

Ein kurzer Cache (Sekunden fuer Intraday, Minuten fuer Tageskerzen) haelt die
Anfragen klein, wenn Chat-Modell und Karte gleichzeitig fragen.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode

BASE = "https://query1.finance.yahoo.com"
UA = "Mozilla/5.0"
RANGES = ["1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max"]
INTERVALS = ["1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"]
# Kerzen je Zeitraum, so dass es etwa 50-250 werden
DEFAULT_INTERVAL = {"1d": "5m", "5d": "15m", "1mo": "60m", "3mo": "1d", "6mo": "1d", "ytd": "1d", "1y": "1d",
                    "2y": "1wk", "5y": "1wk", "10y": "1mo", "max": "1mo"}
# was man so tippt: 1t, 1w, 3m, 1j, …
RANGE_WORDS = {"1t": "1d", "1d": "1d", "tag": "1d", "heute": "1d", "5t": "5d", "5d": "5d", "1w": "5d", "woche": "5d",
               "1m": "1mo", "1mo": "1mo", "monat": "1mo", "3m": "3mo", "3mo": "3mo", "6m": "6mo", "6mo": "6mo",
               "ytd": "ytd", "1j": "1y", "1y": "1y", "jahr": "1y", "2j": "2y", "2y": "2y", "5j": "5y", "5y": "5y",
               "10j": "10y", "10y": "10y", "max": "max", "alles": "max"}
# bekannte Namen, die die Suche schlecht trifft
ALIASES = {"dax": "^GDAXI", "mdax": "^MDAXI", "tecdax": "^TECDAX", "sdax": "^SDAXI", "eurostoxx": "^STOXX50E",
           "euro stoxx": "^STOXX50E", "dow": "^DJI", "dow jones": "^DJI", "s&p": "^GSPC", "s&p 500": "^GSPC",
           "sp500": "^GSPC", "nasdaq": "^IXIC", "nasdaq 100": "^NDX", "nikkei": "^N225", "gold": "GC=F",
           "silber": "SI=F", "öl": "BZ=F", "brent": "BZ=F", "bitcoin": "BTC-EUR", "btc": "BTC-EUR",
           "ethereum": "ETH-EUR", "eth": "ETH-EUR", "euro": "EURUSD=X", "eur/usd": "EURUSD=X", "eurusd": "EURUSD=X"}
CACHE_INTRADAY = 10
CACHE_DAILY = 300

_cache = {}
_lock = threading.Lock()


def _get(path, params, ttl):
    url = f"{BASE}{path}?{urlencode(params)}"
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = ((json.loads(e.read()).get("chart") or {}).get("error") or {}).get("description")
        except ValueError:
            detail = None
        if e.code == 429:
            raise RuntimeError("Yahoo bremst gerade (zu viele Anfragen); gleich noch einmal versuchen") from None
        raise RuntimeError(detail or f"Yahoo antwortet mit {e.code}") from None
    except (OSError, ValueError, urllib.error.URLError) as e:
        raise RuntimeError(f"Yahoo nicht erreichbar: {getattr(e, 'reason', e)}") from None
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[url] = (now, data)
    return data


def search(text, count=8):
    text = str(text or "").strip()
    if not text:
        raise ValueError("Suchbegriff fehlt")
    data = _get("/v1/finance/search", {"q": text, "quotesCount": count, "newsCount": 0, "lang": "de-DE"}, CACHE_DAILY)
    out = []
    for q in data.get("quotes") or []:
        if not q.get("symbol") or q.get("quoteType") in ("OPTION", "FUTURE") and "=" not in q["symbol"]:
            continue
        out.append({"symbol": q["symbol"], "name": (q.get("longname") or q.get("shortname") or "").strip(),
                    "exchange": q.get("exchDisp"), "type": q.get("typeDisp") or q.get("quoteType")})
    return {"query": text, "results": out}


def _symbol(text):
    """Ein Symbol zu einem Namen oder Kuerzel: bekannte Namen, dann die Suche.
    Was wie ein Symbol aussieht (SAP.DE, ^GDAXI, EURUSD=X, NVDA), bleibt wie es ist."""
    t = str(text or "").strip()
    if not t:
        raise ValueError("Symbol fehlt")
    if t.lower() in ALIASES:
        return ALIASES[t.lower()]
    if re.fullmatch(r"\^?[A-Z0-9][A-Z0-9.\-=^]{0,14}", t) and any(c in t for c in ".^=-"):
        return t
    hits = search(t, 6)["results"]
    if not hits:
        raise ValueError(f"Kein Wertpapier zu „{t}“ gefunden")
    # ein gross getipptes Kuerzel gilt woertlich (NVDA, SAP an der NYSE) ...
    if t.isupper() and any(h["symbol"] == t for h in hits):
        return t
    # ... ein Name meint das Unternehmen, das so heisst (Siemens, nicht Siemens Energy),
    # und unter gleich guten die deutsche Notierung (Siemens -> SIE.DE)
    want = _core(t)

    def rank(item):
        i, h = item
        core = _core(h["name"])
        return (core != want, not core.startswith(want), len(core.split()), not h["symbol"].endswith(".DE"), i)
    return min(enumerate(hits), key=rank)[1]["symbol"]


LEGAL = {"ag", "se", "sa", "nv", "plc", "inc", "corp", "corporation", "co", "company", "kgaa", "ltd", "limited",
         "aktiengesellschaft", "holding", "holdings", "group", "the", "n", "vz", "st", "adr", "class", "a"}


def _core(name):
    """Der Name ohne Rechtsform und Zusaetze: "SIEMENS AG   N" -> "siemens"."""
    words = re.findall(r"[a-zäöüß0-9&]+", str(name or "").lower())
    return " ".join(w for w in words if w not in LEGAL) or " ".join(words)


def _meta(m, symbol):
    price, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
    change = price - prev if price is not None and prev else None
    return {
        "symbol": m.get("symbol") or symbol,
        "name": (m.get("longName") or m.get("shortName") or symbol).strip(),
        "currency": m.get("currency"), "exchange": m.get("fullExchangeName") or m.get("exchangeName"),
        "type": m.get("instrumentType"), "timezone": m.get("exchangeTimezoneName"),
        "price": price, "previous_close": prev,
        "change": round(change, 6) if change is not None else None,
        "change_pct": round(100 * change / prev, 3) if change is not None and prev else None,
        "day_high": m.get("regularMarketDayHigh"), "day_low": m.get("regularMarketDayLow"),
        "volume": m.get("regularMarketVolume"),
        "year_high": m.get("fiftyTwoWeekHigh"), "year_low": m.get("fiftyTwoWeekLow"),
        "time": m.get("regularMarketTime"),
    }


def _chart(symbol, rng, interval):
    intraday = interval[-1] in "mh" and interval not in ("1mo", "3mo")
    data = _get(f"/v8/finance/chart/{quote(symbol, safe='')}",
                {"range": rng, "interval": interval, "includePrePost": "false", "events": "div,splits"},
                CACHE_INTRADAY if intraday else CACHE_DAILY)
    chart = data.get("chart") or {}
    if chart.get("error"):
        raise ValueError((chart["error"] or {}).get("description") or f"Kein Wertpapier {symbol}")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise ValueError(f"Keine Kurse zu {symbol}")
    return result, intraday


def quotes(symbols):
    """Aktuelle Kurse zu mehreren Symbolen oder Namen, parallel."""
    if isinstance(symbols, str):
        symbols = [s for s in re.split(r"[,;\n]+", symbols) if s.strip()]
    symbols = [str(s).strip() for s in symbols or [] if str(s).strip()][:12]
    if not symbols:
        raise ValueError("Keine Symbole angegeben")

    def one(text):
        try:
            sym = _symbol(text)
            result, _ = _chart(sym, "1d", "5m")
            return _meta(result.get("meta") or {}, sym)
        except (ValueError, RuntimeError) as e:
            return {"symbol": text, "error": str(e)}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return {"quotes": list(pool.map(one, symbols)), "source": "Yahoo Finance", "fetched": time.time()}


def candles(symbol, rng="3mo", interval=None):
    """Kerzen eines Wertpapiers mit Kurs und Kennzahlen fuer das Modell."""
    rng = RANGE_WORDS.get(str(rng or "3mo").lower(), str(rng or "3mo").lower())
    if rng not in RANGES:
        raise ValueError(f"Zeitraum {rng!r} gibt es nicht; möglich: {', '.join(RANGES)}")
    interval = str(interval or DEFAULT_INTERVAL[rng]).lower()
    if interval not in INTERVALS:
        raise ValueError(f"Intervall {interval!r} gibt es nicht; möglich: {', '.join(INTERVALS)}")
    sym = _symbol(symbol)
    result, intraday = _chart(sym, rng, interval)
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for i, t in enumerate(result.get("timestamp") or []):
        o, h, l, c = (q.get(k, [None])[i] if i < len(q.get(k) or []) else None for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [None] * (i + 1))[i]
        rows.append([t, round(o, 6), round(h, 6), round(l, 6), round(c, 6), v or 0])
    if not rows:
        raise ValueError(f"Keine Kerzen zu {sym} im Zeitraum {rng}")
    # Der Vortagesschluss steht nur im Tageschart: bei laengeren Zeitraeumen meint Yahoos
    # chartPreviousClose den Schluss vor dem Zeitraum, nicht den von gestern.
    day = result if rng == "1d" else _chart(sym, "1d", "5m")[0]
    meta = _meta(day.get("meta") or {}, sym)
    return {"meta": meta, "range": rng, "interval": interval, "intraday": intraday, "candles": rows,
            "stats": stats(rows), "source": "Yahoo Finance", "fetched": time.time()}


def stats(rows):
    """Kennzahlen ueber die Kerzen: was das Modell zum Einordnen braucht."""
    closes = [r[4] for r in rows]
    first, last = rows[0][1], closes[-1]
    out = {"candles": len(rows), "first_open": first, "last_close": last,
           "change_pct": round(100 * (last - first) / first, 2) if first else None,
           "high": max(r[2] for r in rows), "low": min(r[3] for r in rows),
           "from": rows[0][0], "to": rows[-1][0]}
    for n in (20, 50, 200):
        if len(closes) >= n:
            out[f"sma{n}"] = round(sum(closes[-n:]) / n, 4)
    if len(closes) > 14:   # RSI nach Wilder
        gains = losses = 0.0
        for a, b in zip(closes[:14], closes[1:15]):
            gains += max(0, b - a)
            losses += max(0, a - b)
        gain, loss = gains / 14, losses / 14
        for a, b in zip(closes[14:], closes[15:]):
            gain = (gain * 13 + max(0, b - a)) / 14
            loss = (loss * 13 + max(0, a - b)) / 14
        out["rsi14"] = round(100 - 100 / (1 + gain / loss), 1) if loss else 100.0
    returns = [(b - a) / a for a, b in zip(closes, closes[1:]) if a]
    if len(returns) > 2:
        mean = sum(returns) / len(returns)
        out["volatility_pct_per_candle"] = round(100 * (sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)) ** 0.5, 3)
    return out


def resolve(text):
    """Fuer den Schnellweg: "SAP, Siemens 1j" -> [(Symbol oder Name, Zeitraum)]."""
    out = []
    for part in re.split(r"[,;\n]+", str(text or "")):
        words = part.split()
        rng = None
        if words and words[-1].lower() in RANGE_WORDS:
            rng = RANGE_WORDS[words.pop().lower()]
        name = " ".join(words).strip()
        if name:
            out.append({"query": name, "range": rng})
    if not out:
        raise ValueError("Was soll ich zeigen? Etwa „SAP“, „Siemens 1j“ oder „DAX, Nvidia 5t“")
    return out[:8]
