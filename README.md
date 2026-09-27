# 주식·ETF 티커 목록 수집 (바이낸스 bStocks, 바이낸스 TradFi 선물, 하이퍼리퀴드 HIP-3)

## 결론

세 목록 모두 API 키 없이 공개 엔드포인트로 가져올 수 있습니다. `fetch_tradfi_tickers.py`는 세 목록을 한 번에 받아서 ETF와 개별 주식을 구분한 뒤 CSV로 저장합니다. 표준 라이브러리만 사용합니다.

```
python3 fetch_tradfi_tickers.py            # output/ 에 CSV 3개와 summary.json 저장
python3 fetch_tradfi_tickers.py --include-non-equity   # 바이낸스 원자재·FX·Pre-IPO 선물까지 포함
python3 fetch_tradfi_tickers.py --hl-categories stocks,indices   # 하이퍼리퀴드 지수 선물까지 포함
```

| 목록 | 식별 기준 | 엔드포인트 |
|---|---|---|
| 바이낸스 현물 bStocks | `tags`에 `"bStocks"` 포함 | `GET https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true` |
| 바이낸스 선물 (USDⓈ-M) | `contractType == "TRADIFI_PERPETUAL"`이고 `underlyingType`이 `EQUITY`, `HK_EQUITY`, `KR_EQUITY`, `CN_EQUITY` 중 하나 | `GET https://fapi.binance.com/fapi/v1/exchangeInfo` |
| 하이퍼리퀴드 (HIP-3) | `perpDexs`로 모든 dex를 나열하고, 각 dex의 `meta`와 `perpConciseAnnotations`의 `category == "stocks"`를 결합 | `POST https://api.hyperliquid.xyz/info` |

(작성 중)
