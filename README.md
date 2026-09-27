# 주식·ETF 티커 목록 수집 (바이낸스 bStocks, 바이낸스 TradFi 선물, 하이퍼리퀴드 HIP-3)

## 결론

세 목록 모두 API 키 없이 공개 엔드포인트로 가져올 수 있습니다. 다만 세 거래소 모두 ETF와 개별 주식을 구분하는 필드를 제공하지 않으므로, 기초 자산의 실제 상장 티커를 찾은 뒤 외부 종목 마스터와 결합해야 합니다. `fetch_tradfi_tickers.py`는 이 과정을 한 번에 수행해서 거래소별 CSV와 `summary.json`을 만듭니다. 표준 라이브러리만 사용합니다.

```
python3 fetch_tradfi_tickers.py                                  # output/ 에 저장 (약 1분)
python3 fetch_tradfi_tickers.py --include-non-equity             # 바이낸스 원자재·FX·Pre-IPO 선물까지 포함
python3 fetch_tradfi_tickers.py --hl-categories stocks,indices   # 하이퍼리퀴드 지수 선물까지 포함
python3 fetch_tradfi_tickers.py --include-delisted               # 하이퍼리퀴드 상장폐지(거래 중단) 종목까지 포함
```

| 목록 | 목록을 가져오는 기준 | 실제 티커를 얻는 곳 | 2026-09-27 결과 |
|---|---|---|---|
| 바이낸스 현물 bStocks | `get-all-asset`에서 `tags`에 `"bStocks"`가 있는 자산의 USDT 페어 | 같은 응답의 `uq` 필드 | 80개 (ETF 14, 주식 66) |
| 바이낸스 선물 | `exchangeInfo`에서 `contractType == "TRADIFI_PERPETUAL"`, `underlyingType`이 `…EQUITY`, 상태가 `TRADING` | `/fapi/v1/constituents`의 데이터 벤더 심볼 | 188개 (ETF 37, 주식 151) |
| 하이퍼리퀴드 | 모든 HIP-3 dex의 `meta`에서 상장폐지되지 않은 종목 중 카테고리가 `stocks`인 것 | `perpAnnotation` 설명문의 `(Nasdaq: TSLA)` 같은 표기 | 121개 (ETF 21, 주식 100) |

ETF 여부는 미국 상장 종목의 경우 Nasdaq Trader 심볼 디렉터리의 `ETF` 열로 판정하고, 바이낸스 자체 미국 주식 카탈로그의 `t`(STOCK/ETF) 필드로 교차 검증합니다. 2026-09-27 실행에서 두 출처가 어긋난 종목은 없었습니다.

## 1. 바이낸스 현물 bStocks

bStocks는 BTech Holdings가 발행하고 미국 주식 1주로 1:1 담보되는 토큰화 증권입니다. 2026-06-11에 처음 상장했으며, 현물 심볼은 `<원래 티커>B` + `USDT` 형식입니다(예: `NVDABUSDT`, `SPYBUSDT`).

```
GET https://www.binance.com/bapi/asset/v2/public/asset/asset/get-all-asset
  → data[] 중 "bStocks" in tags, test == 0, delisted == false
  → assetCode(NVDAB), assetName("NVIDIA (bStocks)"), uq(NVDA), ml(배당 반영 배수)
GET https://api.binance.com/api/v3/exchangeInfo   (451이면 https://data-api.binance.vision/api/v3/exchangeInfo)
  → baseAsset이 위 assetCode인 심볼
```

