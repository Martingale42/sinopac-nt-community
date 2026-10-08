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
- 不帶帳戶的 `trades` 只回預設的證券帳戶：28 筆全為 `S`／`STK`，同日 16 筆期貨委託只出現在 SSE。改為逐帳戶查詢並依實測的數量語意推導狀態後，當日對帳 44 筆全數一致（REST 與兩個 client 皆 44 筆，無差異）。
- 改量與刪單的數量欄位（股票與期貨生命週期案例各一筆，下 2 口、改價、減量 1、刪單，皆 `op_code` `00`）：`order.quantity` 始終為下單數量；`status.cancel_quantity` 是該次操作取消的量，不累計；`status.order_quantity` 是操作後仍有效的量（2→1→0）。REST 狀態為 `Cancelled`。
- 有一筆期貨委託的成交事件（`FuturesDeal`）早於它的 New 事件送達。
- `custom_field`：6 字元的委託被接受，委託事件原樣帶回同樣 6 字元（2 筆）；7 字元在下單時同步回 422「ensure this value has at most 6 characters」。
- 股票委託的 MKP：6 筆 `place_order` 皆同步回 400 `parse http data failed`；期貨的 MKT、MKP 各委託條件皆成交。
- 交易所拒單只以 `op_code` `88` 的事件送達：「價格超過漲跌幅範圍」2 筆、「價格檔數錯誤」3 筆（ETF 零股以 0.01 的 ETF tick 掛價；改用股票 tick 後被接受）。
- 下單請求送出到 SSE 收到 New 事件：43 筆，中位數 70.8 ms、最大 557 ms。到第一筆成交事件：期貨多在 1 秒內，2890 與 00981A 多為數秒、最長 18.3 秒；推論：模擬撮合跟著真實成交發生。
- 模擬撮合不會讓未成交的遠價 IOC／FOK 自動到期，委託停在 `Submitted`（P1REC-27）；以合約檔漲跌停掛價會被模擬環境以價格範圍退回。
- 兩個 client 的各串流 frame 數完全相同（行情 284837、委託 353、合約事件 266），對帳時兩者之間沒有缺漏的報告。
- 心跳：6 條串流約 2.2 小時，間隔 29.993–30.008 秒，沒有超過 60 秒的空白；`gap_threshold_s` 依此設為 60.1。
- 行情尖峰 859 frame／秒（13:04:47 台北時間）；收到時間減交易所時間 p50 21.8 ms、p99 132.7 ms、最大 241 ms（本機時鐘與 NTP 差 −4.3 ms），170 筆時間無法解析。
- 歷史查詢四筆（ticks、兩筆 kbars、snapshots）回應合計約 500 KB，`usage` 的 `bytes` 前後不變；推論：歷史查詢不計入當日額度，或計數不即時更新。

**10/2 soak（證據在本機 `results/p1/20261001T040917Z-stream` 與 `results/p1/2026-10-01-server/server.log`）**

- 開始後約 29 小時內，6 條 SSE 從未斷線重連，錄製器沒有 WARN 或 ERROR，對外 IP 未變。
- 每日維護在 08:15:17 執行：server 自行重新登入取得新 token，並在約 1 秒內重放 24 個行情、2 個委託回報與 2 個 Contract V2 訂閱；錄製器 08:35 的全量重送（2 個 `subscribe_trade`、24 個訂閱）皆回 200。
- §6 D2：10:30 單一一次取消 2330 tick 訂閱後，client a、b 同時不再收到 2330 tick（之前各 1849 筆，之後皆 0），訂閱是 server 全域的。
- 夜盤（15:00 至次日 05:00）每個 client 約 26 萬筆期貨與選擇權 tick。
- server log 待 Task 14 歸類的三類：`/order/trades` 的「Conflicting reconciliation deal payload」WARN 854 筆，全在 10/1 密集查詢 `trades` 的時段；帳務查詢的「simulation mode, returning default」WARN 3 筆；10/2 14:50:05 的 `Syntax("too_short")` 解析 ERROR 2 筆，內容開頭像 TMF、TXF 訊息。

**10/3–10/4 中斷與續錄**

- 10/3 08:15 的每日維護與 08:35 的全量重送照常完成。
- 10/3 約 16:20 主機重開機（與官方 server 無關），soak 的錄製停在 16:19:17；各錄製檔最後一行皆為完整 JSON、無 NUL 位元組。中斷期間為週末休市，沒有漏錄交易時段。
- 10/4 04:51 以新的 run `20261003T205132Z-stream` 續錄到 10-06 09:30：快取 token 已失效，server 以原本登記的對外 IP 重新登入成功；啟動時的 2 個 `subscribe_trade` 與 24 個訂閱皆回 200。週五 10:30 的排程動作因已過期而跳過（WARNING）。

**10/5 對外 IP 變動、重新登入失敗與 soak 暫停**

