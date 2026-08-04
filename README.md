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

## 首次登入 Threads

```bash
bash scripts/login.sh
```

腳本會先停止背景 Worker，再按需啟動互動式 Chromium。從提示的 Tailscale 網址開啟 noVNC，親自登入專用 Threads 帳號；完成後回到終端按 Enter。密碼不會送入本應用程式或資料庫。

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
