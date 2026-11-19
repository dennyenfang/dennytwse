# 台股訊號觀測站

Denny777 台股版，保留 PWA、個股明細、K 線與 MA5/20/60/120、MACD(12,26,9)、SQLite 掃描結果和個人持股清單。首頁分類為上市、上櫃、ETF、觀察清單及我的持股。

## 本機啟動

Windows PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

開啟 <http://127.0.0.1:5003/>。預設資料庫為本資料夾的 `portfolio.db`，可用 `DENNYTWSE_DB_PATH` 指定其他位置，連接埠可用 `DENNYTWSE_PORT` 覆寫。

## 行情來源與快取

- 上市每日 OHLCV：TWSE OpenAPI 的 `STOCK_DAY_ALL`；批次歷史掃描使用 TWSE 盤後每日行情。
- 上櫃每日 OHLCV：TPEx OpenAPI 的 `tpex_mainboard_daily_close_quotes`；歷史掃描使用櫃買中心盤後每日行情。
- 個股詳細歷史資料由交易所逐月行情介面回補，標準化為開、高、低、收、成交股數後計算訊號。
- 行情和掃描結果寫入 SQLite。Google Sheet 以 `WATCHLIST_SHEET_ID`、`WATCHLIST_SHEET_GID` 設定，C 欄讀取台股代號作為「觀察清單」。

TWSE 與 TPEx 的即時 OpenAPI 提供最新交易日快照；每日排程需在台北時間收盤後抓取並存入 SQLite，才能形成可供指標計算的本機歷史快取。初次回補會比平日掃描花較久時間。

## 訊號定義

- MACD = EMA(12) − EMA(26)，訊號線 = MACD 的 EMA(9)，柱狀圖 = MACD − 訊號線。
- 金叉／死叉：MACD 黑線上穿／下穿訊號線；掃描列顯示最近 3 個交易日內日期。
- 負轉正／正轉負：MACD 黑線上穿／下穿零軸；掃描列顯示最近 3 個交易日內日期。
- 爆大量：最近 5 個交易日中最高量，若大於此前 60 個交易日平均量的 1.5 倍，顯示最高量日期。

個股與技術指標僅供觀察，不構成投資建議。