- 공식 문서에 bStocks를 가리키는 필드는 없습니다. `get-all-asset`과 `get-products`의 `bStocks` 태그는 웹사이트용 내부 API이므로 예고 없이 바뀔 수 있습니다.
- 현재 bStocks 페어 80개는 모두 `TRD_GRP_261` 권한 그룹을 가지고 있으며, 이 그룹을 가진 다른 심볼은 없습니다. 따라서 `exchangeInfo?permissions=["TRD_GRP_261"]`로도 같은 80개가 나옵니다. 하지만 이 그룹의 의미는 문서화되어 있지 않고, bStocks 상장 전에는 BTCUSDT에 붙어 있던 기록도 있습니다. 그래서 스크립트는 이 그룹을 교차 검증에만 쓰고, 두 집합이 다르면 `summary.json`에 기록합니다.
- 공식 API로는 `GET /sapi/v1/equity/market/tokenized-assets`(API 키 필요)가 `underlyingEquitySymbol`을 제공합니다. 다만 이 엔드포인트는 발행·상환 가능한 자산 목록이므로, 현물 상장 목록과 같다는 보장은 없습니다.
- `get-products`와 `get-all-asset`의 `etf` 필드는 예전 레버리지 토큰(BTCUP 등)을 뜻하므로, 주식 ETF 판정에는 쓸 수 없습니다.

## 2. 바이낸스 선물 (USDⓈ-M TradFi 무기한 선물)

```
GET https://fapi.binance.com/fapi/v1/exchangeInfo   (451이면 https://www.binance.com/fapi/v1/exchangeInfo)
  → contractType == "TRADIFI_PERPETUAL"
    and underlyingType in {EQUITY, HK_EQUITY, KR_EQUITY, CN_EQUITY}
    and status == "TRADING"
GET https://fapi.binance.com/fapi/v1/constituents?symbol=STXXUSDT
  → dxfeed "STX:USLF24//…", massive "FMV.STX//…", pyth_pro "STX//…" → 실제 티커 STX
```

- `underlyingType`은 증권 종류가 아니라 시장을 나타냅니다. `EQUITY`는 미국 시장이며, 여기에 SPY, QQQ, SOXL 같은 ETF 34개도 함께 들어 있습니다. 그 밖의 값으로는 `COMMODITY`(금·은·원유 등 8개), `FX`(USDBRL), `PREMARKET`(OpenAI, Anthropic 등 Pre-IPO 4개)가 있습니다. 새 시장이 추가될 수 있으므로 스크립트는 이름이 `EQUITY`로 끝나는 값을 모두 받습니다.
- **바이낸스 심볼이 실제 티커와 다를 수 있습니다.** 주식 티커가 기존 코인 티커와 겹치면 별칭을 씁니다. `BBXUSDT`는 BlackBerry(NYSE: BB), `STXXUSDT`는 Seagate(Nasdaq: STX), `QNTXUSDT`는 Quantinuum(Nasdaq: QNT)입니다. 특히 `STXX`는 Nasdaq에 실제로 존재하는 2배 레버리지 ETF 티커이기 때문에, `baseAsset`으로 조회하면 Seagate가 ETF로 잘못 분류됩니다. `BRKBUSDT`의 실제 티커는 `BRK.B`입니다. 그래서 스크립트는 `constituents`에서 실제 티커를 읽습니다.
- 한국(KR)과 중국(CN) 계약은 `constituents`가 `-1121 Invalid symbol`을 반환하므로 코드를 얻을 수 없습니다. 비미국 종목 25개는 바이낸스 상장 공지로 확인한 표(`NON_US_UNDERLYINGS`)로 분류하고, 표에 없는 새 종목은 `unknown`으로 남깁니다.
- 바이낸스 상장 공지에도 "BBXUSDT Perpetual Contract tracks the price of BlackBerry Limited Common Stock (NYSE: BB)" 같은 문구가 있습니다. 공지 목록은 `https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=48`, 본문은 `…/cms/article/detail/query?articleCode=<code>`로 받을 수 있습니다.
- `www.binance.com` 미러의 `exchangeInfo`는 서버에서 캐시한 응답이라 몇 시간(관측치 약 14시간) 늦습니다. 스크립트는 실시간인 `/fapi/v1/ticker/24hr`과 비교해서, 아직 `exchangeInfo`에 없는 새 상장 심볼을 `trading_but_not_in_exchange_info`에 기록합니다.
- 같은 기초 자산을 추적하는 계약이 둘인 경우가 있습니다. `SPCXUSDT`와 `SPCXUSD1`(USD1 마진), `HK0700USDT`(HKD 퀀토)와 `TENCENTUSDT`가 그렇습니다. 고유 종목 수를 세려면 `listing` 열로 중복을 제거하면 됩니다.
- 상장폐지된 계약은 `SETTLING` 상태로 `exchangeInfo`에 남으므로 상태 필터가 필요합니다. TradFi 선물을 거래하려면 계정에서 TradFi-Perps 약관에 동의해야 하지만(`POST /fapi/v1/stock/contract`), 시세와 목록 조회에는 필요하지 않습니다. COIN-M(dapi)에는 TradFi 계약이 없습니다.

