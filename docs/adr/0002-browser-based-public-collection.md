---
status: superseded by ADR-0006
---

# 以未登入瀏覽器擷取公開 Threads 資料

系統使用 Python Playwright 驅動 Chromium，只讀取未登入訪客在 Threads 公開頁面正常可見或載入的資料，不保存或使用登入憑證。選擇瀏覽器擷取是為了支援任意公開帳號並避免依賴未公開內部 API；代價是頁面改版可能使解析器失效，且遇到驗證或限制頁時必須暫停並退避，而不是嘗試繞過。
