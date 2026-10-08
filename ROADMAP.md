# Roadmap

整體方向見 `docs/adr/`：以官方 server 為基礎、adapter 以 Rust 為主、追 crates.io nautilus-* 發行版。

## 進行中

- **P2 adapter v2 化**：純 Rust `DataClient` / `ExecutionClient` 接官方 server，追 crates.io 最新 nautilus-*；發佈時同步更新 community listing（nautechsystems/nautilus_trader#4324）的安裝說明。輸入是 P1 契約文件 §1.1、§3、§5；第一個驗證項目是正式環境能否讓程式自動下單。

## 排隊

- **P3 輔助服務**：P1 判定 GO，不開；只有多個 node 共用一台官方 server、需要訂閱引用計數與回報分送時才重新評估。

## 完成

- **P1 官方 server 驗證 spike**：GO，P2 直接接官方 server 1.7.6。契約與量測見 `docs/official-server-contract.md`，委託標記見 ADR-0005。

## 待決定

- 資料管線（`shioaji-data` CLI、universe 腳本、cron，現位於舊 gateway repo）的去向。
- v0.1.x（Python，nautilus_trader 1.228.0）的維護方式。
