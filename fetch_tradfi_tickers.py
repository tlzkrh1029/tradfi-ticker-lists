#!/usr/bin/env python3
"""Fetch stock/ETF ticker lists from Binance spot (bStocks), Binance USD-M
futures (TradFi perpetuals) and Hyperliquid HIP-3 perp dexes.

Standard library only. Writes one CSV per venue plus summary.json to --out.

ETF vs single-stock classification (no venue publishes this flag):
  US listings   Nasdaq Trader symbol directory "ETF" column, cross-checked
                against Binance's own US equity catalog (type STOCK/ETF).
  Binance       the real ticker comes from /fapi/v1/constituents, because
  futures       Binance renames tickers that collide with crypto assets
                (BBXUSDT = BlackBerry "BB", STXXUSDT = Seagate "STX").
  Hyperliquid   the reference listing is parsed from perpAnnotation
                ("(Nasdaq: TSLA)", "(KRX: 000660)").
  Non-US        NON_US_UNDERLYINGS below, checked against Binance listing
                announcements; anything missing from it is "unknown".
Rows that cannot be resolved are written as "unknown" and listed under
summary.json "review".
"""

import argparse
import csv
import datetime as dt
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (tradfi-ticker-fetcher)"}

# Official hosts first. www.binance.com / data-api.binance.vision serve the
# same public payloads where api/fapi.binance.com answer HTTP 451.
BINANCE_SPOT_EXCHANGE_INFO = [
    "https://api.binance.com/api/v3/exchangeInfo",
    "https://data-api.binance.vision/api/v3/exchangeInfo",
]
BINANCE_FAPI_HOSTS = ["https://fapi.binance.com", "https://www.binance.com"]
BINANCE_ALL_ASSETS = "https://www.binance.com/bapi/asset/v2/public/asset/asset/get-all-asset"
BINANCE_EQUITY_CATALOG = "https://www.binance.com/bapi/equity/v1/public/equity/symbol/get-symbols-static"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
NASDAQ_DIR = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

BSTOCKS_TAG = "bStocks"
# Undocumented spot permission group that only bStocks pairs carry today.
# Used as a cross-check only; the asset tag is the source of truth.
BSTOCKS_PERMISSION_GROUP = "TRD_GRP_261"

UNDERLYING_MARKET = {"EQUITY": "US", "HK_EQUITY": "HK", "KR_EQUITY": "KR", "CN_EQUITY": "CN"}

# Binance TradFi perps on non-US listings, verified against the Binance
# listing announcements (2026-09). Binance publishes no machine-readable
# name or code for KR/CN contracts, so new ones show up as "unknown".
NON_US_UNDERLYINGS = {
    "BYD": ("HKEX:1211", "BYD Company Limited (H shares)", "stock"),
    "CSOPSAMSUNG2L": ("HKEX:7747", "CSOP Samsung Electronics Daily (2x) Leveraged Product", "etf"),
    "CSOPSKHYNIX2L": ("HKEX:7709", "CSOP SK Hynix Daily (2x) Leveraged Product", "etf"),
    "GIGADEV": ("HKEX:3986", "GigaDevice Semiconductor Inc. (H shares)", "stock"),
    "HK0625": ("HKEX:0625", "SHEIN Global Holdings Limited", "stock"),
    "HK0700": ("HKEX:0700", "Tencent Holdings Limited (HKD quanto)", "stock"),
    "HK0992": ("HKEX:0992", "Lenovo Group Limited", "stock"),
    "HK1810": ("HKEX:1810", "Xiaomi Corporation", "stock"),
    "KUAISHOU": ("HKEX:1024", "Kuaishou Technology", "stock"),
    "MEITUAN": ("HKEX:3690", "Meituan", "stock"),
    "MINIMAX": ("HKEX:0100", "MiniMax Group Inc.", "stock"),
    "POPMART": ("HKEX:9992", "Pop Mart International Group Limited", "stock"),
    "TENCENT": ("HKEX:0700", "Tencent Holdings Limited", "stock"),
    "ZHIPU": ("HKEX:2513", "Knowledge Atlas Technology (Zhipu AI)", "stock"),
    "ZHONGJI": ("HKEX:3308", "Zhongji Innolight Co., Ltd. (H shares)", "stock"),
    "HANMI": ("KRX:042700", "Hanmi Semiconductor Co., Ltd.", "stock"),
    "HYUNDAI": ("KRX:005380", "Hyundai Motor Company", "stock"),
    "KODEX200": ("KRX:069500", "Samsung KODEX 200 ETF", "etf"),
    "LGELECTRONICS": ("KRX:066570", "LG Electronics Inc.", "stock"),
    "NAVER": ("KRX:035420", "NAVER Corporation", "stock"),
    "SAMSUNG": ("KRX:005930", "Samsung Electronics Co., Ltd.", "stock"),
    "SAMSUNGEM": ("KRX:009150", "Samsung Electro-Mechanics Co., Ltd.", "stock"),
    "SKHYNIX": ("KRX:000660", "SK hynix Inc.", "stock"),
    "CXMT": ("SSE:688825", "CXMT Corporation (ChangXin Memory Technologies)", "stock"),
    "UNITREE": ("SSE:688836", "Unitree Robotics Co., Ltd.", "stock"),
}

