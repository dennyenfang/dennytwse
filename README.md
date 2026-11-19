# Denny888

此 repo 收錄 Flask/PWA 美股掃描器，介面包含個人持股、MACD 歷史圖、個股詳情及市場訊號掃描。

## Windows 本機啟動

需要 Python 3.10 或更新版本。在此資料夾開啟 PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

編輯 `.env` 設定所需資料來源，然後執行：

```powershell
python -m flask --app app run --host 127.0.0.1 --port 5002
```

瀏覽 <http://127.0.0.1:5002/>。持股及掃描資料會儲存在此資料夾的 `portfolio.db`。如需 Sheet 匯入功能，再填入 `SHEET_ID` 和 `SHEET_GID`。

## 我的持股

任何訪客都可在首頁輸入股票代號新增持股，並可刪除清單中的股票。代號清單儲存在 SQLite，不要求登入，也不記錄股數、成本或損益。瀏覽器保留一個匿名識別碼，用來取回自己的資料；不同瀏覽器或裝置不會自動同步。持股指標與個股 MACD 歷史圖透過行情 API 顯示，訊號紀錄列出金叉、爆量＋金叉、爆量＋負轉正。

## 功能與資料

- 儲存在 SQLite 並依匿名瀏覽器識別碼區分的美股持股清單；不記錄股數、成本或損益。
- 120 日 MACD(12,26,9) 歷史圖與個股詳情。
- 美股收盤掃描涵蓋 S&P 500 全部成分股、依市值排序的醫療保健前 50、金融前 10、電信營運商前 10、消費品前 50、能源前 20，以及 [觀察清單與 ETF清單](https://docs.google.com/spreadsheets/d/1miH6Rv7AS0xI8bxM-9X-kHTErM3SuPmx-55xh4-nj28/edit?usp=sharing)。兩個清單分別讀取「美股觀察名單」與「美股ETF名單」欄下的代號；各分類保留成員資格，同一代號每天合併掃描一次。消費品合計防禦性與循環性消費類股。
- 爆大量（取最近 5 個交易日中成交量最大的日期；該日成交量須大於此前 60 個交易日平均量的 1.5 倍，基準期不包含最近 5 日）、MACD 零軸翻轉、黃金／死亡交叉及組合篩選。掃描表的四種 MACD 訊號只顯示最近 3 個交易日內最新一次訊號的日期，沒有訊號時留白；組合篩選要求爆量日與 MACD 訊號日相同。
- 行情使用 yfinance / Yahoo Finance。

「美股訊號掃描」讀取 SQLite 最近發布的交易日結果。每次掃描保存當日名單、逐股結果、行情缺漏及掃描時間。網頁的分類、訊號篩選與欄位排序直接使用這份結果。若上游資料暫時無法發布，網頁繼續顯示上一次成功的日期與結果。

本機開發可執行 `python scheduled_market_scan.py`，產生最近已到期的交易日結果。觀察清單及 ETF清單分別讀取同一 Google Sheet 中「美股觀察名單」和「美股ETF名單」欄，共用持股由管理頁維護。

## GitHub 市場行情快照

`data/market_snapshot.sqlite` 是從本機 `portfolio.db` 匯出的市場資料快照，包含掃描結果、分類名單、掃描紀錄及日線，不包含 `holdings`、`user_holdings` 或 `alerts` 個人資料表。更新快照時執行 `python scripts/export_market_snapshot.py`，再提交此檔案。這是程式庫中的靜態快照；正式 Raspberry Pi 服務仍使用 `~/.local/share/denny888/data/portfolio.db`，兩者不會因推送程式碼而自動同步。

## ropihung 部署

正式服務使用 Gunicorn。Cloudflare Tunnel 連到 `http://127.0.0.1:5001`。市場掃描可公開瀏覽；個人持股清單儲存在訪客的瀏覽器。

點首頁的「管理持股」會開啟彈出視窗。任何人都能免登入新增、編輯和刪除共用持股，關閉視窗後可繼續查看原先的掃描篩選。[直接開啟管理持股](https://denny888.usdollar.win/manage/)

在具備 user systemd、linger、Bubblewrap、Git、curl 及 Python 3.10+（含 venv）的 Linux 主機上，將此 repo 唯讀 deploy key 的私鑰存入 `~/.config/denny888/deploy_key`，將該目錄及私鑰的權限分別設為 `0700`、`0600`。部署程式使用嚴格主機驗證，請將已核對的 GitHub SSH 主機公鑰存入同目錄的 `known_hosts`。[GitHub 官方指紋](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)

從已審查的程式碼執行一次安裝：

```sh
bash deploy/ropihung/install.sh
systemctl --user status denny888.service denny888-deploy.timer denny888-market-scan.timer
curl -fsS http://127.0.0.1:5001/healthz
```

可在 `~/.config/denny888/service.env` 指定 `SHEET_ID`、`SHEET_GID` 或 `DENNY888_PORT`。未設定 Google Sheet 時，仍可在 APP 管理持股。

正式站在同一檔案設定 `DENNY888_PUBLIC_SHARED=1`、`DENNY888_PUBLIC_EDITING=1` 與 `DENNY888_PUBLIC_ORIGIN=https://denny888.usdollar.win`。管理區的寫入請求須來自本站。若要恢復指定編輯者模式，請停用 `DENNY888_PUBLIC_EDITING`，為 `/manage` 設定 Cloudflare Access，並設定 `DENNY888_ACCESS_TEAM_URL`、`DENNY888_ACCESS_AUD` 及 `DENNY888_EDITOR_EMAILS`。

計時器每兩分鐘檢查 `main`。新版本通過獨立資料庫測試後才會切換，實際服務的 `/healthz` 回傳狀態及完整 commit SHA。啟動失敗會回到前一個可用版本。資料庫存於 `~/.local/share/denny888/data/portfolio.db`，更版前的最近五份快照存於 `backups/`。套件安裝、測試與服務分別在 Bubblewrap 裡執行，GitHub 憑證位於沙箱外。若更新部署腳本或 systemd 單元，請審查後在主機重新執行安裝程式。

手動觸發檢查可執行 `systemctl --user start denny888-deploy.service`。在 ropihung 查看最近紀錄可執行 `journalctl _SYSTEMD_USER_UNIT=denny888-deploy.service -n 50 --no-pager`。

`denny888-market-scan.timer` 依紐約證交所日曆，交易日收盤後 20 分鐘進行初掃，60 分鐘進行收盤複核；一般為美東 16:20、17:00，13:00 提早收盤時為 13:20、14:00。複核會重新抓取名單行情；若 Yahoo 回傳的歷史資料較短，只有在重疊日期及收盤價吻合時才以快取的舊資料補齊歷史區段。主機恢復後會檢查最近到期的階段。手動檢查可用 `systemctl --user start denny888-market-scan.service`，實際發布結果可查 `/api/market-scan/latest`。選擇特定歷史日期與分類的例子為 `/api/market-scan?as_of=2026-11-18&group=sheet`。

持股匯入表的設定和正式 SQLite 資料庫存放在 Pi 上。行情與訊號僅供資訊參考。

## Cloudflare Workers 部署

Cloudflare 版本使用 Python Workers + D1，行情由 Yahoo Finance chart API 提供；持股 API 與整個網站應由 Cloudflare Access 保護。D1 資料庫名稱為 `denny123`，資料庫 ID 已填入 `wrangler.jsonc`。Google Sheet ID 與分頁 GID 應以 Worker 私密變數 `SHEET_ID` / `SHEET_GID` 設定；勿將 Google Sheet ID 或 `.env` 提交到 Git。

`main` 分支已連結 Cloudflare Workers Builds。部署環境需使用 `uv run pywrangler deploy` 以打包 Python Worker，D1 綁定與 migration 設定見 `wrangler.jsonc` 與 `migrations/`。Python Worker 使用 Cloudflare 支援的 Worker runtime，與本機 Flask/SQLite 執行環境分開。
