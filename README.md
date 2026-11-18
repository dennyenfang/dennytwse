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

## ropihung 部署

正式服務使用 Gunicorn。Cloudflare Tunnel 的來源位址設為 `http://127.0.0.1:5001`，對外網域為 `denny888.usdollar.win`。持股與成本等功能共用同一個 SQLite 資料庫，應在公開 Tunnel 路由前設定 Cloudflare Access 的登入與允許名單。

在具備 user systemd、linger、Bubblewrap、Git、curl 及 Python 3.10+（含 venv）的 Linux 主機上，將此 repo 唯讀 deploy key 的私鑰存入 `~/.config/denny888/deploy_key`，將該目錄及私鑰的權限分別設為 `0700`、`0600`。部署程式使用嚴格主機驗證，請將已核對的 GitHub SSH 主機公鑰存入同目錄的 `known_hosts`。[GitHub 官方指紋](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)

從已審查的程式碼執行一次安裝：

```sh
bash deploy/ropihung/install.sh
systemctl --user status denny888.service denny888-deploy.timer
curl -fsS http://127.0.0.1:5001/healthz
```

可在 `~/.config/denny888/service.env` 指定 `SHEET_ID`、`SHEET_GID` 或 `DENNY888_PORT`。未設定 Google Sheet 時，仍可在 APP 管理持股。

計時器每兩分鐘檢查 `main`。新版本通過獨立資料庫測試後才會切換，實際服務的 `/healthz` 回傳狀態及完整 commit SHA。啟動失敗會回到前一個可用版本。資料庫存於 `~/.local/share/denny888/data/portfolio.db`，更版前的最近五份快照存於 `backups/`。套件安裝、測試與服務分別在 Bubblewrap 裡執行，GitHub 憑證位於沙箱外。若更新部署腳本或 systemd 單元，請審查後在主機重新執行安裝程式。

手動觸發檢查可執行 `systemctl --user start denny888-deploy.service`，查看最近紀錄可執行 `journalctl --user -u denny888-deploy.service -n 50`。
