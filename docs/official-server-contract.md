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

### 1.1 adapter 現行契約的差距表（§6 A2）

adapter v0.1.x 對舊 gateway 用到 11 個 HTTP endpoint 與 5 種 WebSocket 業務訊息，共 16 列；連線與控制訊息另列。判定依 `openapi.json` 與 §2、§3 的實測。

| # | adapter 項目 | 官方 1.7.6 對應 | 判定 | 轉換要點 |
|---|---|---|---|---|
| 1 | 股票合約 | GET `/api/v1/data/contracts`（分頁）＋ `/contracts/{code}/info` | 可轉換 | 清單只有 `code`、`exchange`，其餘欄位逐檔查 info，或只載入設定的商品 |
| 2 | 期貨合約 | GET `/api/v1/data/contracts/futures` | 可轉換 | 欄位改名（`root`、`quote_ccy`），欄位可為 null；另帶 `tick`、`tick_rule` |
| 3 | 選擇權合約 | GET `/contracts/options/roots` → 逐 root 查 `/contracts/options` | 可轉換 | `root` 必填 |
| 4 | 歷史 tick | POST `/api/v1/data/ticks` | 可轉換 | JSON 本體帶 `contract`；時間為 `datetime` 字串 |
| 5 | 歷史 K 棒 | POST `/api/v1/data/kbars` | 可轉換 | 鍵名大寫並多 `Amount`；只有 1 分 K（G1），其他粒度自行聚合 |
| 6 | 新單 | POST `/api/v1/order/place_order` | 可轉換 | `account` 帶完整帳戶物件；`trade_id` 取 `order.id`；股票 MKP 同步 400，需在本地擋；數量單位依 `order_lot`（C5） |
| 7 | 改單 | POST `/order/update_price`、`/order/update_qty` | 可轉換 | 改價與改量拆成兩個請求；`update_qty` 的數量是減少量或新總量未能區分，P2 驗證 |
| 8 | 刪單 | POST `/api/v1/order/cancel_order` | 可轉換 | 回應改為 `Trade` |
| 9 | 當日委託 | POST `/api/v1/order/trades` | 可轉換 | 逐帳戶查；成交量取 `status.deal_quantity`；只回當日（C7） |
| 10 | 部位 | POST `/api/v1/portfolio/position_unit` | 可轉換 | 以 `S`／`F` 帳戶查；股票部位的 `unit` 未實測，P2 驗證 |
| 11 | 餘額 | POST `/api/v1/portfolio/account_balance` | 可轉換 | 模擬模式回 0 |
| 12 | 訂閱 | POST `/api/v1/stream/subscribe` | 可轉換 | 同步回應；server 全域；server 未登入時仍回 200，成敗另看 health |
| 13 | 取消訂閱 | POST `/api/v1/stream/unsubscribe` | 可轉換 | 一個 client 取消，所有 client 都停收（D2） |
| 14 | tick 推送 | SSE `tick_stk`、`tick_fop` | 可轉換 | 價格為十進位字串；時間為無時區的台北本地字串；`simtrade` 標記試撮 |
| 15 | bidask 推送 | SSE `bidask_stk`、`bidask_fop` | 可轉換 | 五檔；時間格式同 tick（B3） |
| 16 | 委託與成交回報 | `subscribe_trade` ＋ SSE `order_event` | 可轉換 | 外層 `state` 加同名內層鍵；`event_id` 可去重；拒單以 `op_code` `88`；成交可能早於 New |
| 附 A | 連線、心跳、重連 | 每種資料一條 SSE，server 每 30 秒送心跳 | 可轉換 | frame 沒有 `id:`，斷線後以 REST 對帳補齊（B2） |
| 附 B | 控制與錯誤 | 同步 HTTP 回應與 `ErrorResponse` | 可轉換 | 錯誤改由 HTTP 狀態碼承載 |

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
- harness 實例（`SJ_AGENT_HARNESS=all`、`127.0.0.1:18081`）：登入與 CA 啟用成功後，HTTP server 拒絕啟動，訊息為 `Invalid HTTP transport configuration: SJ_AGENT_HARNESS_SECRET is required for environment bootstrap`。`all` 模式需要另設這個密鑰，計畫未預見；它是讓 agent 取得下單能力的密鑰，當日未產生，也未下 harness 單。使用者決定不測 harness，F2 以官方文件作答並維持關閉：soak 實例的 `/api/v1/agent/harness` 為 `enabled: false`、`mode: off`，harness 的設定檔與 token 已刪除。官方說明（[shioaji-pro-app#51](https://github.com/Sinotrade/shioaji-pro-app/issues/51)、[#47](https://github.com/Sinotrade/shioaji-pro-app/issues/47)）把 agent 權限分為唯讀、逐筆確認與受控自動，正式環境的自動下單須使用者明確選擇並經原生授權，且稱 `SJ_AGENT_HARNESS_SECRET` 環境變數是不足的舊式 bootstrap，因為同一使用者的程序可讀取 sidecar 的環境而繞過 App 的核准。
- 經 soak 實例下單：`lifecycle_futures_tmf` 完成生命週期，`market_futures_tmf_mkt_ioc` 成交後平倉，共 3 張委託；`orders` 回 0。
- 盤中重啟 soak 實例（12:55:46）：沿用快取 token，2 秒內 `healthy`，錄製器未重啟。三次啟動（10/6 soak 實例、10/8 harness 實例、10/8 重啟）的 jitter offset 都是 992 秒，維護時刻固定在 08:28:32，與啟動時間無關。
- 重啟後 `reconcile --trading-day 2026-10-08`：`rest_order_count` 為 3，兩個 client 各 3；`only_in_rest`、`only_in_sse`、`mismatched` 與 `missing_events_by_client` 皆為 0。重啟後 REST 能取回當日全部委託與成交（GO 準則 5）。

**假設關卡**

- A1 成立：`GET /api/v1/info` 的 `/simulation`。
- A2 成立：SSE 走 GET，訂閱與取消訂閱是 POST。
- A3 成立：下單、改單、刪單與查詢皆為 JSON over HTTP。
- A4 成立（規格層級）：`/api/v1/stream/data/order_event` 回傳 `OrderEvent`，需先 `subscribe_trade`；實際事件待 Task 12。

## 3. 量測清單答案

對應設計 §6；fixture 在 `test_data/official/1.7.6/`，來源與行號見其 `MANIFEST.toml`。

**A. 契約對應**

- A1 OpenAPI 規格：`openapi.json`，OpenAPI 3.1.0，100 條路徑、209 個 schema；server 版本見 `A2_info`。
- A2 差距：§1.1 的 16 列與附錄全部可轉換，沒有「缺」（`A2_info`、`A2_accounts`、`A2_contracts`、`A2_funds_queries`）。

**B. 行情**

- B1 每種資料一條 GET 端點，`text/event-stream`，frame 為 `event:` 加 `data:<JSON>`；事件有 `tick_stk`、`tick_fop`、`bidask_stk`、`bidask_fop`、`order_event`、合約事件與 `heartbeat`，綜合串流也帶 `order_event`。心跳每 30 秒，含各連線不同的 `connection_id`。訂閱以 POST 送出、為 server 全域（`B1_stream_subscribe`、`B1_heartbeats`、`B1_tick_stk`、`B1_tick_fop`）。
- B2 不能以 `Last-Event-ID` 接續：frame 沒有 `id:`，重連後換新 `connection_id`，斷線期間的 frame 不會重放；漏掉的回報由 REST `trades` 補（`B1_heartbeats`、`E1_heartbeats_across_restart`）。
- B3 買賣各 5 檔，另有 `first_derived_*`、`diff_*_vol` 與總量；`date`、`time` 為台北本地字串、無時區（`B3_bidask_stk`、`B3_bidask_fop`）。
- B4 tick 以 `simtrade` 布林標記試撮；10/7 的 2330 在 09:00:09 轉為一般 tick，0050 到 09:02:00 仍為 `true`；TXFJ6 在 08:45:00 轉換（`B4_simtrade_stk`、`B4_simtrade_fop`）。
- B5 盤中零股以 `tick_stk` 送出並帶 `intraday_odd: true` 與 `closing_oddlot_*`；夜盤期貨與日盤同為 `tick_fop`、`bidask_fop`（`B5_odd_lot_quotes`、`B5_night_session_quotes`）。
- B6 10/1（家用網路）每秒尖峰 859 frame，收到時間減交易所時間 p50 21.8 ms、p99 132.7 ms（`B6_data_stream_stats`）。09:00 開盤尖峰當日未錄到；固定 IP 主機的整段 soak 統計見 §4：p50 6.5 ms、p99 132 ms，每秒尖峰 2,182 frame。

**C. 委託與成交**

- C1 `place_order` 同步回 200 與 `Trade`（`PendingSubmit`）；交易所接受以 `op_code` `00` 的 New 事件送達，拒單只以 `op_code` `88` 事件與 `trades` 的 `Failed` 出現；規則檢查失敗同步回 4xx；成交事件可能早於 New（`C1_stock_lifecycle_*`、`C1_futures_lifecycle_*`、`C1_deal_before_new_events`）。
- C2 期貨 MKT、MKP 的 ROD、IOC、FOK 皆成交；股票與零股的 MKP 同步回 400；超出漲跌幅與零股價格檔數錯誤以 `88` 拒絕（`C2_*`）。
- C3 成交事件的 `trade_id` 等於委託的 `order.id`，`seqno`、`ordno` 也一致，10/1 的 26 筆成交全數對得上；刪單以 `order.id` 為 `trade_id`（`C3_futures_fill_rest`、`C3_futures_fill_events`）。
- C4 `custom_field` 6 個 base62 字元原樣往返（含邊界字元與大小寫），7 字元在下單時同步 422；編碼以 ADR-0005 定案（`C4_custom_field_rest`、`C4_custom_field_events`）。
- C5 成交數量與委託同單位，由 `order_lot` 決定：`Common` 為張，`IntradayOdd` 為股；舊 gateway 一律乘 1000 的假設在零股上錯誤（`C5_board_lot_deal_events`、`C5_odd_lot_deal_events`）。
- C6 未知：模擬模式的資金查詢回 0 卻照常接單（P1REC-26），資金不足拒單在模擬下無法觸發；遠價 IOC、FOK 在模擬中停在 `Submitted`、不會自動取消（P1REC-27）。兩者的正式環境行為留給 P2（`C6_ioc_fok_resting_events`、`C6_ioc_resting_rest`、`A2_funds_queries`）。
- C7 10/8 盤中重啟後，`trades` 取回當日 3 張委託與成交，與兩個 client 的事件逐筆一致，不含其他日期的委託（`C7_trades_after_restart`）。

**D. 多 client**

- D1 每個 SSE client 都收到所有帳戶的全部回報，兩個 client 的 frame 數相同（`D1_client_b_order_events`，對照 `C1_futures_lifecycle_events`）。
- D2 訂閱為 server 全域：10/2 一次取消 2330 tick 後，兩個 client 同時停止收到（`D2_*`）。

**E. 韌性**

- E1 server 重啟時 SSE 斷線，約 1 秒後可重連並換新 `connection_id`；重啟沿用快取 token。錄製器重連後立即重送訂閱，全部回 200；server 會不會自行恢復訂閱為未知，因為重送早於能觀察的時間點（`E1_*`）。
- E2 token 約 24 小時；每日維護在依帳戶固定的時刻（本帳戶 08:28:32）重新登入並重放訂閱，新 token 在下次維護時到期。登入被拒時，間隔 1、2、4、8 分鐘再每 10 分鐘重試，共 10 次後等到隔日維護；期間 health 回 503、`/info` 仍回 200、SSE 只剩心跳、`subscribe_trade` 先回 401 再回 500（`E2_*`）。
- E3 合約事件經 `contract_event` 串流送出，server log 同時記 `SYS/CONTRACT Check invalidation`；觀察到 10/7 13:55 與 10/8 07:45（FUT、OPT、STK）（`E3_contract_events`）。

**F. 限制與權限**

- F1 未知：未刻意觸發，1729 個 REST 回應中沒有 429，log 也沒有速率限制的訊息。
- F2 模擬模式預設 `off`；`all` 模式缺 `SJ_AGENT_HARNESS_SECRET` 時拒絕啟動，使用者決定不測，以官方文件作答（§2 的 10/8 紀錄）。正式環境的自動下單須使用者明確選擇並經原生授權，只能以正式帳戶驗證（`A2_info`、`F2_harness_all_refused_log`）。
- F3 一個官方實例佔 1 條連線，上限 5；每日額度 2 GiB。歷史查詢前後 `bytes` 不變；串流一天在磁碟上達 GB 等級，`bytes` 只到數十 MB，推論串流不全額計入（`F3_usage`、`G1_*`）。
- F4 未知：啟動時即以 `.pfx` 啟用 CA 並記錄到期日，但模擬模式的下單回應與事件中 `ca` 皆為空，是否真的簽章無法從閉源執行檔確認（`F4_ca_status`、`F4_ca_activation_log`）。
- F5 綁 localhost 時不驗證 API key；帶 `Origin` 的請求沒有收到 `Access-Control-Allow-Origin`，只有 `access-control-expose-headers`（`F5_http_auth_log`、`A2_info`）。

**G. 歷史資料**

- G1 ticks、kbars、snapshots 皆以 POST JSON 本體查詢；kbars 只有 1 分 K（2330 一日 266 根）；四筆查詢前後 `usage.bytes` 不變（`G1_kbars_2330`、`G1_snapshots`）。

## 4. Soak 與 log sweep

固定 IP 主機的 soak 從 10/6 16:44 錄到 10/12 16:54（計畫 R8），之後停止服務；結果是 P2 的參考，不改變 §5 的結論（設計 §9.3）。範圍包括 soak 與 harness 實例的 server log、15 個 `recorder.log`、兩個服務的 journald、`watch.jsonl`，以及 10/1–10/5 家用網路的 server log 與錄製器 log。

**串流統計（`20261006T084434Z-stream`）**：兩個 client 各 6,498,899 個行情 frame，各類事件數相同；心跳間隔中位數 30.000 秒、最大 30.09 秒，沒有超過 60.1 秒的空白；唯一的斷線是 10/8 計畫內的重啟（1.03 秒重連）。收到時間減交易所時間 p50 6.5 ms、p99 132 ms、最大 5.0 秒；每秒尖峰 2,182 frame（10/8 15:00 夜盤開盤），期貨日盤 08:45 開盤約 1,250 frame。監看 1731 次檢查沒有任何異常；systemd 自動重啟 0 次、OOM 0 次、主機重開機 0 次。

**session 過期**：token 在下一次每日維護的時刻到期，維護時無條件重新登入，所以重新登入的週期就是維護的週期，24 小時 ±1 秒。固定 IP 主機 6 次維護全部第一次即成功，維護以外的重新登入 0 次；家用網路的機制相同（08:15:17，3 次成功、10/5 因白名單失敗）。P2 watchdog 的依據：`next_maintenance` 過後幾分鐘仍未換到新 token，或出現第一行 `Daily maintenance failed`，即告警；重試序列在 65 分鐘後結束，之後 server 不會自行恢復。

| 類別（已遮蔽的樣板） | 次數（固定 IP ／家用） | 處置：說明 |
|---|---|---|
| WARN `No legacy contracts loaded after client replacement` | 6 ／ 3 | 接受：每次維護成功換新 client 時記一次，之後訂閱全數重放 |
| ERROR `Syntax("too_short")` | 2 ／ 2 | 契約發現：期交所預備盤（08:30:05、14:50:05）送出的清空委託簿 frame 帶空字串 `time`，解析失敗；不保證每次送出，價格 `"0"` 的檔位應視為空檔 |
| WARN `<op>: simulation mode, returning default` | 3 ／ 3 | 契約發現：模擬模式的帳務查詢回 0（P1REC-26） |
| WARN `Conflicting reconciliation deal payload` | 3 ／ 854 | 契約發現：查詢已有成交的委託時，server 保留事件確認的版本；對帳兩次皆 0 差異，隨 `trades` 查詢次數增加，監看不應告警 |
| ERROR 登入或維護 `not allow`（含重試用盡） | 0 ／ 12 | 接受：家用對外 IP 不在白名單；契約發現：維護失敗後重試 10 次再等到隔日，P2 需要固定 IP 與 watchdog |
| WARN `Solace SDK is reconnecting` 與 SDK NOTICE | 0 ／ 6 | 接受：10/5 家用 IP 變動時行情通道斷線，24 秒後自行重連，不需重新登入 |
| ERROR `Failed to start HTTP server: … is required for environment bootstrap` | 1 ／ 0 | 契約發現：`all` 模式需要 harness 密鑰（§2 的 10/8 紀錄） |
| WARNING `skipping scheduled unsubscribe … had passed at startup` | 1 ／ 1 | 接受：啟動時跳過已過期的排程動作 |
| WARNING `SSE … disconnected: … incomplete chunked read` | 6 ／ 0 | 接受：10/8 計畫內重啟；契約發現：server 停止時不正常結束 chunked 回應，adapter 要當成可重連的斷線 |
| ERROR `insufficient stock funds`、`insufficient futures margin` | 2 ／ 0 | 接受：P1REC-26 的下游結果 |
| ERROR `subscribe_trade … failed: HTTP 401／500` | 0 ／ 2 | 接受：session 已失效時的重送；契約發現：401、500 都代表 session 失效 |

- server 把 HTTP 4xx、5xx 記為 INFO：家用網路 10/5 有 500 ×741、503 ×8；只看 WARN、ERROR 的監看會漏掉。
- 沒有原因不明的類別。`Conflicting reconciliation deal payload` 衝突的欄位，以及預備盤清空 frame 只在部分日子帶空 `time` 的原因，只推論到觸發條件，列為 P2 的選做待辦（`BACKLOG.md` OFFSRV-1、OFFSRV-2）。

## 5. go/no-go

依設計 §8，2026-10-08 判定。

| 準則 | 判定 | 依據 |
|---|---|---|
| 1. P2 第一輪功能皆為相同或可轉換 | 成立 | §1.1 的 16 列全部可轉換 |
| 2. 下單回應、委託回報、成交回報的 ID 能確定對應 | 成立 | C3 |
| 3. `custom_field` 至少往返 6 個 base62 字元 | 成立 | C4、ADR-0005 |
| 4. SSE 回報與同時段 REST 當日委託逐筆比對沒有遺漏 | 成立 | 10/1 44 筆、10/8 3 筆皆一致 |
| 5. 重啟後 REST 能取回當日委託與成交 | 成立 | C7 |
| 6. harness 有文件記載的自動化方式，或可由設定關閉 | 模擬模式成立 | 預設 `off`（F2）；正式環境留給 P2 驗收 |

**結論：GO。** P2 直接接官方 server，不開 P3。§1.1 的轉換都可在 adapter 內完成；只有多個 node 共用一台官方 server 時，才需要一層輔助服務處理訂閱引用計數與回報分送。

P2 必須處理的發現：

- 官方 server 登入失敗時 `/info` 仍回 200、訂閱仍回 200 卻收不到行情（E2）；adapter 要看 `/api/v1/health`，不能以 `/info` 判斷是否可用。
- server 重啟後由 adapter 重送訂閱與 `subscribe_trade`（E1）；frame 沒有 `id:`，斷線後以 `trades` 對帳（B2）。
- 股票數量單位依 `order_lot`（C5）；`place_order` 帶完整帳戶物件；`trades` 逐帳戶查。
- API key 綁對外 IP 白名單，官方 server 必須在固定 IP 的主機上執行（§2 的 10/5 與固定 IP 主機紀錄）。

風險與 P2 的驗證項目：

- **正式環境能否讓程式自動下單**：官方文件說正式環境的自動下單須使用者明確選擇並經原生授權，只有正式帳戶量得到，列為 P2 第一個驗證項目。
- 正式環境才看得到的行為：CA 是否真的簽章（F4）、資金不足拒單（C6）、IOC、FOK 是否自動取消（C6）、`update_qty` 的數量語意與股票部位的 `unit`（§1.1）。
- 速率上限未知（F1），adapter 需要自己的節流。
- soak 的 log sweep 與固定 IP 主機的串流統計見 §4，結果是 P2 的參考，不改變本結論（設計 §9.3）。
