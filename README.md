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

正式服務使用 Gunicorn。Cloudflare Tunnel 連到 `http://127.0.0.1:5001`。任何人都可以使用行情掃描並查看同一份持股，包含股數及成本。[公開網站](https://denny888.usdollar.win/)

Roland 和 Denny 可從「管理持股」進入編輯頁。Cloudflare Access 以 Email 驗證碼驗證身分，登入有效期為 24 小時。Access 的應用程式路徑為 `/manage`，涵蓋其下的管理 API。Tunnel 應優先套用管理路徑的 Access 驗證規則，再套用公開網站路由。[管理持股](https://denny888.usdollar.win/manage/)

在具備 user systemd、linger、Bubblewrap、Git、curl 及 Python 3.10+（含 venv）的 Linux 主機上，將此 repo 唯讀 deploy key 的私鑰存入 `~/.config/denny888/deploy_key`，將該目錄及私鑰的權限分別設為 `0700`、`0600`。部署程式使用嚴格主機驗證，請將已核對的 GitHub SSH 主機公鑰存入同目錄的 `known_hosts`。[GitHub 官方指紋](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)

從已審查的程式碼執行一次安裝：

```sh
bash deploy/ropihung/install.sh
systemctl --user status denny888.service denny888-deploy.timer
curl -fsS http://127.0.0.1:5001/healthz
```

可在 `~/.config/denny888/service.env` 指定 `SHEET_ID`、`SHEET_GID` 或 `DENNY888_PORT`。未設定 Google Sheet 時，仍可在 APP 管理持股。

公開共用模式在同一檔案設定 `DENNY888_PUBLIC_SHARED=1`、`DENNY888_PUBLIC_ORIGIN=https://denny888.usdollar.win`、`DENNY888_ACCESS_TEAM_URL=https://rolandtalk.cloudflareaccess.com`、管理應用程式的 `DENNY888_ACCESS_AUD` 以及以逗號分隔的 `DENNY888_EDITOR_EMAILS`。後端會檢查管理請求的 Access 簽章、應用程式受眾和編輯者信箱。

計時器每兩分鐘檢查 `main`。新版本通過獨立資料庫測試後才會切換，實際服務的 `/healthz` 回傳狀態及完整 commit SHA。啟動失敗會回到前一個可用版本。資料庫存於 `~/.local/share/denny888/data/portfolio.db`，更版前的最近五份快照存於 `backups/`。套件安裝、測試與服務分別在 Bubblewrap 裡執行，GitHub 憑證位於沙箱外。若更新部署腳本或 systemd 單元，請審查後在主機重新執行安裝程式。

手動觸發檢查可執行 `systemctl --user start denny888-deploy.service`。在 ropihung 查看最近紀錄可執行 `journalctl _SYSTEMD_USER_UNIT=denny888-deploy.service -n 50 --no-pager`。

Google Sheet ID 和持股資料庫存放在 Pi 上。行情與訊號僅供資訊參考。

## Cloudflare Workers 部署

Cloudflare 版本使用 Python Workers + D1，行情由 Yahoo Finance chart API 提供；持股 API 與整個網站應由 Cloudflare Access 保護。D1 資料庫名稱為 `denny123`，資料庫 ID 已填入 `wrangler.jsonc`。Google Sheet ID 與分頁 GID 應以 Worker 私密變數 `SHEET_ID` / `SHEET_GID` 設定；勿將 Google Sheet ID 或 `.env` 提交到 Git。

`main` 分支已連結 Cloudflare Workers Builds。部署環境需使用 `uv run pywrangler deploy` 以打包 Python Worker，D1 綁定與 migration 設定見 `wrangler.jsonc` 與 `migrations/`。Python Worker 使用 Cloudflare 支援的 Worker runtime，與本機 Flask/SQLite 執行環境分開。
