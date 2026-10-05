# Backlog — sinopac-nt-community

未完成工作的唯一清單，每一項都是表格中的一列並帶狀態。

- **Status**：`open`、`closed @<hash>`、`deferred`。關閉時就地改狀態，下次發行時移出本檔，由 hash 與 CHANGELOG 留下紀錄。
- 每列一行：症狀、位置、來源。新資訊改寫該列，不加註記。
- ID 公布後不重新編號。

## P1REC — P1（正確性與強化；live 前可選做）

| ID | Status | Item | Where | Source |
|---|---|---|---|---|
| P1REC-1 | open | 殘留部位旗標是整個行程共用，股票或零股成交案例被拒時會跳過所有期貨成交案例；改為依帳戶類型分開，或期貨案例先跑 | `orders.py` `_Runner.run_case`、`_residual_position` | final review（live-day） |
| P1REC-2 | open | 非成交案例回報 `PartFilled` 但 `deal_quantity` 為 null 時判為 `order_may_rest`（ERROR），應為 `residual_position`（CRITICAL） | `orders.py` `_order_state` | final review（live-day） |
| P1REC-3 | open | `reconcile --rest-run` 指向 orders run 時取到的是只查單一帳戶的 `trades`，期貨委託全變成 `only_in_sse`；只接受 client 為 `reconcile` 的 run | `reconcile.py` `last_trades_payload` | final review（live-day） |
| P1REC-4 | open | `startup_requests` 接受下單與刪單等操作，會在每次重連與每日重送時略過連鎖檢查送出；改為允許清單 | `config.py` stream 設定載入 | final review（live-day） |
| P1REC-5 | open | 重複訂閱若回 `success: false`（例如已訂閱）會在整個 soak 期間每 60 秒重試 | `stream.py` `_Coordinator.retry_failed` | final review（live-day） |
| P1REC-6 | open | 每日只在 08:35 重送一次，維護時間晚於此就要在 soak 中途重啟；改讀 health 的 `next_maintenance` 或在 08:45 前再送一次 | `stream.py` 每日重送 | final review（live-day） |
| P1REC-7 | open | SSE 錄製執行緒因非 httpx 錯誤（如磁碟滿）結束後不會重啟或升級告警，若是 leader 則重連後的重送會靜默停止 | `sse.py` `SseRecorder.run` | final review（live-day） |
| P1REC-8 | open | 交易時段預算未計入 HTTP 往返時間，server 緩慢時收尾可能越過 13:25 或 13:45 | `orders.py` `case_budget_s` | final review（live-day） |
| P1REC-9 | open | 不成交的股票案例沒有時段限制，誤在每日維護窗口執行時以 exit 1 與 CRITICAL 結束 | `orders.py` `session_for` | final review（live-day） |
| P1REC-10 | open | 交易額度很大時 `exceed_funds` 的張數可能超過證交所單筆上限，拒單原因會變成數量上限而非資金不足 | `orders.py` `exceed_funds_lots` | final review（live-day） |
| P1REC-11 | open | 對仍在寫入的 soak run 執行 `reconcile` 或 `stats` 可能讀到寫到一半的最後一行而以 exit 1 結束 | `records.py` `read_jsonl` | final review（live-day） |
| P1REC-12 | open | `orders` 沒有 SIGTERM／SIGHUP 處理，被終止時不寫最後的 `orders.json`；五日 `stream` 沒有監督程序 | `cli.py`、`orders.py` | final review（live-day） |
| P1REC-13 | open | 短的 `ca_passwd` 以子字串遮蔽，會把 log 時間戳與數字換成 `***` 並弄壞 `_log_line_ns`；改以 token 邊界比對 | `redact.py` `RedactingFormatter` | final review（safety） |
| P1REC-14 | open | 短值保護寫死 `ACCOUNT_ID`／`USERNAME` 類別名，但類別由 `p1.toml` 指標的最後一段決定，改名即失效 | `scrub/_map.py` | final review（safety） |
| P1REC-15 | open | 外洩掃描漏掉以 JSON 數字寫出的前導零帳號，以及 `*_b64` 欄位以外的 base64 token | `scrub/_matching.py` | final review（safety） |
| P1REC-16 | open | `account_id_keys` 只列 `subaccount`；加入 `account_id`、`person_id`、`username` 作為縱深防護，先確認文件不會誤報 | `p1.toml` `[scrub]` | final review（safety） |
| P1REC-24 | open | 沒給 `--trading-day` 且 `trades` 為空時，各 client 的 SSE 委託不計入任何欄位；改記未依日期過濾的數量，或計入 `excluded_other_days` | `reconcile.py` `run_reconcile` | final review（fb 複核） |
| P1REC-26 | open | 模擬模式的帳務查詢由 SDK 回傳寫死的 0，preflight 的資金比對在模擬下沒有意義；改為模擬時只記錄不比對，並修改設計與計畫 | `preflight.py`、計畫 Task 5 | live 10/1 |
| P1REC-27 | open | 模擬撮合不會讓未成交的 IOC／FOK 到期，遠價 IOC／FOK 案例停在 `Submitted` 而判為 `order_may_rest`；逾時後應主動刪單並記錄 | `orders.py` `_settle_fill` 與劇本 | live 10/1 |
| P1REC-28 | open | `exceed_funds_stock_2890` 掛跌停價會被模擬環境以價格範圍退回，且模擬不檢查資金；改用帶內價格或移除此案例 | `scenarios.toml`、`orders.py` `check_cases` | live 10/1 |
| P1REC-29 | open | 賣出案例的平倉買單仍掛 `limit_up`，會被模擬環境以價格範圍退回；目前劇本沒有賣出案例 | `orders.py` `_Runner._prepare` | live 10/1 |
| P1REC-30 | closed @0fcc0e3 | 對外 IP 會變動，錄製檔裡會出現多個 IP，但 scrub 只認一個 `P1_EGRESS_IP`；改為可列多個 | `config.py` `[secrets]`、`scrub/_map.py` | live 10/1 |

