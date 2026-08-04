# 僅從版本化 GitHub Releases 更新

公開 GitHub 儲存庫允許 Ubuntu 主機匿名安裝，但正式部署不追蹤 `main`；`update.sh` 只升級至最新正式版本標籤，並在更新前備份 SQLite、失敗時回復原版本。這犧牲取得最新提交的速度，以換取可辨識、可重現且可回復的部署版本。
