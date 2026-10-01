# 官方 server 1.7.6 契約與量測紀錄

P1 spike 的產出文件（設計：`docs/plans/2026-09-29-official-server-spike-design.md`）。規格原文：`test_data/official/1.7.6/openapi.json`（OpenAPI 3.1.0，100 條路徑、209 個 schema）。

## 1. 端點對照

| 邏輯操作 | method | path | 請求 | 回應 | 備註 |
|---|---|---|---|---|---|
| `mode` | GET | `/api/v1/info` | — | `ApiInfoResponse` | `/simulation` 為布林；連鎖檢查用 |
| `accounts` | GET | `/api/v1/auth/accounts` | — | `Account[]` | 含 `account_type`（S／F／H）與 `signed` |
| `contracts_stock` | GET | `/api/v1/data/contracts/{code}/info` | — | `ContractInfoRecord` | 單檔查詢；`/api/v1/data/contracts` 可列全部 |
| `contracts_futures` | GET | `/api/v1/data/contracts/futures` | query `root`、`underlying_code`、`delivery_month` | `ContractInfoRecord[]` | 回應直接含 `limit_up`、`limit_down`、`reference` |
| `contracts_options` | GET | `/api/v1/data/contracts/options` | query `root` 等 | `ContractInfoRecord[]` | |
| `snapshots` | POST | `/api/v1/data/snapshots` | `SnapshotRequest` | `Snapshot[]` | JSON 本體，不是 query |
| `ticks` | POST | `/api/v1/data/ticks` | `TicksRequest` | `Ticks` | JSON 本體 |
| `kbars` | POST | `/api/v1/data/kbars` | `KBarsRequest` | `KBars` | JSON 本體 |
| `stream_connect` | GET | `/api/v1/stream/data` 與 `/api/v1/stream/data/<類型>` | query `region`、`security_type`（僅綜合串流） | SSE | 每種資料一條端點；見 §2 |
| `stream_subscribe` | POST | `/api/v1/stream/subscribe` | `BaseSubscriptionRequest` | `SubscriptionResponse` | 無 session／connection 識別，訂閱為 server 全域 |
| `stream_unsubscribe` | POST | `/api/v1/stream/unsubscribe` | 同上 | 同上 | |
| `subscribe_trade` | POST | `/api/v1/auth/subscribe_trade` | `AccountRequest` | `SubscribeTradeOut` | 啟用該帳戶的委託與成交回報 |
| `place_stock` | POST | `/api/v1/order/place_order` | `PlaceOrderRequest`（`contract` + `stock_order`） | `Trade` | 股票與期貨共用路徑 |
| `place_futures` | POST | `/api/v1/order/place_order` | `PlaceOrderRequest`（`contract` + `futures_order`） | `Trade` | |
| `update_price` | POST | `/api/v1/order/update_price` | `trade_id`、`price` | `Trade` | |
| `update_qty` | POST | `/api/v1/order/update_qty` | `trade_id`、`quantity` | `Trade` | |
| `cancel` | POST | `/api/v1/order/cancel_order` | `trade_id` | `Trade` | |
| `trades` | POST | `/api/v1/order/trades` | `AccountRequest` + `refresh` | `Trade[]` | `refresh` 省略時為 `true`（先向上游更新狀態） |
| `positions` | POST | `/api/v1/portfolio/position_unit` | `AccountRequest` + `unit` | `Position[]` | |
| `balance` | POST | `/api/v1/portfolio/account_balance` | `AccountRequest` | `AccountBalance` | |
| `margin` | POST | `/api/v1/portfolio/margin` | `AccountRequest` | `Margin` | |
| `usage` | GET | `/api/v1/auth/usage` | — | `UsageOut` | `connections`、`bytes`、`limit_bytes`、`remaining_bytes` |
| `ca_status` | GET | `/api/v1/auth/ca_expiretime` | query `person_id`（必填） | `CaExpireResponse` | 請求 URL 與回應都含 `person_id`，需去識別化 |
| `harness_info` | GET | `/api/v1/agent/harness` | — | `AgentHarnessInfo` | `info` 與 `health` 也帶同樣欄位 |

規格中另有而 adapter 可能用到的：`/api/v1/order/order_deal_records`、`/api/v1/order/trade_cache_health`、`/api/v1/stream/status`、`/api/v1/data/contracts/tick-bands/{rule}`、`/api/v1/stream/data/contract_event`。

## 2. 執行環境與探勘觀察

**環境**