## 3. 하이퍼리퀴드 (HIP-3 빌더 배포 무기한 선물)

기본 dex에는 주식이 없고, 주식 선물은 모두 HIP-3 dex에 있습니다. 모든 요청은 `POST https://api.hyperliquid.xyz/info`입니다.

```
{"type":"perpDexs"}                         → [null, {name:"xyz",…}, {name:"flx",…}, …]
{"type":"meta","dex":"xyz"}                 → universe[] (name "xyz:TSLA", isDelisted …)
{"type":"perpConciseAnnotations"}           → [["xyz:TSLA",{"category":"stocks","displayName":…,"keywords":[…]}], …]
{"type":"perpAnnotation","coin":"xyz:EWY"}  → {"category":"stocks","description":"EWY references 1 share of the iShares
                                                MSCI South Korea ETF (NYSE Arca: EWY), …","keywords":["korea","etf"]}
```

- `{"type":"allPerpMetas"}` 한 번으로 모든 dex의 universe를 받을 수도 있습니다. `{"type":"perpCategories"}`는 `[코인, 카테고리]` 쌍만 돌려줍니다.
- 카테고리는 배포자가 자유롭게 입력하는 문자열입니다. 그래서 `stocks`와 `stock`, `fx`와 `FX`가 섞여 있습니다. ETF도 `stocks`에 들어 있으며, 별도의 ETF 카테고리는 없습니다.
- `perpCategories`만 쓰면 안 됩니다. 상장폐지된 코인의 카테고리도 남아 있기 때문입니다. 2026-09-27 기준으로 `flx`, `vntl`, `hyna`, `km`, `abcd`, `cash`는 전 종목이 `isDelisted`이고, 주식 선물이 거래되는 dex는 `xyz`(trade.xyz, 93개), `para`(Paragon, 21개), `io`(EntropyIO, 6개), `mkts`(Markets by Kinetiq, 1개)입니다.
- 코인 이름은 실제 티커가 아닐 수 있습니다. `xyz:SKHX`는 KRX의 SK hynix 원주(000660)를 추적하지만, 미국의 `SKHX`는 2배 레버리지 ETF입니다. `xyz:PURRDAT`은 Nasdaq `PURR`이고, `mkts:USBOND`는 displayName이 `TLT`입니다. 그래서 스크립트는 설명문에 적힌 상장 티커를 우선 쓰고, 그다음 displayName, 마지막으로 코인 이름을 씁니다. 설명문이 KRX·HKEX·SSE·TSE 같은 비미국 거래소를 가리키면 미국 디렉터리 조회를 건너뜁니다.
- 카테고리가 없는 활성 코인이 3개 있습니다(`io:EWY`, `para:2Y`, `para:30Y`). 스크립트는 주식을 상장한 dex에 있고 미국 상장 티커와 이름이 같은 코인만 포함하며(`io:EWY`), 나머지는 `summary.json`의 `uncategorized_coins`에 기록합니다.
- `isDelisted`는 영구 폐지만 뜻하지 않을 수 있습니다. HIP-3 배포자는 `haltTrading`으로 거래를 멈췄다가 다시 열 수 있습니다. 예를 들어 trade.xyz는 2026-09-28 KIOXIA 주식 분할 때 거래를 잠시 중단한다고 공지했습니다. 그러므로 목록은 저장해 두고 쓰기보다 필요할 때마다 다시 받는 편이 안전합니다.
- `para:UNITREE`는 카테고리가 `stocks`이지만, 설명문에 "ahead of its public listing"이라고 적혀 있어 상장 전 종목에 가깝습니다. `xyz:UNITREE`는 상하이 STAR 마켓 상장 주식(688836)을 추적합니다.
- 같은 종목이 여러 dex에 따로 상장되어 있습니다(예: `xyz:EWY`와 `io:EWY`, `xyz:AVGO`와 `para:AVGO`). 담보 토큰과 호가창도 dex마다 다릅니다.
- 조회 한도는 IP당 분당 가중치 1200이고, `perpAnnotation`은 호출당 20입니다. 스크립트는 HTTP 429를 받으면 기다렸다가 다시 시도합니다.

