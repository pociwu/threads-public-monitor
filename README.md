# Threads Public Monitor

以專用 Threads 帳號定期觀察最多 16 個任意公開帳號，保存個人檔案、統計、粉絲／追蹤中名單、每日名單差異、串文、回覆、轉發、引用轉發，以及去重後的照片與影片。介面固定使用繁體中文深色主題，僅透過 Tailscale 私網提供。

完整需求見 [產品規格](docs/product-spec.md)，架構選擇見 [ADR](docs/adr)。

## 正式環境

- Ubuntu 24.04 LTS ARM64
- Docker Engine
- Docker Compose v2
- 已登入並啟用的 Tailscale
- 至少 120 GB 可用磁碟空間（媒體上限預設 100 GB）

## 從 GitHub Release 安裝

```bash
git clone https://github.com/pociwu/threads-public-monitor.git
cd threads-public-monitor
git checkout "$(git tag --list 'v[0-9]*' --sort=-v:refname | head -n1)"
bash install.sh
```

安裝程式會偵測主機 Tailscale IPv4；若沒有偵測到，預設為 `100.120.200.116`。請在 `.env` 確認：

```dotenv
TAILSCALE_IP=100.120.200.116
WEB_PORT=8080
LOGIN_PORT=6080
```

服務只綁定這個 Tailscale IP，不監聽 `0.0.0.0`。

## Telegram 異動通知（選用）

先透過 Telegram 的 `@BotFather` 建立 Bot，並由接收通知的帳號或群組先傳訊息給該 Bot。接著在 `.env` 設定：

```dotenv
TELEGRAM_BOT_TOKEN=123456789:your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

重新啟動服務後，系統會通知增量階段首次擷取的新串文、新回覆、新引用與新轉發，以及完整掃描後的粉絲／追蹤中名單新增與退出。內容通知包含監看帳號、文字摘要及原文連結；同一內容重複擷取不重複通知，不同監看帳號轉發同一原文則各自通知。名單掃描耗盡自動重試並正式失敗時也會通知原因與目前進度；等待自動重試中的暫時失敗不通知。初始回補不會發送大量舊內容；第一次完整名單只建立比較基準，也不會發送異動。通知使用資料庫 Outbox 獨立重試，Telegram 暫時失敗不會影響 Threads 擷取結果。

`TELEGRAM_CHAT_ID` 也可填公開頻道的 `@channel_username`，或頻道的數字 Chat ID。Bot 必須先加入頻道並設為具備發文權限的管理員。

## 首次登入 Threads

```bash
bash scripts/login.sh
```

腳本會先停止背景 Worker，再按需啟動互動式 Chromium。從提示的 Tailscale 網址開啟 noVNC，親自登入專用 Threads 帳號；完成後回到終端按 Enter。密碼不會送入本應用程式或資料庫。

## 擷取節流與 429 冷卻

到期工作依「內容更新 → 關係名單 → 舊貼文更新 → 個人檔案／帳號驗證」輪替；沒有到期工作的類別直接跳過。同類別內仍依優先序及到期時間選取，輪替位置保存於資料庫，重啟後繼續。持續續抓名單不再阻擋貼文更新；每日上限、批次等待與全域冷卻仍優先適用。排程延誤後首次抓到的內容也可能較舊，通知代表首次觀察時間，不代表即時發文。

系統只使用單一背景 Worker，預設每個 Threads 批次之間隨機等待 3–8 分鐘、每日最多執行 200 個批次，名單每批預設保存 25 人，並限制在 25–50 人的安全範圍。舊版 `.env` 的較小設定會提升至 25，過大的設定會壓到 50；統計數達 200 人的長名單會自動使用 50 人批次。這可減少每輪從名單頂端重掃的次數，同時避免形成無上限的單次請求突發；全域等待與 429 冷卻仍維持不變。未變更的成員頭像會依 Meta 穩定媒體識別碼重用，不再因網址簽章改變而重複下載。

若 Threads 明確回傳 HTTP 429，或顯示「請稍後再試／Too many requests」限流頁，系統會把所有 Threads 擷取工作暫停並將冷卻狀態保存到 SQLite。第一次冷卻預設為 45–90 分鐘；24 小時內再次命中時會乘 4，逐步延長到 3–6 小時、12–24 小時，最高 24 小時，且會遵守伺服器較長的 `Retry-After`。重啟 Worker 或按「立即重試」都不會跳過仍有效的全域冷卻；限流不會耗用工作本身的功能性重試次數，也不會把尚未完成的名單掃描標成正式失敗。

Threads 顯示的統計數可能大於目前登入身分實際能遍歷的名單。可捲動名單需在末端穩定至少 8 秒；沒有捲軸且少於統計數時需穩定至少 20 秒才視為可存取末端。若完成遍歷顯示有人退出，系統會在下一個獨立工作從頭再遍歷一次，只有連續兩次得到相同退出集合才建立異動與通知。

可在 `.env` 調整：

```dotenv
RATE_LIMIT_INITIAL_MIN_DELAY_SECONDS=2700
RATE_LIMIT_INITIAL_MAX_DELAY_SECONDS=5400
RATE_LIMIT_BACKOFF_MULTIPLIER=4
RATE_LIMIT_MAX_DELAY_SECONDS=86400
RATE_LIMIT_STREAK_RESET_SECONDS=86400
```

## 更新與回復

```bash
bash update.sh
```

更新腳本只選擇最新正式版本標籤，更新前停止服務並以 SQLite `.backup` 建立一致性備份。建置、遷移或啟動失敗時會回復原 Git 版本與資料庫。

## 常用操作

```bash
docker compose ps
docker compose logs -f worker
docker compose restart web worker
bash scripts/login.sh
```

網站預設網址：`http://100.120.200.116:8080`

## 本機開發

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

另開終端執行：

```bash
python -m app.worker
```

## 資料目錄

- `data/threads-monitor.db`：SQLite WAL 資料庫
- `data/media/`：以 SHA-256 分層保存的媒體
- `browser-profile/`：專用 Threads 登入工作階段
- `backups/`：版本更新前的 SQLite 備份

上述目錄不會提交至 Git。系統不提供每日或異地備份。

## 授權

[MIT](LICENSE)