- 官方 CLI：`shioaji` 1.7.6，以 `uv tool` 安裝；wheel 為 PyPI 的 `cp37-abi3-manylinux_2_17_x86_64`，sha256 與 PyPI 公布值一致。
- 設定：`~/.sinopac-p1/env`（0600），`SJ_HOME_PATH=~/.sinopac-p1/home`，綁 `127.0.0.1:18080`（本機另一個位址的 8080 已有其他程序 listen，改用 18080 避免混淆），另開 UDS `~/.sinopac-p1/home/server-18080.sock`。
- 啟動：`shioaji server start --no-open` 於前景執行，log 寫到 stdout／stderr，由呼叫端導向 `results/p1/<run_id>/server-start.log`；`shioaji server status` 只追蹤 daemon 模式，前景執行時顯示 `running: false`。
- 本機 `127.0.0.1:8081` 已被其他程序佔用，harness 實例改用 `18081`。

**探勘觀察（2026-09-29，模擬模式；證據在本機 `results/p1/*-discovery/`，P1 結束時挑為 fixture）**

- 登入要求對外 IP 在 API key 白名單內，否則回 400 `ip: <IP> not allow`；clientname 仍為 `PYAPI/{API key 前 10 字元}/…`。
- 啟動時即以 `SJ_CA_PATH` 啟用 CA，並在 log 記錄到期日。
- token 有效期約 24 小時（health 的 `token_expires_in_seconds` 起始值 86396），另有 `token_stale`、`session_recovering`、`session_recovery_attempts`。
- 每日維護：在台北時間 08:12–08:30 的窗口內加隨機偏移執行一次，下一次時間見 health 的 `next_maintenance`。
- 綁 localhost 時 HTTP 端不驗證 API key（log：`API key authentication disabled (localhost bind)`）。
- `SJ_AGENT_HARNESS` 在模擬模式預設 `mode=off`；啟用時以請求標頭 `X-Shioaji-Agent-Capability` 攜帶 `compact_keyed_blake3` 摘要，TTL 上限 60 秒。
- 帳戶：`S`（`signed: true`）、`F`（`signed: false`）、`H`（`signed: false`）。依永豐文件，`signed` 表示該帳戶已完成 API 簽署與模擬環境測試（登入 + 下單），證券與期貨分別簽署、分別測試；模擬模式下單本身即是測試手段，因此 `F` 未簽署預期不影響模擬單，正式環境期貨下單則需先完成。
- usage：`connections` 計的是同一身分的全部 session。舊 gateway 與官方 server 同時登入時為 2，兩次量測（Task 1 與之後的複查）一致，官方 server 一個實例佔 1 條。當日額度上限 2 GiB。
- SSE：`text/event-stream`，frame 為 `event:<名稱>` + `data:<JSON>`，沒有 `id:` 欄位；心跳是 `event:heartbeat` 事件，內含每條連線不同的 `connection_id`。
- 心跳間隔：期貨 API 測試的 `order_event` 串流中 17 個心跳時間戳，相鄰間隔皆為 30.000 秒 ±1 ms；錄製器以此設定 SSE 讀取逾時。
- 綜合串流 `/api/v1/stream/data` 與 `/api/v1/stream/data/tick_fop` 在訂閱 TMFJ6 tick 後收到相同數量的 frame；`order_event` 串流在未呼叫 `subscribe_trade` 前只有心跳。
- tick（`event:tick_fop`）價格為十進位字串，`date` 與 `time` 為台北本地時間且無時區標記，帶 `simtrade` 布林欄位。
- 訂閱與取消訂閱回 `{"success": true, "message": …, "subscription": …}`。
- 期貨 API 測試（2026-09-29 18:31–18:40，模擬模式）：`subscribe_trade` 後 `order_event` 送出拒單、New、Cancel 三個事件，成功事件的 `op_code` 為 `00`，拒單為 `88`；事件格式為 `{"state": "FuturesOrder", "data": {"FuturesOrder": {...}}}`，內層鍵等於 `state`，委託事件本身不帶狀態字串；登入加一筆期貨下單與刪單後，重新登入時 `F` 的 `signed` 由 `false` 變為 `true`。
- 夜盤以合約檔的 `limit_down` 買進 TMF 被拒（op_code `88`，「價格超過漲跌幅範圍」），以 `reference × 0.95` 買進則被接受。推論（未查證）：合約檔的漲跌停只適用期交所日盤，夜盤依當日日盤結算另計；該筆 18:33 下單時，日盤收盤 48193 × 0.9 ≈ 43374 已高於合約檔的 `limit_down` 43003。錄製器的期貨劇本單因此只在日盤下。
- 被交易所拒絕的委託不會在下單回應中顯示：期貨 API 測試中超出漲跌幅的那筆，`place_order` 回 HTTP 200、狀態 `PendingSubmit`，拒絕（op_code `88`）只出現在隨後的 `order_event` 與 `trades` 的 `Failed` 狀態。同步回 4xx 的是規則檢查失敗（舊 gateway 對應 422，`docs/sinopac.md`）。
- `place_order` 的 `account` 須帶 `/auth/accounts` 回傳的完整帳戶物件；只帶帳號時 `account_type` 預設為 `S`。
- 重啟 server 會沿用 `SJ_HOME_PATH` 下 token pool 的快取 token，不會重新登入；要強制重新登入須先 `shioaji utils token clean --slot <n>`。
- server log 的 `Subscribed trade events for account …` 一行含帳號明文，引用 server log 前須去識別化。

