# 採用伺服器渲染 Web 與單一背景 Worker

介面使用 FastAPI、Jinja2、HTMX、SortableJS 與 Chart.js，資料層使用 SQLAlchemy 與 Alembic，不建立 React／Vue 單頁應用或 Node.js 建置鏈。Docker Compose 將 `web` 與唯一的 `worker` 分為兩個服務，兩者共享 SQLite 與媒體掛載目錄；這在保留拖曳、局部更新和趨勢圖能力的同時降低部署複雜度，但未來若要多 Worker 擴充，必須先替換 SQLite 與工作鎖定模型。