## P1REC — P2（整理；不改行為）

| ID | Status | Item | Where | Source |
|---|---|---|---|---|
| P1REC-17 | open | exit code 分散在多個模組與 `cli.py` 的字面值，收成一個模組 | `records.py`、`preflight.py`、`history.py`、`interlock.py`、`cli.py` | final review（safety） |
| P1REC-18 | open | 判斷 2xx 有三種寫法共十處，統一為一個 helper | `stream.py` `_is_success` 等 | final review（safety） |
| P1REC-19 | open | `_value_or_none` 兩份相同實作與 `scrub/_discovery.py` 的第三個 JSON pointer 走訪，改為 `jsonpointer` 的一個函式 | `broker.py`、`orders.py`、`scrub/_discovery.py` | final review（safety） |
| P1REC-20 | open | `recorder.log` 時間戳為本地時間且無時區，改為 UTC 並標 `Z` | `redact.py` `_LOG_FORMAT` | final review（safety） |
| P1REC-21 | open | pyright 在 `exc_value` 可能為 None 處報兩個 `reportArgumentType` | `redact.py` excepthook | final review（safety） |
| P1REC-22 | open | `scrub/_export.py` 重複定義 `records.py` 已有的時間常數與 `_iso_utc` | `scrub/_export.py` | final review（safety） |
| P1REC-23 | open | 部分註解帶數字或歷史、少句點或位置不當 | `p1.toml`、`scrub/_matching.py`、`orders.py` `_Runner.run_case` | final review（safety） |
| P1REC-25 | open | `p1rec` 沒有自己的 ruff 設定，套用根目錄給 adapter 的規則會報 101 項（多為 FURB157、UP017）；訂定此工具的規則集後修正或豁免 | `tools/p1-official-server/pyproject.toml` | final review（lint） |
