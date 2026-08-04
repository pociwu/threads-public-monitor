# 在 ARM64 Ubuntu 主機建置 Chromium 映像

正式部署目標是 Ubuntu 24.04 LTS ARM64；公開 GitHub Release 提供版本化原始碼，由 Ubuntu 主機在更新後本機建置 Docker 映像，並使用 ARM64 套件庫中的系統 Chromium，由 Playwright 指定可執行檔驅動。這避免依賴架構供應不一致的預製瀏覽器映像，代價是安裝與更新需要較長的主機建置時間，且 ARM64 Chromium 組合必須納入持續驗證。