**10/1 live（Task 12；證據在本機 `results/p1/20261001T*`）**

- soak 起點延到 12:09：前一晚對外 IP 換了，快取 token 過期後重新登入被白名單拒絕（`not allow`），改用登記當日 IP 的新 API key 後才啟動。浮動 IP 會讓每日重新登入失敗，正式上線前需要固定對外 IP。
- 模擬模式的帳務查詢（`trading_limits`、`account_balance`、`margin`）可用額度皆回 0，preflight 的資金比對因此不成立（`funds_ok: false`，需求為股票 79914、期貨保證金 70100），下單照常被接受（P1REC-26）。
- 不帶帳戶的 `trades` 只回預設的證券帳戶：28 筆全為 `S`／`STK`，同日 16 筆期貨委託只出現在 SSE。對帳須逐帳戶查詢。
- 改量與刪單的數量欄位（股票與期貨生命週期案例各一筆，下 2 口、改價、減量 1、刪單，皆 `op_code` `00`）：`order.quantity` 始終為下單數量；`status.cancel_quantity` 是該次操作取消的量，不累計；`status.order_quantity` 是操作後仍有效的量（2→1→0）。REST 狀態為 `Cancelled`。
- 有一筆期貨委託的成交事件（`FuturesDeal`）早於它的 New 事件送達。
- 模擬撮合不會讓未成交的遠價 IOC／FOK 自動到期，委託停在 `Submitted`（P1REC-27）；以合約檔漲跌停掛價會被模擬環境以價格範圍退回。
- 兩個 client 的各串流 frame 數完全相同（行情 284837、委託 353、合約事件 266），對帳時兩者之間沒有缺漏的報告。
- 心跳：6 條串流約 2.2 小時，間隔 29.993–30.008 秒，沒有超過 60 秒的空白；`gap_threshold_s` 依此設為 60.1。
- 行情尖峰 859 frame／秒（13:04:47 台北時間）；收到時間減交易所時間 p50 21.8 ms、p99 132.7 ms、最大 241 ms（本機時鐘與 NTP 差 −4.3 ms），170 筆時間無法解析。
- 歷史查詢四筆（ticks、兩筆 kbars、snapshots）回應合計約 500 KB，`usage` 的 `bytes` 前後不變；推論：歷史查詢不計入當日額度，或計數不即時更新。

**假設關卡**

- A1 成立：`GET /api/v1/info` 的 `/simulation`。
- A2 成立：SSE 走 GET，訂閱與取消訂閱是 POST。
- A3 成立：下單、改單、刪單與查詢皆為 JSON over HTTP。
- A4 成立（規格層級）：`/api/v1/stream/data/order_event` 回傳 `OrderEvent`，需先 `subscribe_trade`；實際事件待 Task 12。

## 3. 量測清單答案

對應設計 §6；每項附 fixture 名稱或 log 行。

**A. 契約對應**

- A1 OpenAPI 規格：未作答
- A2 現行 adapter 契約 16 列的差距：未作答

**B. 行情**

- B1 SSE 訂閱流程、事件類型、frame 格式、心跳間隔：未作答
- B2 `Last-Event-ID` 斷點接續：未作答
- B3 五檔深度的檔數、時間戳格式與時區：未作答
- B4 試撮標記：未作答
- B5 盤中零股與期貨夜盤行情格式：未作答
- B6 開盤尖峰每秒訊息量與延遲：未作答

**C. 委託與成交**

- C1 各操作的同步回應與事件順序：未作答
- C2 委託條件組合（含拒單）：未作答
- C3 `trade_id`、`ordno`、`seqno` 對應：未作答
- C4 `custom_field` 長度、字元集與往返：未作答
- C5 股票成交數量單位：未作答
- C6 資金不足拒單與 IOC／FOK 未成交取消：未作答
- C7 重啟後當日委託與成交的取回：未作答

**D. 多 client**

- D1 client 之間的委託回報分送：未作答
- D2 一個 client 取消訂閱對其他 client 的影響：未作答

**E. 韌性**

- E1 重啟時 SSE client 的行為與訂閱是否需重做：未作答
- E2 session 過期與自動重登：未作答
- E3 合約更新事件：未作答

**F. 限制與權限**

- F1 速率上限與實際遇到的錯誤：未作答
- F2 `SJ_AGENT_HARNESS` 預設與 `all` 模式下的回應：未作答
- F3 連線數與每日額度：未作答
- F4 模擬模式下 CA 是否真的簽章：未作答
- F5 HTTP 端驗證與 CORS：未作答

**G. 歷史資料**

- G1 ticks、kbars、snapshots 的格式與額度消耗：未作答