## ETF 판정 방법

| 대상 | 1차 근거 | 교차 검증 |
|---|---|---|
| 미국 상장 종목 | Nasdaq Trader `nasdaqlisted.txt`와 `otherlisted.txt`의 `ETF` 열 | 바이낸스 `bapi/equity/v1/public/equity/symbol/get-symbols-static`의 `t` |
| 하이퍼리퀴드의 비미국 종목 | `perpAnnotation` 설명문과 키워드(ETF, "the fund", "Bull 3X" 등) | 없음 |
| 바이낸스 선물의 비미국 종목 | 상장 공지로 확인한 `NON_US_UNDERLYINGS` 표 | HK 종목은 `constituents`의 HKEX 코드와 대조 |

- 레버리지·인버스 ETF(TQQQ, SOXL, TSLL 등), 채권 ETF(TLT, TMF), 변동성 ETP(UVXY)는 모두 `etf`로 분류합니다.
- 바이낸스의 `CSOPSKHYNIX2L`(HKEX 7709)과 `CSOPSAMSUNG2L`(HKEX 7747)은 홍콩의 레버리지 상품(Leveraged Product)입니다. 형식상 ETF는 아니지만 펀드 구조이므로 `etf`로 분류했습니다.
- 우선주(STRC), ADR(TSM, BABA), 폐쇄형 투자회사(xyz:BOT)는 Nasdaq 디렉터리 기준에 따라 `stock`입니다.

## 출력 파일

`output/`에 저장하며, Excel에서 한글이 깨지지 않도록 UTF-8 BOM을 붙입니다.

| 파일 | 주요 열 |
|---|---|
| `binance_spot_bstocks.csv` | `symbol`, `underlying`, `binance_name`, `asset_class`, `us_security_name`, `binance_equity_type`, `status` |
| `binance_futures_tradfi.csv` | `symbol`, `underlying_type`, `market`, `underlying`, `ticker_source`, `asset_class`, `listing`, `security_name`, `onboard_date` |
| `hyperliquid_hip3_stocks.csv` | `coin`, `dex`, `category`, `reference_listing`, `asset_class`, `asset_source`, `annotation_says_etf`, `security_name`, `description` |
| `summary.json` | 목록별 개수, 교차 검증 결과, 데이터 신선도, `review`(분류하지 못한 종목, 두 출처가 어긋난 종목) |

`asset_class`는 `etf`, `stock`, `unknown` 중 하나입니다. `--include-non-equity`나 `--hl-categories`로 범위를 넓히면 `commodity`, `fx`, `premarket`, `index` 같은 값도 나옵니다. `review` 목록이 비어 있지 않다면 해당 종목을 직접 확인해야 합니다.

## 한계

- 바이낸스의 `bapi` 엔드포인트(`get-all-asset`, `get-symbols-static`, 공지 API)는 웹사이트 내부용이라 공식 지원 대상이 아닙니다.
- `api.binance.com`과 `fapi.binance.com`은 일부 지역(미국 등)에서 HTTP 451을 반환합니다. 이때 스크립트는 `data-api.binance.vision`과 `www.binance.com` 미러로 넘어갑니다. 이 결과물도 미러로 받았으며, 선물 `exchangeInfo`는 2026-09-26 20:55 UTC 시점의 캐시였습니다. 실시간 티커와 비교했을 때 누락된 심볼은 없었습니다.
- 한국·중국 선물과 Pre-IPO 계약은 실제 코드를 API로 확인할 수 없으므로, 새 종목이 생기면 `NON_US_UNDERLYINGS` 표를 갱신해야 합니다.
- 결과 CSV는 2026-09-27 스냅샷입니다. 세 거래소 모두 매주 종목을 추가하므로, 최신 목록이 필요하면 스크립트를 다시 실행해야 합니다.
