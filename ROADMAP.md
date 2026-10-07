# Roadmap

整體方向見 `docs/adr/`：以官方 server 為基礎、adapter 以 Rust 為主、追 crates.io nautilus-* 發行版。

## 進行中

- **P1 官方 server 驗證 spike**：回答 adapter 能否直接接官方 server 1.7.6 與其契約。設計：`docs/plans/2026-09-29-official-server-spike-design.md`。

## 排隊

- **P2 adapter v2 化**：純 Rust `DataClient` / `ExecutionClient` 接官方 server，追 crates.io 最新 nautilus-*；發佈時同步更新 community listing（nautechsystems/nautilus_trader#4324）的安裝說明。設計可先開始，實作等 P1 go/no-go。
- **P3 輔助服務**：只在 P1 判定 GO＋P3 時開，範圍即 P1 的缺口清單。

## 待決定

- 資料管線（`shioaji-data` CLI、universe 腳本、cron，現位於舊 gateway repo）的去向。
- v0.1.x（Python，nautilus_trader 1.228.0）的維護方式。