- 對外 IP 在 04:11 至 05:11 之間又換了一次（五天內第二次），新 IP 不在 API key 白名單內；server 照常運作到 token 過期為止，08:11 時 `health` 已轉 `degraded`（`token_stale`）。
- 08:15 每日維護重新登入被拒（`not allow`），之後每隔 1、2、4、8 分鐘再每 10 分鐘重試，共 10 次皆失敗，09:20 後不再重試；`health` 回 503（`unhealthy`），與永豐的連線中斷且自行重連未恢復。server 不會自行脫離這個狀態。
- 失敗期間 `/api/v1/info` 仍回 200；`stream/subscribe` 24 個請求皆回 200，但最後一筆 tick 停在 08:35:00，此後只有心跳；`subscribe_trade` 先回 401，錄製器每 60 秒重試，到 14:45 共回 741 次 500。SSE 連線本身沒有中斷。
- Task 13 的三個步驟因 server 未登入而未執行；14:45 暫停錄製（run `20261003T205132Z-stream` 正常結束），14:48 停止 server。使用者決定改在有固定對外 IP 的 AWS 台北主機上重跑 soak。

**固定 IP 主機（10/6 架設；計畫 `docs/plans/2026-10-06-soak-rehost-plan.md` R4；證據在本機 `results/p1/2026-10-06-server/` 與 `results/p1/20261006T084434Z-stream`）**

- 主機：AWS 台北區域的 `t3.micro`（1 GiB），另加 2 GiB swap；時區 `Asia/Taipei`；chrony 與 NTP 相差約 2 µs。
- 新 API key 的白名單只有這台主機的固定 IP；舊 gateway 的設定未動。
- 官方 server、錄製器與每 5 分鐘一次的本地監看都是 systemd 服務（`tools/p1-official-server/deploy/`），開機自動啟動。
- 第一次登入（10/6 16:44，模擬模式）：`simulation` 為 `true`，health 為 `healthy`，token 剩 86391 秒，下次維護 10/7 08:28:32；`usage.connections` 為 1，舊 gateway 沒有同時登入；帳戶 `S`、`F` 已簽署，`H` 未簽署；出口 IP 等於固定 IP；server log 沒有 `not allow`、ERROR 或 WARN。
- 錄製器啟動時唯一的 WARNING 是過期的 10/2 排程動作被跳過，與 10/4 續錄時相同。
- soak 為 10/6 16:44 第一次登入至 10/12 09:30，不另開正式窗口（設計補充 §1、§11.2）。
- 10/7 每日維護在 08:28:32 開始，約 1.3 秒內重新登入並重放 24 個行情與 2 個委託回報訂閱。jitter 每次啟動都相同（見 10/8），每天同一時刻維護；新 token 的到期時間等於下次維護時間。維護時唯一的 WARN 是 `No legacy contracts loaded after client replacement`，與家用網路時相同；完成訊息為 `0 contracts loaded`，訂閱改經 Contract V2 解析。
- 錄製器 08:35 的全量重送（2 個 `subscribe_trade`、24 個訂閱）皆回 200。兩個 client 的行情 frame 數相同；超過 60 秒的空白只出現在 05:00–08:30 與 13:45 之後的休市時段。
- 資源（試跑第一天）：CPU 每小時平均最高 5%，CPU credit 維持在上限；兩個服務的匿名記憶體各約 18 MiB，systemd 的 `MemoryCurrent` 含寫檔造成的 page cache，因此在 17–395 MiB 之間擺動；swap 用量約 50 MiB；錄製檔每天約 2 GB。
- 取回：拉回的 10 個檔案，每個檔案前綴的 sha256 都與 VPS 上相同。
- 10/8 Task 13（計畫 R7；12:53 起，比預定晚約 2.5 小時，仍在期交所日盤內）：`self-test-interlock` 回 0；`preflight` 只因資金比對回 2（P1REC-26）；`usage.connections` 為 2，第二條推論為舊 gateway 容器，它過去 24 小時的 log 有 226 行 `not allow`。
- harness 實例（`SJ_AGENT_HARNESS=all`、`127.0.0.1:18081`）：登入與 CA 啟用成功後，HTTP server 拒絕啟動，訊息為 `Invalid HTTP transport configuration: SJ_AGENT_HARNESS_SECRET is required for environment bootstrap`。`all` 模式需要另設這個密鑰，計畫未預見；它是讓 agent 取得下單能力的密鑰，當日未產生，也未下 harness 單，F2 待使用者決定。
- 經 soak 實例下單：`lifecycle_futures_tmf` 完成生命週期，`market_futures_tmf_mkt_ioc` 成交後平倉，共 3 張委託；`orders` 回 0。
- 盤中重啟 soak 實例（12:55:46）：沿用快取 token，2 秒內 `healthy`，錄製器未重啟。三次啟動（10/6 soak 實例、10/8 harness 實例、10/8 重啟）的 jitter offset 都是 992 秒，維護時刻固定在 08:28:32，與啟動時間無關。
- 重啟後 `reconcile --trading-day 2026-10-08`：`rest_order_count` 為 3，兩個 client 各 3；`only_in_rest`、`only_in_sse`、`mismatched` 與 `missing_events_by_client` 皆為 0。重啟後 REST 能取回當日全部委託與成交（GO 準則 5）。

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
