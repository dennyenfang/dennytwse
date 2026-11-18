# Denny 888 美股 MACD 掃描器

此 repo 收錄本機預覽 `http://127.0.0.1:5001/` 的 Flask/PWA 美股掃描器版本，介面包含持股、MACD 歷史圖、個股詳情及市場訊號掃描。

## Windows 本機啟動

需要 Python 3.10 或更新版本。在此資料夾開啟 PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

編輯 `.env` 填入自己的 Google Sheet ID 和 GID，然後執行：

```powershell
python app.py
```

瀏覽 <http://127.0.0.1:5001/>。持股資料會儲存在此資料夾的 `portfolio.db`。

## 功能與資料

- 美股持股清單、股數、每股平均成本及未實現損益。
- 120 日 MACD(12,26,9) 歷史圖與個股詳情。
- S&P 500、醫療保健、金融、電信營運商、消費品、能源區塊掃描。
- 爆大量（當日成交量至少是前 20 日均量 1.5 倍）、MACD 零軸翻轉、黃金／死亡交叉及組合篩選。
- 行情使用 yfinance / Yahoo Finance。

此 repo 僅包含程式碼範本；Google Sheet ID、持股資料庫等本機資料不會提交。公開部署前需加入登入保護與持久化儲存。僅供資訊參考，不構成投資建議。