RETRYABLE = (urllib.error.URLError, http.client.HTTPException, TimeoutError,
             ConnectionError, json.JSONDecodeError)


# ------------------------------------------------------------------------ HTTP


def _fetch(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def http_get(url, body=None, parse=json.loads, retries=4):
    for attempt in range(retries):
        try:
            return parse(_fetch(url, body))
        except urllib.error.HTTPError as e:
            # 403/451 = geo-restricted host, 4xx = bad request: let the
            # caller move on. 429 and 5xx are worth another try.
            if (e.code != 429 and e.code < 500) or attempt == retries - 1:
                raise
            time.sleep(15 if e.code == 429 else 2 ** attempt)
        except RETRYABLE:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def http_text(url):
    return http_get(url, parse=lambda b: b.decode("utf-8", "replace"))


def first_reachable(urls, valid):
    errors = []
    for url in urls:
        try:
            d = http_get(url)
            if valid(d):
                return url, d
            errors.append(f"{url}: unexpected payload")
        except Exception as e:  # any failure moves on to the next mirror
            errors.append(f"{url}: {e!r}")
    raise RuntimeError("all mirrors failed:\n  " + "\n  ".join(errors))


def ms_to_iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ms else ""


# ----------------------------------------------------------- US classification


def norm_symbol(sym):
    return re.sub(r"[.\-/ ]", "", (sym or "").upper())


class UsDirectory:
    """US listings from Nasdaq Trader, plus Binance's US equity catalog."""

    def __init__(self):
        self.nasdaq = {}
        for url in NASDAQ_DIR:
            lines = http_text(url).splitlines()
            header = lines[0].split("|")
            name_col, etf_col, test_col = (header.index(c) for c in ("Security Name", "ETF", "Test Issue"))
            for line in lines[1:]:
                f = line.split("|")
                if line.startswith("File Creation Time") or len(f) <= max(etf_col, test_col) or f[test_col] == "Y":
                    continue
                # column 0 is "Symbol" (nasdaqlisted) or "ACT Symbol" (otherlisted)
                self.nasdaq.setdefault(norm_symbol(f[0]), (f[0], f[name_col].strip(), f[etf_col] == "Y"))
        self.binance = {}
        self.binance_error = ""
        try:
            for r in http_get(BINANCE_EQUITY_CATALOG)["data"]:
                self.binance[norm_symbol(r.get("s"))] = (r.get("t") or "").lower()
        except Exception as e:  # optional second opinion
            self.binance_error = repr(e)

    def known(self, ticker):
        return norm_symbol(ticker) in self.nasdaq

    def classify(self, ticker):
        """Return (asset_class, listing symbol, security name, binance catalog type)."""
        key = norm_symbol(ticker)
        bn = self.binance.get(key, "")
        hit = self.nasdaq.get(key)
        if hit:
            sym, name, is_etf = hit
            return ("etf" if is_etf else "stock"), sym, name, bn
        if bn in ("stock", "etf"):
            return bn, ticker, "", bn
        return "unknown", "", "", bn


# ---------------------------------------------------------------- Binance spot


def binance_spot_bstocks(us):
    assets = {
        a["assetCode"]: a for a in http_get(BINANCE_ALL_ASSETS)["data"]
        if BSTOCKS_TAG in (a.get("tags") or []) and not a.get("test") and not a.get("delisted")
    }
    info_url, info = first_reachable(BINANCE_SPOT_EXCHANGE_INFO, lambda d: isinstance(d, dict) and "symbols" in d)

    rows, by_group = [], set()
    for s in info["symbols"]:
        if any(BSTOCKS_PERMISSION_GROUP in g for g in s.get("permissionSets") or []):
            by_group.add(s["symbol"])
        a = assets.get(s["baseAsset"])
        if a is None:
            continue
        ticker = a.get("uq") or s["baseAsset"][:-1]
        asset_class, listing, sec_name, bn_type = us.classify(ticker)
        rows.append({
            "symbol": s["symbol"],
            "base_asset": s["baseAsset"],
            "quote_asset": s["quoteAsset"],
            "underlying": ticker,
            "binance_name": re.sub(r"\s*\(bStocks\)\s*$", "", a.get("assetName") or ""),
            "asset_class": asset_class,
            "us_listing": listing,
            "us_security_name": sec_name,
            "binance_equity_type": bn_type,
            "status": s.get("status", ""),
            "dividend_multiplier": a.get("ml", ""),
        })
    tagged = {r["symbol"] for r in rows}
    check = {
        "exchange_info_source": info_url,
        "bstocks_assets": len(assets),
        "assets_without_spot_pair": sorted(set(assets) - {r["base_asset"] for r in rows}),
        f"only_in_{BSTOCKS_PERMISSION_GROUP}": sorted(by_group - tagged),
        f"missing_from_{BSTOCKS_PERMISSION_GROUP}": sorted(tagged - by_group),
    }
    return sorted(rows, key=lambda r: r["symbol"]), check


# ------------------------------------------------------------- Binance futures

# dxfeed "STX:USLF24//...", massive "FMV.STX//...", pyth_pro "STX//..." or "0700HK"
VENDOR_TICKER = [
    ("dxfeed", re.compile(r"^([A-Z][A-Z0-9./]*):US")),  # "BRK/B:USLF24"
    ("massive", re.compile(r"^FMV\.([A-Z][A-Z0-9.]*)")),
    ("pyth_pro", re.compile(r"^([A-Z][A-Z0-9.]*)//")),
]
PYTH_HK = re.compile(r"^(\d{4})HK")


def underlying_from_constituents(host, symbol):
    """Real listing ticker ("STX", "HKEX:0700"), "" when Binance gives none,
    or None when the request itself failed."""
    try:
        d = http_get(f"{host}/fapi/v1/constituents?symbol={symbol}")
    except urllib.error.HTTPError as e:
        return "" if e.code == 400 else None  # 400 = -1121 for KR/CN/pre-IPO contracts
    except Exception:
        return None
    votes = {}
    for c in d.get("constituents") or []:
        sym = c.get("symbol") or ""
        m = PYTH_HK.match(sym) if c.get("exchange") == "pyth_pro" else None
        if m:
            return f"HKEX:{m.group(1)}"
        for exch, rx in VENDOR_TICKER:
            m = rx.match(sym) if c.get("exchange") == exch else None
            if m:
                t = m.group(1).replace("/", ".")
                votes[t] = votes.get(t, 0) + 1
    return max(votes, key=votes.get) if votes else ""


def binance_futures_tradfi(us, include_non_equity=False):
    host, info = first_reachable([f"{h}/fapi/v1/exchangeInfo" for h in BINANCE_FAPI_HOSTS],
                                 lambda d: isinstance(d, dict) and "symbols" in d)
    host = host.rsplit("/fapi/", 1)[0]
    listed = [
        s for s in info["symbols"]
        if s.get("contractType") == "TRADIFI_PERPETUAL"
        and s.get("status") in ("TRADING", "PENDING_TRADING")  # delisted ones linger as SETTLING
        and (include_non_equity or (s.get("underlyingType") or "").endswith("EQUITY"))
    ]

    with ThreadPoolExecutor(max_workers=4) as ex:
        refs = list(ex.map(
            lambda s: underlying_from_constituents(host, s["symbol"]) if s["underlyingType"].endswith("EQUITY") else "",
            listed))

    rows = []
    for s, ref in zip(listed, refs):
        base, utype = s["baseAsset"], s.get("underlyingType", "")
        market = UNDERLYING_MARKET.get(utype, "")
        listing = name = bn_type = ""
        ticker_source = "constituents" if ref else ("baseAsset (constituents failed)" if ref is None else "baseAsset")
        ref = ref or ""
        if market == "US":
            asset_class, listing, name, bn_type = us.classify(ref or base)
        elif base in NON_US_UNDERLYINGS:
            listing, name, asset_class = NON_US_UNDERLYINGS[base]
            if ref and ref != listing:
                asset_class, name = "unknown", f"constituents say {ref}, table says {listing}"
        elif utype.endswith("EQUITY"):
            asset_class, listing = "unknown", ref
        else:
            asset_class = utype.lower()  # commodity / fx / premarket
        rows.append({
            "symbol": s["symbol"],
            "base_asset": base,
            "quote_asset": s["quoteAsset"],
            "underlying_type": utype,
            "underlying_sub_type": ";".join(s.get("underlyingSubType") or []),
            "market": market,
            "underlying": ref or base,
            "ticker_source": ticker_source,
            "asset_class": asset_class,
            "listing": listing,
            "security_name": name,
            "binance_equity_type": bn_type,
            "status": s.get("status", ""),
            "onboard_date": ms_to_iso(s.get("onboardDate"))[:10],
        })

    # The www.binance.com mirror serves a cached exchangeInfo (hours old), so
    # compare with the live 24h ticker to catch contracts listed since then.
    freshness = {"exchange_info_source": host, "exchange_info_server_time": ms_to_iso(info.get("serverTime"))}
    try:
        live = {t["symbol"] for t in http_get(f"{host}/fapi/v1/ticker/24hr")}
        known = {s["symbol"] for s in info["symbols"]}
        freshness["trading_but_not_in_exchange_info"] = sorted(live - known)
    except Exception as e:
        freshness["ticker_check_error"] = repr(e)
    return sorted(rows, key=lambda r: (r["underlying_type"], r["symbol"])), freshness


# ------------------------------------------------------------------ Hyperliquid


def hl(body):
    return http_get(HYPERLIQUID_INFO, body)


US_EXCHANGES = {"NASDAQ", "NYSE", "NYSE ARCA", "NYSE AMERICAN", "CBOE", "CBOE BZX"}
# "(Nasdaq: TSLA)", "(NYSE Arca: EWY)", "(KRX: 000660)", "(SSE STAR: 688825)"
REFERENCE_RE = re.compile(r"\(([A-Za-z][A-Za-z .]*?):\s*([A-Z0-9][A-Z0-9.\-]*)\)")
ETF_TEXT = re.compile(r"\betf\b|\bthe fund\b|\bbull \d+x\b|\bbear \d+x\b", re.I)
SHARE_TEXT = re.compile(r"\b(share|shares|stock|ads|a-share|h-share)\b", re.I)
CATEGORY_CLASS = {"indices": "index", "commodities": "commodity", "fx": "fx", "preipo": "preipo",
                  "crypto": "crypto", "rates": "rates"}


def reference_listing(description):
    """First "(EXCHANGE: TICKER)" in an annotation, else (None, None)."""
    m = REFERENCE_RE.search(description or "")
    return (m.group(1).strip(), m.group(2)) if m else (None, None)


def hyperliquid_hip3(us, include_delisted=False, categories=("stocks",)):
    dexes = [d for d in hl({"type": "perpDexs"}) if d]  # element 0 is the native dex (null)
    concise = {coin: a or {} for coin, a in hl({"type": "perpConciseAnnotations"})}
    errors = {}

    def dex_meta(d):
        try:
            return d, hl({"type": "meta", "dex": d["name"]})
        except Exception as e:  # one broken dex must not sink the rest
            errors[d["name"]] = repr(e)
            return d, {"universe": []}

    with ThreadPoolExecutor(max_workers=4) as ex:
        metas = list(ex.map(dex_meta, dexes))

    def category(coin):
        c = (concise.get(coin, {}).get("category") or "").lower()
        return {"stock": "stocks"}.get(c, c)

    candidates, review = [], []
    for d, meta in metas:
        active = [u for u in meta["universe"] if include_delisted or not u.get("isDelisted")]
        equity_dex = any(category(u["name"]) == "stocks" for u in active)
        for u in active:
            coin, cat = u["name"], category(u["name"])
            if cat in categories:
                candidates.append((d, meta, u, cat))
            elif not cat:
                # No annotation at all (io:EWY). Keep it only on a dex that
                # lists stocks and only if the ticker is a US listing.
                keep = "stocks" in categories and equity_dex and us.known(coin.split(":", 1)[1])
                review.append(coin)
                if keep:
                    candidates.append((d, meta, u, cat))

    def full_annotation(coin):
        try:
            return hl({"type": "perpAnnotation", "coin": coin}) or {}, ""
        except Exception as e:
            return {}, repr(e)

    with ThreadPoolExecutor(max_workers=2) as ex:
        details = list(ex.map(lambda c: full_annotation(c[2]["name"]), candidates))

    rows = []
    for (d, meta, u, cat), (ann, err) in zip(candidates, details):
        coin = u["name"]
        ticker = coin.split(":", 1)[1]
        short = concise.get(coin, {})
        description = ann.get("description") or ""
        keywords = ann.get("keywords") or short.get("keywords") or []
        display = ann.get("displayName") or short.get("displayName") or ""
        says_etf = bool(ETF_TEXT.search(description)) or "etf" in [k.lower() for k in keywords]
        exch, ref = reference_listing(description)
        us_ref = exch is None or exch.upper() in US_EXCHANGES
        listing = f"{exch}: {ref}" if exch else ""
        sec_name = bn_type = ""

        if cat and cat != "stocks":
            asset_class, source = CATEGORY_CLASS.get(cat, cat), "category"
        else:
            asset_class, source = "unknown", ""
            if us_ref:
                # The annotation's own ticker beats the coin name (xyz:PURRDAT
                # references Nasdaq: PURR; mkts:USBOND has displayName TLT).
                for key, src in ((ref, "annotation ticker"), (display, "displayName"), (ticker, "coin name")):
                    if key and us.known(key):
                        asset_class, us_sym, sec_name, bn_type = us.classify(key)
                        listing = listing or f"US: {us_sym}"
                        source = f"nasdaq ({src})"
                        break
            if asset_class == "unknown" and not err:
                # Non-US listing (KRX, HKEX, SSE, TSE) or no usable ticker:
                # the coin name may collide with an unrelated US symbol
                # (xyz:SKHX is KRX 000660, US "SKHX" is a 2x ETF), so read
                # the annotation text instead.
                if says_etf:
                    asset_class, source = "etf", "annotation"
                elif SHARE_TEXT.search(description):
                    asset_class, source = "stock", "annotation"
        if err:
            source = (source + "; " if source else "") + "perpAnnotation failed"
        rows.append({
            "coin": coin,
            "dex": d["name"],
            "dex_name": d.get("fullName", ""),
            "ticker": ticker,
            "display_name": display,
            "category": cat or "(none)",
            "reference_listing": listing,
            "asset_class": asset_class,
            "asset_source": source,
            "annotation_says_etf": says_etf,
            "security_name": sec_name,
            "binance_equity_type": bn_type,
            "max_leverage": u.get("maxLeverage", ""),
            "collateral_token": meta.get("collateralToken", ""),
            "delisted": bool(u.get("isDelisted")),
            "description": description.replace("\n", " "),
        })
    extra = {"uncategorized_coins": review, "dex_errors": errors,
             "annotation_errors": [c[2]["name"] for c, (_, e) in zip(candidates, details) if e]}
    return sorted(rows, key=lambda r: (r["dex"], r["ticker"])), extra


# ------------------------------------------------------------------------ main

FIELDS = {
    "binance_spot_bstocks": ["symbol", "base_asset", "quote_asset", "underlying", "binance_name", "asset_class",
                             "us_listing", "us_security_name", "binance_equity_type", "status",
                             "dividend_multiplier"],
    "binance_futures_tradfi": ["symbol", "base_asset", "quote_asset", "underlying_type", "underlying_sub_type",
                               "market", "underlying", "ticker_source", "asset_class", "listing", "security_name",
                               "binance_equity_type", "status", "onboard_date"],
    "hyperliquid_hip3_stocks": ["coin", "dex", "dex_name", "ticker", "display_name", "category",
                                "reference_listing", "asset_class", "asset_source", "annotation_says_etf",
                                "security_name", "binance_equity_type", "max_leverage", "collateral_token",
                                "delisted", "description"],
}


def write_csv(path, fields, rows):
    # Always rewrite, so an empty result never leaves an older file behind.
    # utf-8-sig lets Excel detect the encoding.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def count(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items()))


def class_conflicts(rows):
    """Rows where Nasdaq and Binance's equity catalog disagree on ETF vs stock."""
    return [r.get("symbol") or r.get("coin") for r in rows
            if r.get("binance_equity_type") in ("stock", "etf") and r["asset_class"] in ("stock", "etf")
            and r["binance_equity_type"] != r["asset_class"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))
    ap.add_argument("--include-non-equity", action="store_true",
                    help="also keep Binance TradFi commodity/FX/pre-IPO perps")
    ap.add_argument("--include-delisted", action="store_true", help="keep Hyperliquid delisted coins")
    ap.add_argument("--hl-categories", default="stocks",
                    help="comma-separated Hyperliquid categories to keep (e.g. stocks,indices)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    categories = tuple(c.strip().lower() for c in args.hl_categories.split(",") if c.strip())

    us = UsDirectory()
    summary = {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "binance_equity_catalog_error": us.binance_error or None}
    review = {}
    jobs = {
        "binance_spot_bstocks": lambda: binance_spot_bstocks(us),
        "binance_futures_tradfi": lambda: binance_futures_tradfi(us, args.include_non_equity),
        "hyperliquid_hip3_stocks": lambda: hyperliquid_hip3(us, args.include_delisted, categories),
    }
    failed = False
    for name, job in jobs.items():
        try:
            rows, extra = job()
        except Exception as e:
            failed = True
            summary[name] = {"error": repr(e)}
            print(f"{name}: {e!r}", file=sys.stderr)
            continue
        write_csv(os.path.join(args.out, f"{name}.csv"), FIELDS[name], rows)
        summary[name] = {"count": len(rows), "by_asset_class": count(rows, "asset_class"), **extra}
        if name == "binance_futures_tradfi":
            summary[name]["by_underlying_type"] = count(rows, "underlying_type")
        if name == "hyperliquid_hip3_stocks":
            summary[name]["by_dex"] = count(rows, "dex")
        review[name] = {
            "unknown": [r.get("symbol") or r.get("coin") for r in rows if r["asset_class"] == "unknown"],
            "nasdaq_vs_binance_catalog": class_conflicts(rows),
        }
        if name == "binance_futures_tradfi":
            # without constituents an aliased contract (BBX, STXX) is looked up under the wrong ticker
            review[name]["us_ticker_not_confirmed"] = [
                r["symbol"] for r in rows if r["market"] == "US" and r["ticker_source"] != "constituents"]
        if not rows:
            print(f"warning: {name} returned 0 rows", file=sys.stderr)
    summary["review"] = review

    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    print()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
