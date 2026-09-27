#!/usr/bin/env python3
"""Fetch stock/ETF ticker lists from Binance spot (bStocks), Binance USD-M
futures (TradFi perpetuals) and Hyperliquid HIP-3 perp dexes.

Standard library only. Writes one CSV per venue plus summary.json to --out.

ETF vs single-stock classification:
  1. Nasdaq Trader symbol directory (nasdaqlisted.txt / otherlisted.txt,
     column "ETF") for US-listed tickers.
  2. Hyperliquid perpAnnotation keywords/description ("etf") as a fallback.
  3. NON_US_ETF for non-US underlyings on Binance futures.
Anything that none of these resolves is written as "unknown".
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (tradfi-ticker-fetcher)"}

# Official hosts first; www.binance.com / data-api.binance.vision serve the
# same public payloads from regions where the api/fapi hosts return HTTP 451.
BINANCE_SPOT_EXCHANGE_INFO = [
    "https://api.binance.com/api/v3/exchangeInfo",
    "https://data-api.binance.vision/api/v3/exchangeInfo",
]
BINANCE_FUTURES_EXCHANGE_INFO = [
    "https://fapi.binance.com/fapi/v1/exchangeInfo",
    "https://www.binance.com/fapi/v1/exchangeInfo",
]
BINANCE_PRODUCTS = (
    "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products"
    "?includeEtf=true"
)
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
NASDAQ_DIR = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

BSTOCKS_TAG = "bStocks"
EQUITY_UNDERLYING_TYPES = {"EQUITY", "HK_EQUITY", "KR_EQUITY", "CN_EQUITY"}
UNDERLYING_MARKET = {"EQUITY": "US", "HK_EQUITY": "HK", "KR_EQUITY": "KR", "CN_EQUITY": "CN"}

# Non-US Binance futures underlyings that are funds rather than single stocks.
# The Nasdaq directory cannot see these, so they are listed by hand.
NON_US_ETF = {
    "KODEX200": "Samsung KODEX 200 ETF (KRX: 069500)",
    "CSOPSKHYNIX2L": "CSOP SK Hynix Daily (2x) Leveraged Product (HKEX)",
    "CSOPSAMSUNG2L": "CSOP Samsung Electronics Daily (2x) Leveraged Product (HKEX)",
}


def http_json(url, body=None, retries=3):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 451/403 = geo-restricted host; let the caller try the next mirror.
            if e.code in (403, 451) or attempt == retries - 1:
                raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)


def http_text(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def first_reachable(urls):
    errors = []
    for url in urls:
        try:
            return url, http_json(url)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            errors.append(f"{url}: {e}")
    raise RuntimeError("all mirrors failed:\n  " + "\n  ".join(errors))


def norm_symbol(sym):
    return re.sub(r"[.\-/ ]", "", sym.upper())


def load_nasdaq_directory():
    """Return {normalized symbol: (symbol, security name, is_etf)} for US listings."""
    out = {}
    for url in NASDAQ_DIR:
        lines = http_text(url).splitlines()
        header = lines[0].split("|")
        sym_col = 0  # "Symbol" in nasdaqlisted, "ACT Symbol" in otherlisted
        name_col = header.index("Security Name")
        etf_col = header.index("ETF")
        test_col = header.index("Test Issue")
        for line in lines[1:]:
            if line.startswith("File Creation Time"):
                continue
            f = line.split("|")
            if len(f) <= max(etf_col, test_col) or f[test_col] == "Y":
                continue
            out.setdefault(norm_symbol(f[sym_col]), (f[sym_col], f[name_col].strip(), f[etf_col] == "Y"))
    return out


def classify_us(ticker, nasdaq):
    hit = nasdaq.get(norm_symbol(ticker))
    if hit is None:
        return "unknown", "", ""
    sym, name, is_etf = hit
    return ("etf" if is_etf else "stock"), sym, name


# ---------------------------------------------------------------- Binance spot


def binance_spot_bstocks(nasdaq):
    products = http_json(BINANCE_PRODUCTS)["data"]
    tagged = {p["s"]: p for p in products if BSTOCKS_TAG in (p.get("tags") or [])}

    # Cross-check: every bStocks pair also sits in a spot permission group no
    # other pair uses. Report both directions so drift in either source shows.
    info_url, info = first_reachable(BINANCE_SPOT_EXCHANGE_INFO)
    spot = {s["symbol"]: s for s in info["symbols"]}
    groups = {}
    for sym in tagged:
        if sym in spot:
            for g in spot[sym].get("permissionSets") or []:
                for p in g:
                    groups[p] = groups.get(p, 0) + 1
    exclusive = sorted(
        g for g, n in groups.items()
        if n == len(tagged)
        and not any(g in grp for s, v in spot.items() if s not in tagged for grp in v.get("permissionSets") or [])
    )
    by_group = {
        s for s, v in spot.items()
        if any(g in grp for g in exclusive for grp in v.get("permissionSets") or [])
    }

    rows = []
    for sym, p in sorted(tagged.items()):
        base = p["b"]
        ticker = base[:-1] if base.endswith("B") else base
        asset_class, us_sym, us_name = classify_us(ticker, nasdaq)
        s = spot.get(sym, {})
        rows.append({
            "symbol": sym,
            "base_asset": base,
            "quote_asset": p["q"],
            "underlying": ticker,
            "binance_name": re.sub(r"\s*\(bStocks\)\s*$", "", p.get("an") or ""),
            "asset_class": asset_class,
            "us_listing": us_sym,
            "us_security_name": us_name,
            "status": s.get("status", p.get("st", "")),
            "margin": s.get("isMarginTradingAllowed", ""),
        })
    check = {
        "exchange_info_source": info_url,
        "tagged_count": len(tagged),
        "exclusive_permission_groups": exclusive,
        "only_in_tag": sorted(set(tagged) - by_group),
        "only_in_permission_group": sorted(by_group - set(tagged)),
    }
    return rows, check


# ------------------------------------------------------------- Binance futures


def binance_futures_tradfi(nasdaq, include_non_equity=False):
    url, info = first_reachable(BINANCE_FUTURES_EXCHANGE_INFO)
    rows = []
    for s in info["symbols"]:
        if s.get("contractType") != "TRADIFI_PERPETUAL":
            continue
        utype = s.get("underlyingType", "")
        if utype not in EQUITY_UNDERLYING_TYPES and not include_non_equity:
            continue
        base = s["baseAsset"]
        market = UNDERLYING_MARKET.get(utype, "")
        us_sym = us_name = ""
        if market == "US":
            asset_class, us_sym, us_name = classify_us(base, nasdaq)
        elif market:
            asset_class = "etf" if base in NON_US_ETF else "stock"
            us_name = NON_US_ETF.get(base, "")
        else:
            asset_class = utype.lower()  # commodity / fx / premarket
        rows.append({
            "symbol": s["symbol"],
            "base_asset": base,
            "quote_asset": s["quoteAsset"],
            "underlying_type": utype,
            "underlying_sub_type": ";".join(s.get("underlyingSubType") or []),
            "market": market,
            "asset_class": asset_class,
            "us_listing": us_sym,
            "us_security_name": us_name,
            "status": s.get("status", ""),
            "onboard_date": dt.datetime.fromtimestamp(s["onboardDate"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
            if s.get("onboardDate") else "",
        })
    meta = {"exchange_info_source": url, "server_time": info.get("serverTime")}
    return sorted(rows, key=lambda r: (r["underlying_type"], r["symbol"])), meta


# ------------------------------------------------------------------ Hyperliquid


def hl(body):
    return http_json(HYPERLIQUID_INFO, body)


US_EXCHANGES = {"NASDAQ", "NYSE", "NYSE ARCA", "NYSE AMERICAN", "CBOE BZX", "CBOE"}
# "(Nasdaq: TSLA)", "(NYSE Arca: EWY)", "(KRX: 000660)", "(SSE STAR: 688825)"
REFERENCE_RE = re.compile(r"\(([A-Za-z][A-Za-z .]*?):\s*([A-Z0-9][A-Z0-9.\-]*)\)")


def reference_listing(description):
    """First "(EXCHANGE: TICKER)" in a trade.xyz-style annotation, else (None, None)."""
    m = REFERENCE_RE.search(description or "")
    return (m.group(1).strip(), m.group(2)) if m else (None, None)


def hyperliquid_hip3(nasdaq, include_delisted=False, categories=("stocks",)):
    dexes = [d for d in hl({"type": "perpDexs"}) if d]  # index 0 is the native dex (null)
    annotations = {coin: a for coin, a in hl({"type": "perpConciseAnnotations"})}

    def dex_meta(d):
        return d, hl({"type": "meta", "dex": d["name"]})

    with ThreadPoolExecutor(max_workers=4) as ex:
        metas = list(ex.map(dex_meta, dexes))

    candidates = []
    for d, meta in metas:
        for u in meta["universe"]:
            if u.get("isDelisted") and not include_delisted:
                continue
            coin = u["name"]
            cat = (annotations.get(coin, {}).get("category") or "").lower()
            cat = {"stock": "stocks"}.get(cat, cat)
            ticker = coin.split(":", 1)[1]
            # Coins without an annotation are kept only if the Nasdaq directory
            # knows the ticker; asset_source marks them for review.
            if cat in categories or (not cat and norm_symbol(ticker) in nasdaq):
                candidates.append((d, meta, u, cat))

    def full_annotation(coin):
        try:
            return hl({"type": "perpAnnotation", "coin": coin}) or {}
        except (urllib.error.HTTPError, urllib.error.URLError):
            return {}

    with ThreadPoolExecutor(max_workers=4) as ex:
        details = list(ex.map(lambda c: full_annotation(c[2]["name"]), candidates))

    rows = []
    for (d, meta, u, cat), ann in zip(candidates, details):
        coin = u["name"]
        ticker = coin.split(":", 1)[1]
        description = ann.get("description") or ""
        text = " ".join([description, " ".join(ann.get("keywords") or [])]).lower()
        mentions_etf = bool(re.search(r"\b(etf|fund)\b", text))
        exch, ref = reference_listing(description)
        if exch and exch.upper() not in US_EXCHANGES:
            # Non-US reference (KRX, HKEX, SSE, TSE ...): the coin ticker may
            # collide with an unrelated US symbol, so skip the Nasdaq lookup.
            asset_class, us_sym, us_name, source = "unknown", "", "", ""
        else:
            # The annotation's own US ticker wins over the coin name
            # (xyz:PURRDAT references Nasdaq: PURR).
            asset_class, us_sym, us_name = classify_us(ref or ticker, nasdaq)
            source = "nasdaq" if ref else "nasdaq(coin name)"
        if asset_class == "unknown":
            source = "annotation"
            if mentions_etf:
                asset_class = "etf"
            elif re.search(r"\b(share|stock)\b", text):
                asset_class = "stock"
            else:
                asset_class = "other"
        rows.append({
            "coin": coin,
            "dex": d["name"],
            "dex_name": d.get("fullName", ""),
            "ticker": ticker,
            "category": cat or "(none)",
            "reference_listing": f"{exch}: {ref}" if exch else "",
            "asset_class": asset_class,
            "asset_source": source if cat else "uncategorized+" + source,
            "annotation_says_etf": mentions_etf,
            "us_listing": us_sym,
            "us_security_name": us_name,
            "max_leverage": u.get("maxLeverage", ""),
            "collateral_token": meta.get("collateralToken", ""),
            "delisted": bool(u.get("isDelisted")),
            "description": (ann.get("description") or "").replace("\n", " "),
        })
    return sorted(rows, key=lambda r: (r["dex"], r["ticker"]))


# ------------------------------------------------------------------------ main


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def count(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))
    ap.add_argument("--include-non-equity", action="store_true",
                    help="also keep Binance TradFi commodity/FX/pre-IPO perps")
    ap.add_argument("--include-delisted", action="store_true", help="keep Hyperliquid delisted coins")
    ap.add_argument("--hl-categories", default="stocks",
                    help="comma-separated Hyperliquid perpCategories to keep (e.g. stocks,indices)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    nasdaq = load_nasdaq_directory()
    spot, spot_check = binance_spot_bstocks(nasdaq)
    fut, fut_meta = binance_futures_tradfi(nasdaq, args.include_non_equity)
    hyper = hyperliquid_hip3(nasdaq, args.include_delisted,
                             tuple(c.strip().lower() for c in args.hl_categories.split(",") if c.strip()))

    write_csv(os.path.join(args.out, "binance_spot_bstocks.csv"), spot)
    write_csv(os.path.join(args.out, "binance_futures_tradfi.csv"), fut)
    write_csv(os.path.join(args.out, "hyperliquid_hip3_stocks.csv"), hyper)

    summary = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "binance_spot_bstocks": {"count": len(spot), "by_asset_class": count(spot, "asset_class"),
                                 "cross_check": spot_check},
        "binance_futures_tradfi": {"count": len(fut), "by_underlying_type": count(fut, "underlying_type"),
                                   "by_asset_class": count(fut, "asset_class"), **fut_meta},
        "hyperliquid_hip3": {"count": len(hyper), "by_dex": count(hyper, "dex"),
                             "by_asset_class": count(hyper, "asset_class"),
                             "by_asset_source": count(hyper, "asset_source")},
        "unknown": {
            "binance_spot_bstocks": [r["symbol"] for r in spot if r["asset_class"] == "unknown"],
            "binance_futures_tradfi": [r["symbol"] for r in fut if r["asset_class"] == "unknown"],
        },
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
