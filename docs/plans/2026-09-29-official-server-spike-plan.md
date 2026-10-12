# P1 官方 shioaji server 驗證 spike 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task.

**Goal:** 建黑箱錄製器 `p1rec`，用它對官方 server 1.7.6（模擬模式）完成量測、語料錄製與 5 天 soak，產出契約文件與 go/no-go 結論。

**Architecture:** `tools/p1-official-server/` 是獨立的 uv 專案（套件 `p1rec`），只相依 `httpx`。REST 與 SSE 一律先原樣落地成 JSONL，分析子命令只讀檔。endpoint 路徑、JSON 指標、請求範本與劇本都放在 TOML，程式只認邏輯操作名稱；Task 1 以真實 server 探勘後填入。

**Tech Stack:** Python 3.12+、httpx（同步；一個 SSE client 一條執行緒）、pytest、uv、官方 `shioaji` 1.7.6 CLI。

**Read first:** `docs/plans/2026-09-29-official-server-spike-design.md` 全文、`CONTEXT.md`、`docs/adr/0004-order-tag-reserves-node-id.md`。

**Done signal:**

- 離線：在 `tools/p1-official-server/` 下 `uv run pytest -q` 全綠（含 Task 11 離線演練）。
- **env-gated**：Task 15 的兩個驗證指令通過。需要 `~/.sinopac-p1/env`（正式帳戶 key 與 CA）、白名單內的對外 IP、證交所行事曆上連續的週四至下週二交易窗口。

**執行分工：** Task 1、12、13、14 為 **live**（持有正式帳戶憑證、依交易時段跨多日），由主 session 執行，不交給 subagent；其餘離線任務交給 subagent-driven-development。

**假設關卡：** Task 1 推翻下列任一假設時，停止後續任務、回報並修訂本計畫。

- A1：目前模式可由一個 GET 回傳的 JSON 布林欄位讀出。
- A2：即時資料走 HTTP SSE（GET 串流），訂閱與取消訂閱是 HTTP 請求。
- A3：下單、改單、刪單與各種查詢都是 JSON over HTTP。
- A4：規格顯示委託與成交回報會出現在 SSE 上。

**Task 1 結果（2026-09-29）：** A1–A4 皆成立（A4 為規格層級）。探勘同時發現：SSE 依資料類型分成多條端點、訂閱為 server 全域且沒有 connection 識別、心跳是 `event:heartbeat` 事件、frame 不帶 `id:`、歷史查詢為 POST 本體、官方 server 一個實例佔 1 條連線；Task 3、5、6、8、9、13 已依此修訂。事實與證據見 `docs/official-server-contract.md` §2。

**長度說明：** 本計畫約為設計文件的 3.5 倍，超出兩倍的預算。原因是設計 §5 只定下錄製器的方向與子命令，錄製器的內部規格（Task 2–11）由本計畫承擔；每個子命令都對應設計中的一條要求，因此沒有刪減。

**工作目錄：** 離線任務的 `uv run` 指令都在 `tools/p1-official-server/` 下執行。

---

## 檔案結構

```
tools/p1-official-server/
  pyproject.toml   uv.lock
  p1.toml          # live 設定，無密鑰
  scenarios.toml   # 下單劇本
  fixtures.toml    # 語料挑選清單
  src/p1rec/
    cli.py         # 子命令分派、--config、--tag、結束碼
    config.py      # TOML 載入與驗證
    rundir.py      # run 目錄
    redact.py      # 遮蔽密鑰的 logging
    capture.py     # REST 原樣錄製
    sse.py         # SSE frame 切分、解析、錄製執行緒
    interlock.py   # 模擬模式連鎖檢查
    scenarios.py   # 劇本資料模型
    prices.py      # 價格參照與升降單位
    preflight.py   # 第 0 步
    stream.py      # 多 client 串流與排程動作
    orders.py      # 劇本執行
    records.py     # 錄製檔（JSONL、SSE frame）讀取與時間換算，對帳與統計共用
    reconcile.py   # 對帳、client 覆蓋
    stats.py       # 串流統計
    history.py     # 歷史資料查詢與額度量測
    scrub/         # 去識別化、語料匯出、掃描（比對與掃描、對照表、找值、匯出、流程各一個模組）
  tests/  stub_server.py  data/  test_*.py
test_data/official/1.7.6/   # openapi.json、fixture、MANIFEST.toml
docs/official-server-contract.md
```

全部子命令共用的結束碼：`0` 成功；`1` 未預期錯誤；`2` 檢查未通過（資金不足、對帳不一致、發現洩漏）；`3` 連鎖檢查中止。

---

### Task 1（live）：啟動官方 server 並探勘 API

**Files:**
- Create: `test_data/official/1.7.6/openapi.json`、`docs/official-server-contract.md`（本任務只寫 §1 端點對照、§2 執行環境）

**Behaviour:**

- `uv tool install shioaji==1.7.6` 後 `shioaji version` 顯示 1.7.6。
- 依設計 §4 建立 `~/.sinopac-p1/env`（0600），不設 `SJ_PRODUCTION`。
- 載入 env 以 `shioaji server start --no-open` 啟動，`shioaji server check` 顯示模擬模式。登入回 `not allow` 時停下比對對外 IP 與白名單，不重試。
- 找出 server log 的實際位置，寫入契約文件 §2。
- 從 `/docs` 頁面找出規格 URL，下載並以 `jq -S .` 排版存為 `openapi.json`。
- 契約文件 §1：每個邏輯操作一列（method、path、請求 schema、回應 schema、備註），規格中沒有的標「缺」。邏輯操作固定為：`mode`、`accounts`、`contracts_stock`、`contracts_futures`、`contracts_options`、`snapshots`、`ticks`、`kbars`、`stream_connect`、`stream_subscribe`、`stream_unsubscribe`、`place_stock`、`place_futures`、`update_price`、`update_qty`、`cancel`、`trades`、`positions`、`balance`、`margin`、`usage`、`ca_status`、`harness_info`。
- 契約文件 §3 預先列出設計 §6 A–G 的每一項，一律標「未作答」，由後續任務逐項填答。
- 另記：`mode` 回應中代表模擬模式的 JSON 指標；HTTP 端是否要求任何驗證；SSE 連線是否帶 connection id 與其來源；可訂閱的報價類型（含盤中零股）；合約更新事件的取得方式；`ca_status` 的回應（CA 是否已啟用）；`SJ_AGENT_HARNESS` 若可見則記預設值。
- 逐條檢查 A1–A4，任一不成立即停止。
- 確認 `127.0.0.1:18081` 未被佔用；結束時 `shioaji server stop`。

**Tests:** 不適用（探勘）。

**Numbers:**

- `18081`：Task 13 第二實例的埠號。`8081` 已被本機其他程序佔用（2026-09-29 查證），改用經本任務確認未被佔用的 `18081`。

**Verification:**

Run: `jq '.paths | length' test_data/official/1.7.6/openapi.json`
Expected: 大於 0。

Run: `set -a; . ~/.sinopac-p1/env; set +a; grep -c -F -e "$SJ_API_KEY" -e "$SJ_SEC_KEY" -e "$SJ_CA_PASSWD" test_data/official/1.7.6/openapi.json docs/official-server-contract.md`
Expected: 兩個檔案皆為 `0`。

**Commit:**

```bash
git add test_data/official/1.7.6/openapi.json docs/official-server-contract.md
git commit -m "Record official server 1.7.6 OpenAPI spec and endpoint map"
```

---

### Task 2：錄製器骨架、設定載入與遮蔽密鑰的 logging

**Files:**
- Create: `tools/p1-official-server/{pyproject.toml,p1.toml}`、`src/p1rec/{__init__,cli,config,rundir,redact}.py`
- Modify: `.gitignore`（加 `/results/`）
- Test: `tests/test_config.py`、`tests/test_rundir.py`、`tests/test_redact.py`

**Behaviour:**

- `pyproject.toml`：套件 `p1rec`、console script `p1rec`、相依 `httpx`、dev 相依 `pytest`、`[tool.pytest.ini_options] testpaths = ["tests"]`。
- `p1rec --help` 列出子命令 `self-test-interlock`、`preflight`、`stream`、`orders`、`reconcile`、`stats`、`history`、`scrub`；全部接受 `--config`（預設 `p1.toml`）與 `--tag`；`preflight`、`orders` 另接受 `--scenarios`（預設 `scenarios.toml`）。`p1.toml` 中的相對路徑一律以 `p1.toml` 所在目錄為基準。
- 設定缺鍵或型別錯誤時以 `ConfigError` 結束，訊息含完整鍵名（例如 `endpoints.place_stock.path`）。
- 密鑰只從 `[secrets]` 指定的環境變數讀取；TOML 出現 `api_key`、`sec_key`、`ca_passwd` 鍵時拒絕載入；環境變數缺少時以 `ConfigError` 指出變數名。
- 每次執行建立 `results/p1/<UTC YYYYMMDDTHHMMSSZ>-<子命令>[-<tag>]/`；已存在則失敗，不覆寫。
- logging 同時寫 `recorder.log` 與 stderr；訊息中出現的任何密鑰值（含 API key 前 10 字元）替換為 `***`。
- `p1.toml` 的路徑與指標取自 Task 1 的端點對照表。

**Interface:**

```python
@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str  # 可含 {name}，由 path_vars 代入

@dataclass(frozen=True)
class P1Config:
    base_url: str
    http_timeout_s: float
    results_root: Path
    fixtures_root: Path
    secret_env: dict[str, str]      # 角色 -> 環境變數名：api_key, sec_key, ca_passwd, person_id, egress_ip
    endpoints: dict[str, Endpoint]  # 鍵為 Task 1 的邏輯操作名
    interlock: InterlockConfig      # Task 4
    contracts: ContractFields       # Task 5
    funds: FundsConfig              # Task 5
    stream: StreamConfig            # Task 6
    orders: OrdersConfig            # Task 7
    events: EventFields             # Task 8
    trades: TradeFields             # Task 8
    history: HistoryConfig          # Task 9
    scrub: ScrubConfig              # Task 10

class ConfigError(ValueError): ...
def load_config(path: Path) -> P1Config: ...
def read_secrets(cfg: P1Config) -> dict[str, str]: ...
def new_run(results_root: Path, subcommand: str, now: datetime, tag: str | None) -> Path: ...
def setup_logging(run_dir: Path, secrets: Iterable[str], level: str) -> None: ...
```

子設定在標註的任務中定義；本任務先建立最小型別讓載入通過。

**Tests:**

- `test_missing_key_names_full_path` — 刪掉 `endpoints.place_stock.path` 後載入，錯誤訊息含完整鍵名。
- `test_inline_secret_rejected` — TOML 含 `api_key = "x"` 時拒絕載入。
- `test_missing_secret_env` — 環境變數不存在時錯誤訊息含變數名。
- `test_new_run_refuses_existing_dir` — 同一時間戳與 tag 第二次建立失敗；不同 tag 可並存。
- `test_log_redacts_secret_and_prefix` — log 含完整 key 與其前 10 字元時，檔案與 stderr 都只看得到 `***`。

**Numbers:**

- API key 前綴 10 字元：1.3.2 的 clientname 為 `PYAPI/{api_key[:10]}/…`（設計 §12 的逆向結果）；1.7.6 若沿用，前綴會出現在回應中。
- `http_timeout_s = 35`：官方 server 預設 `SJ_HTTP_TIMEOUT` 30 秒（CLI `--help`），client 須較長才看得到 server 自己的逾時回應。

**Verification:**

Run: `uv run pytest -q tests/test_config.py tests/test_rundir.py tests/test_redact.py`（於 `tools/p1-official-server/`）
Expected: 全部 passed。

**Commit:**

```bash
git add .gitignore tools/p1-official-server
git commit -m "Add p1rec recorder skeleton with config and redacting logs"
```

---

### Task 3：REST 與 SSE 原樣錄製

**Files:**
- Create: `src/p1rec/capture.py`、`src/p1rec/sse.py`
- Test: `tests/test_capture.py`、`tests/test_sse.py`

**Behaviour:**

- 每個 REST 請求在 `rest.jsonl` 寫一行：`client`、`op`、`method`、`url`、`req_body`、`sent_ts_ns`、`recv_ts_ns`、`status`、`resp_headers`，以及 `resp_body`（UTF-8）或 `resp_body_b64`（無法解碼時）。錯誤狀態碼照記並回傳，不拋例外；連線錯誤記一行 `error` 後拋出。
- SSE 以收到的位元組切分 frame（`\n\n` 與 `\r\n\r\n` 皆可），每個完整 frame 寫一行 `kind="frame"`，含 `raw` 與 `recv_ts_ns`；註解 frame 同樣記錄。
- 連上寫 `kind="connected"`（含狀態碼），斷線寫 `kind="disconnected"`（含錯誤），隔 `retry_s` 重連；曾收到非空 `id` 時，重連帶最近一次的 `id` 作為 `Last-Event-ID`（依 SSE 規範，沒有 `id` 的 frame 不會清掉它）（官方 1.7.6 的 frame 不帶 `id:`，此路徑只在量到 `id` 時生效）；停止時寫 `kind="stopped"`。每次連上後呼叫 `on_connected`。
- `parse_frame` 只供分析，不改動已寫入的 `raw`。

**Interface:**

```python
@dataclass(frozen=True)
class CapturedResponse:
    status: int
    body: bytes
    recv_ts_ns: int
    def json(self) -> object: ...

class CapturingClient:
    def __init__(self, base_url: str, client_name: str, rest_log: Path, timeout_s: float) -> None: ...
    def request(self, op: str, endpoint: Endpoint, *, json_body: object | None = None,
                params: dict[str, str] | None = None,
                path_vars: dict[str, str] | None = None) -> CapturedResponse: ...

@dataclass(frozen=True)
class SseFrame:
    raw: str
    event: str | None
    id: str | None
    data: str | None
    is_comment: bool

def split_frames(buffer: str) -> tuple[list[str], str]: ...  # (完整 frame, 剩餘)
def parse_frame(raw: str) -> SseFrame: ...

class SseRecorder(threading.Thread):
    def __init__(self, base_url: str, client_name: str, stream_name: str, connect: Endpoint,
                 out: Path, retry_s: float, stop: threading.Event,
                 on_connected: Callable[[], None]) -> None: ...
    last_event_id: str | None
```

**Tests:**

- `test_split_frames_lf_and_crlf` — 混合兩種分隔的緩衝切出正確 frame 與剩餘。
- `test_split_frames_partial_tail` — 未完成的 frame 留在剩餘、不寫出。
- `test_parse_multiline_data` — 多行 `data:` 以 `\n` 串接。
- `test_comment_frame_recorded` — `: ping` 被記錄且 `is_comment=True`。
- `test_heartbeat_event_recorded` — `event:heartbeat` 的 frame 原樣記錄，`parse_frame` 取出事件名稱 `heartbeat`。
- `test_reconnect_sends_last_event_id` — `httpx.MockTransport` 模擬斷線，第二次連線標頭含上一個 `id`，且 `on_connected` 被呼叫兩次。
- `test_rest_error_status_recorded_not_raised` — 500 寫入 `rest.jsonl` 並回傳。
- `test_non_utf8_body_base64` — 非 UTF-8 回應寫入 `resp_body_b64`。

**Numbers:**

- `retry_s = 1.0`：錄製器策略；server 在本機，重試成本低，錄製缺口只比實際斷線多約 1 秒。

**Verification:**

Run: `uv run pytest -q tests/test_capture.py tests/test_sse.py`
Expected: 全部 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Record REST exchanges and SSE frames verbatim"
```

---

### Task 4：模擬模式連鎖檢查

**Files:**
- Create: `src/p1rec/interlock.py`
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`
- Test: `tests/test_interlock.py`

**Behaviour:**

- `assert_simulation` 查詢 `mode`，只有 `interlock.pointer` 指到的值恰為 JSON `true` 時放行。
- 非 2xx、非 JSON、指標不存在、值為 `false`、值非布林，一律拋 `InterlockError`、記 ERROR，子命令以 3 結束。
- 所有送出委託、改單、刪單的程式路徑，每次呼叫前都要經過 `assert_simulation`；其他會連 server 的子命令在啟動時檢查一次。
- `self-test-interlock` 不連網：對內建五種假回應（Production、缺欄位、500、非 JSON、Simulation）逐一執行，前四種中止、第五種放行時回 0，否則回 3。

**Interface:**

```python
@dataclass(frozen=True)
class InterlockConfig:
    endpoint_op: str  # 預設 "mode"
    pointer: str      # JSON pointer，取自 Task 1

class InterlockError(RuntimeError): ...
def assert_simulation(client: CapturingClient, cfg: P1Config) -> None: ...
def self_test(interlock: InterlockConfig) -> int: ...
```

**Tests:**

- `test_true_passes` — 值為 `true` 時不拋出。
- `test_false_aborts`、`test_missing_pointer_aborts`、`test_non_bool_aborts`、`test_http_500_aborts`、`test_non_json_aborts` — 各拋 `InterlockError`。
- `test_self_test_returns_zero` — `self_test()` 回 0。
- `test_cli_exit_code_3` — 中止時 CLI 以 3 結束。

**Verification:**

Run: `uv run pytest -q tests/test_interlock.py && uv run p1rec self-test-interlock; echo $?`
Expected: 測試全過，最後印出 `0`。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Abort unless the official server reports simulation mode"
```

---

### Task 5：劇本模型、價格參照與第 0 步 preflight

**Files:**
- Create: `src/p1rec/scenarios.py`、`src/p1rec/prices.py`、`src/p1rec/broker.py`（與 Task 7 共用的查詢）、`src/p1rec/preflight.py`（編排與報告）
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`、`src/p1rec/interlock.py`（自我測試的設定）、`src/p1rec/redact.py`（執行期遮蔽值）、`p1.toml`
- Test: `tests/test_scenarios.py`、`tests/test_prices.py`、`tests/test_broker.py`、`tests/test_preflight.py`、`tests/test_redact.py`；`tests/mock_official.py` 為共用的程序內假 server

**Behaviour:**

- `load_scenarios` 讀 `scenarios.toml` 的 `[[case]]` 表；`[[case]]` 以外的頂層鍵、案例 id 重複、`steps` 含未知動詞、`steps` 不以唯一的 `place` 開頭、`cancel` 之後還有步驟、`update_qty` 累計減量超過案例數量、`expect_fill` 案例用了 `exceed_funds` 時拋 `ConfigError`。
- 價格參照 `limit_down`、`limit_up`、`reference` 取自合約回應；`limit_down_plus_tick` 為跌停價加一個升降單位；`far_below` 在股票與零股為跌停價，在期貨為「參考價 × 0.95 向下取到升降單位」（合約檔的漲跌停只適用期交所日盤，夜盤依當日結算另計，屬推論，見契約文件 §2；期貨劇本單只在日盤下）；`far_below_plus_tick` 為 `far_below` 加一個升降單位；升降單位依市場、價格區間與 `etf_codes` 查表。
- `preflight`：連鎖檢查；查 `accounts`、股票資金來源、`margin`、`usage`、`ca_status`；查劇本案例用到的全部商品的合約（股票與零股逐檔查 `contracts_stock`；期貨依商品根代號各查一次 `contracts_futures`，取 `code` 相符的唯一一筆）；計算資金需求並比對可用額度。只有劇本交易的市場才需要對應帳戶與可用額度：純股票劇本不查 `F` 帳戶與 `margin`。
- 股票可用額度取自 `funds.stock_available_op` 與 `funds.stock_available_pointer`（`p1.toml` 為 `trading_limits` 的 `/trading_available`：推論券商以交易額度而非交割帳戶餘額檢查委託，`calibrate:` Task 12 以模擬帳戶的實際回應確認）；`funds.stock_report_only` 所列的其他來源（`balance` 的 `/acc_balance`）照樣查詢、落地並寫入報告，但不參與比對，失敗只記 WARN。這些操作須為 POST 端點，載入設定時檢查。
- 資金查詢的請求本體取自 `accounts` 回應中第一個 `S`、`F` 帳戶的 `account_type`、`broker_id`、`account_id`、`person_id`；`ca_status` 的 `person_id` 同樣取自該回應，帳戶識別不進 `p1.toml`；`accounts` 查詢失敗時不查 `ca_status`。
- `fetch_accounts` 本身在使用回應前，把每個帳戶的 `account_id`、`person_id`、`username` 以 `add_redaction_values` 加入 log 遮蔽，所有呼叫者（含 Task 7）自動適用；`setup_logging` 把 `httpx`、`httpcore` 降為 WARNING，請求 URL（含 `ca_status` 的 `person_id`）只留在 `rest.jsonl`。
- 2xx 的資金回應帶非空錯誤訊息（`AccountBalance.errmsg`；`Margin`、`TradingLimits` 沒有錯誤欄位）時視為查詢失敗，不當成資金不足；錯誤訊息只留在 `rest.jsonl`。
- 合約限價須 `0 < 跌停 < 參考價 < 漲停`，且漲跌停皆落在所屬市場的升降單位格點上；期貨紀錄帶 `tick` 時須等於 1 點；任一不符即記 ERROR、以 2 結束。
- 資金需求以單一部位計（設計 §4；案例逐筆執行，掛著的單在下一筆開始前已刪單）：計入除 `exceed_funds` 以外的每個買進案例、會成交的案例與期貨賣出案例（期貨委託不分買賣都要保證金；股票賣出圈存的是股票而非現金）。股票為 `max(案例的 張數 × 1000 或零股股數 × 圈存價)`，圈存價在會成交或市價類（MKT、MKP）案例為漲停價，其餘為下單與各 `update_price` 步驟價格參照的最高價；股票需求再加券商手續費：`× (1 + 0.001425)` 後無條件進位到元。期貨為 `max(案例口數 × 該契約代號的原始保證金)`。計入的期貨案例在 `futures_margin_twd` 缺該代號時記 ERROR、以 2 結束。
- 連鎖檢查通過後，任一查詢拿不到回應（連線被拒、逾時）時記 ERROR、該值記為未知、其餘查詢照跑，最後以 2 結束。
- 不足、或找不到可用額度欄位時記 ERROR、以 2 結束；不論成敗（含連鎖檢查中止）都寫 `preflight.json`，金額以十進位字串表示，鍵為 `funds_ok`、`required`、`available`（`stock_twd`、`futures_margin_twd` 與 `stock_sources`：各股票資金來源 → 金額，比對用的在前）、`connections`、`accounts`（帳戶類型 → `signed`）、`limits`（商品 → 漲停、跌停、參考價）。
- 劇本含某市場的案例、而對應帳戶（股票與零股為 `S`，期貨為 `F`）的 `signed` 不是 `true` 時記 WARN，不中止；該市場的拒單照樣錄製。
- 所有查詢的原始回應經 `CapturingClient` 落地，供 Task 15 挑選 fixture。

**Interface:**

```python
class Market(StrEnum): STOCK = "stock"; ODD = "odd"; FUTURES = "futures"
class PriceRef(StrEnum): LIMIT_DOWN = "limit_down"; LIMIT_UP = "limit_up"; REFERENCE = "reference"; LIMIT_DOWN_PLUS_TICK = "limit_down_plus_tick"; FAR_BELOW = "far_below"; FAR_BELOW_PLUS_TICK = "far_below_plus_tick"

@dataclass(frozen=True)
class Case:
    id: str
    market: Market
    code: str
    action: Literal["Buy", "Sell"]
    price_ref: PriceRef
    price_type: Literal["LMT", "MKT", "MKP"]
    order_type: Literal["ROD", "IOC", "FOK"]
    quantity: int | Literal["exceed_funds"]
    custom_field: str | None
    expect_fill: bool
    steps: tuple[str, ...]  # "place" | "update_price:<PriceRef>" | "update_qty:<減少量>" | "cancel"

@dataclass(frozen=True)
class Step:
    verb: Literal["place", "update_price", "update_qty", "cancel"]
    price_ref: PriceRef | None = None
    reduce_by: int | None = None

@dataclass(frozen=True)
class ContractLimits:
    code: str
    limit_up: Decimal
    limit_down: Decimal
    reference: Decimal

@dataclass(frozen=True)
class ContractFields:  # 皆為 JSON 指標，取自 Task 1
    code: str
    limit_up: str
    limit_down: str
    reference: str
    futures_tick: str
    etf_codes: tuple[str, ...]

@dataclass(frozen=True)
class FundsConfig:
    stock_available_op: str           # 比對用的股票資金查詢（POST）
    stock_available_pointer: str
    stock_report_only: dict[str, str]  # 其他股票資金查詢：操作 -> 指標，只報告
    futures_available_pointer: str
    futures_margin_twd: dict[str, Decimal]  # 契約代號 -> 每口原始保證金

@dataclass(frozen=True)
class FundsRequirement:
    stock_twd: Decimal
    futures_margin_twd: Decimal

def load_scenarios(path: Path) -> list[Case]: ...
def parse_step(raw: str) -> Step: ...
def format_decimal(value: Decimal) -> str: ...  # 一律定點表示，不出現 1E+3
def tick_size(market: Market, price: Decimal, *, is_etf: bool) -> Decimal: ...
def check_limits(limits: ContractLimits, market: Market, *, is_etf: bool) -> None: ...  # 不符拋 ValueError
def resolve_price(ref: PriceRef, limits: ContractLimits, market: Market, *, is_etf: bool) -> Decimal: ...
def required_funds(cases: Sequence[Case], limits: Mapping[str, ContractLimits],
                   margins_twd: Mapping[str, Decimal], *, etf_codes: Collection[str]) -> FundsRequirement: ...
def add_redaction_values(values: Iterable[str]) -> None: ...  # redact.py
def run_preflight(cfg: P1Config, scenarios: Path, run_dir: Path, *,
                  transport: httpx.BaseTransport | None = None) -> int: ...

# broker.py：與 Task 7 共用；回應欄位不符拋 BrokerError，拿不到回應時照原樣拋 httpx 例外
class BrokerError(RuntimeError): ...
ACCOUNT_TYPE_FOR_MARKET: Mapping[Market, str]
def parse_json_exact(op: str, response: CapturedResponse) -> object: ...  # 非整數一律 Decimal
def fetch_accounts(client: CapturingClient, cfg: P1Config) -> list[dict[str, object]]: ...
def account_for_market(accounts: Sequence[Mapping[str, object]], market: Market) -> Mapping[str, object]: ...
def account_request(account: Mapping[str, object]) -> dict[str, object]: ...
def fetch_funds_value(client: CapturingClient, cfg: P1Config, op: str, pointer: str,
                      account: Mapping[str, object]) -> Decimal: ...
def fetch_available_twd(client: CapturingClient, cfg: P1Config, market: Market,
                        account: Mapping[str, object]) -> Decimal: ...
def fetch_limits(client: CapturingClient, cfg: P1Config, cases: Sequence[Case]) -> dict[str, ContractLimits]: ...
```

**Tests:**

- `test_duplicate_case_id_rejected`、`test_unknown_step_rejected` — 各拋 `ConfigError`。
- `test_stock_tick_bands` — 9.99→0.01、10→0.05、50→0.1、100→0.5、500→1、1000→5。
- `test_etf_tick_bands` — 49.99→0.01、50→0.05。
- `test_futures_tick_is_one_point` — TMF 任意價格回 1。
- `test_limit_down_plus_tick` — 跌停 9.95 的一般股回 9.96。
- `test_far_below_futures` — TMF 參考價 47781 時回 45391（47781 × 0.95 = 45391.95 向下取到 1 點）；一般股回跌停價。
- `test_required_funds_single_position` — 兩筆會成交的股票案例取較大者，不加總。
- `test_required_funds_resting_order_dominates_fill` — 2 張遠價掛單的圈存（2 × 1000 × 跌停價）大於 1 張成交單時取前者。
- `test_insufficient_exits_2_and_writes_json` — 回 2，`preflight.json` 存在且 `funds_ok` 為 `false`。
- `test_missing_available_field_exits_2` — 找不到可用額度欄位時回 2。
- `test_missing_futures_margin_exits_2` — 會成交的期貨案例缺原始保證金時回 2，ERROR 含該代號。
- `test_unsigned_futures_account_warns` — `F` 帳戶 `signed: false` 且劇本含期貨案例時記 WARN、結束碼仍為 0。
- `test_refused_connection_mid_preflight_exits_2` — `margin` 連線被拒時記 ERROR、其餘查詢照跑、回 2。
- `test_stock_only_scenarios_need_no_futures_account` — 純股票劇本在沒有 `F` 帳戶時回 0，不查 `margin`、沒有 ERROR。
- `test_errmsg_on_compared_balance_is_failed_query_not_shortfall` — 帶 `errmsg` 的 2xx 回應記為查詢失敗，不記資金不足。
- `test_malformed_stock_limits_fail_closed`、`test_futures_tick_mismatch_fails_closed` — 限價順序、格點或期貨 `tick` 不符時拋 `BrokerError`。
- `test_continuous_month_alias_resolves_its_root` — `TMFR1` 與 `TMFJ6` 同屬根代號 `TMF`，只查一次。
- `test_malformed_scenarios_writes_json_and_raises` — 劇本檔有誤時仍寫出 `preflight.json`。
- `test_httpx_request_url_not_logged`、`test_added_redaction_value_is_redacted` — `recorder.log` 與 stderr 都看不到請求 URL 與執行期加入的值。

**Numbers:**

- 1000 股／張：證交所整股交易單位。
- 零股案例的數量即股數：證交所盤中零股交易以 1 股為交易單位。
- 期貨根代號為契約代號去掉末兩碼交割月份代碼（月份字母加年份個位數，或近月、次月的 `R1`、`R2`）：2026-09-29 期貨 API 測試時 `contracts_futures?root=TMF` 回傳的 8 個代號皆為此形式（本機 `results/p1/*-api-test/contracts-tmf.json`）。
- 股票升降單位（<10：0.01；10–<50：0.05；50–<100：0.1；100–<500：0.5；500–<1000：1；≥1000：5）與 ETF（<50：0.01；≥50：0.05）：證交所升降單位規定。
- 期貨升降單位 1 點：期交所 TXF／MXF／TMF 契約規格。
- 期貨遠價 0.95：期貨 API 測試（契約文件 §2）中以跌停價下單被拒、以參考價 × 0.95 下單被接受。
- 股票買進手續費率 0.001425：證交所規定的券商手續費上限。
- `futures_margin_twd`：`calibrate:` Task 12 執行 preflight 前，依期交所當日公告的原始保證金填入。

**Verification:**

Run: `uv run pytest -q tests/test_scenarios.py tests/test_prices.py tests/test_broker.py tests/test_preflight.py tests/test_redact.py`
Expected: 全部 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Add scenario model, price references and funds preflight"
```

---

### Task 6：多 client 串流與排程動作

**Files:**
- Create: `src/p1rec/stream.py`
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`
- Test: `tests/test_stream.py`

**Behaviour:**

- `p1rec stream --clients a,b --until <ISO 8601 含時區>`：連鎖檢查後，每個 client 對 `streams` 中的每條端點（例如綜合行情、`order_event`、`contract_event`）各開一個 `SseRecorder`，輸出 `sse_<client>_<串流名>.jsonl`。
- 訂閱是 server 全域（契約文件 §1）：所有 recorder 首次連上後，依序送出 `startup_requests`（例如每個帳戶的 `subscribe_trade`），再對 `instruments` 的每個商品與報價類型送出一次訂閱（本體由 `subscribe_body` 代入 `{code}`、`{exchange}`、`{security_type}`、`{quote_type}`、`{intraday_odd}`）。
- 首次送出時任一 `startup_requests` 失敗或被跳過（含 `accounts` 查詢失敗；`subscribe_trade` 回應的 `subscribe_trade` 不是 `true` 也算失敗），記 ERROR、不送訂閱，並自行停止：所有 client 照常寫出 `stopped` 後以 2 結束，由操作者排除原因後重新啟動。
- 之後只有「主 recorder」（第一個 client 的第一條串流）重連時，才重送 `startup_requests` 與目前有效的全部訂閱，並記一次 INFO；其他 recorder 重連不重送。每天台北時間 `daily_resend_at`（08:35，契約文件 §2 的每日維護窗口之後；維護可能丟掉訂閱而不中斷任何串流）也做一次同樣的完整重送。`calibrate:` 維護實際持續多久尚未量測，Task 12／14 由 server log 確認維護結束時間，晚於 08:35 就往後調。
- 首次送出之後，失敗的 `startup_requests` 與訂閱（非 2xx，或 `success`／`subscribe_trade` 不是 `true`）記下來並繼續其餘請求，每 `request_retry_s`（60 秒：確保兩輪重試不會落在舊 gateway `shioaji-server` 記載的帳務查詢上限「每 5 秒 25 次」的同一個 5 秒窗口；訂閱是否計入任何券商速率上限未知，見設計 §6 F；一次完整重送目前約 22 個請求，加上 TXO 一對為 26 個，不受此間隔限制）以及任一 recorder 下一次連上時重試，直到成功。同一個請求第一次失敗記 ERROR，之後連續失敗記 DEBUG 並附次數，恢復時記 INFO。
- 控制執行緒本身發生未預期的錯誤時記 ERROR 並自行停止，以 2 結束，而不是在沒有重送與重試的情況下繼續錄製。
- 所有 REST 請求都由一條控制執行緒送出；recorder 在 `on_connected` 只通知它，因此重送再慢也不耽誤主 recorder 讀取 frame。
- `[[stream.action]]` 在指定時間送出 `subscribe` 或 `unsubscribe` 並更新有效訂閱集合；啟動時已過期的動作記 WARN 並跳過。
- 到 `--until`（啟動時換算成 `time.monotonic()` 的期限，牆上時鐘跳動不影響停止時間）或收到 SIGINT／SIGTERM 時，所有 client 寫出 `stopped` 後以 0 結束。SIGINT／SIGTERM 在所有執行緒中被擋下、由主執行緒以 `sigtimedwait` 接收，因此 `run_stream` 開始擋訊號前若已有其他執行緒在跑，拋出 `RuntimeError`。對每個 recorder 的 join 最多等 `sse.SSE_READ_TIMEOUT_S + retry_s`（讀取逾時為三個心跳間隔，契約文件 §2；recorder 的等待從它發出連線起算，stop 可能晚於連線發出，因此加一個 `retry_s` 作為餘裕）；逾時仍未結束的 recorder（例如卡在等待 response header）記 WARN 並列出名稱，結束碼仍為 0。

**Interface:**

```python
@dataclass(frozen=True)
class Instrument:
    code: str
    exchange: str        # TSE | OTC | TAIFEX
    security_type: str   # STK | FUT | OPT | IND
    quote_types: tuple[str, ...]  # Tick | BidAsk | Quote
    intraday_odd: bool

@dataclass(frozen=True)
class StartupRequest:  # __post_init__ 要求 body 與 account_type 恰有一個有值
    op: str                    # [endpoints] 中的邏輯操作
    body: str | None           # JSON 本體；account_type 有值時為 None
    account_type: str | None   # S | F | H，由 accounts 回應組成該帳戶的 AccountRequest

@dataclass(frozen=True)
class StreamAction:
    at: datetime
    op: Literal["subscribe", "unsubscribe"]
    code: str
    quote_type: str

@dataclass(frozen=True)
class StreamConfig:  # 預設值代表 p1.toml 沒有 [stream]；有 [stream] 時 subscribe 與 unsubscribe 必有值
    streams: dict[str, Endpoint] = field(default_factory=dict)  # 串流名 -> SSE 端點
    subscribe: Endpoint | None = None   # 取自 endpoints.stream_subscribe
    unsubscribe: Endpoint | None = None  # 取自 endpoints.stream_unsubscribe
    subscribe_body: str = ""            # JSON 範本
    unsubscribe_body: str = ""
    startup_requests: tuple[StartupRequest, ...] = ()
    retry_s: float = 0.0
    request_retry_s: float = 0.0         # 首次送出後失敗請求的重試間隔
    daily_resend_at: time | None = None  # 台北時間，每日完整重送
    instruments: tuple[Instrument, ...] = ()
    actions: tuple[StreamAction, ...] = ()

def run_stream(cfg: P1Config, clients: list[str], until: datetime, run_dir: Path) -> int: ...
```

`p1.toml` 中 `startup_requests` 寫成 `[邏輯操作, 本體]`，載入時解析成 `StartupRequest`；本體寫成 `"@account:S"`（`F`、`H` 同理）時，代表以 `accounts` 查到的該類型帳戶組成的 `AccountRequest`（`account_type`、`broker_id`、`account_id`、`person_id`）；repo 是公開的，帳號因此不寫進 `p1.toml`：任何設定的請求本體在任何深度出現 `account_id`、`person_id`、`username` 都在載入時拒絕，`[stream]` 及其子表的未知鍵也一樣。帳戶清單查到後沿用；查詢失敗不快取，同一輪送出只查一次。

**Tests:**

- `test_one_recorder_per_client_and_stream` — 兩個 client、三條串流時開出六個檔案。
- `test_startup_then_subscriptions_sent_once` — 全部連上後 `startup_requests` 先送、每個訂閱只送一次，不隨 client 數倍增。
- `test_leader_reconnect_resends` — 主 recorder 重連時重送全部有效訂閱；其他 recorder 重連時不送。
- `test_scheduled_unsubscribe_updates_active_set` — 動作執行後，主 recorder 再重連時不再訂閱該商品。
- `test_past_action_skipped_with_warning` — 過期動作不執行並記 WARN。
- `test_until_stops_all_clients` — 到時間後每個檔案最後一行皆為 `stopped`。

**Verification:**

Run: `uv run pytest -q tests/test_stream.py`
Expected: 全部 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Stream quotes to several recorded SSE clients"
```

---

### Task 7：下單劇本執行

**Files:**
- Create: `src/p1rec/orders.py`、`tools/p1-official-server/scenarios.toml`
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`、`src/p1rec/interlock.py`（自我測試的設定）、`p1.toml`
- Test: `tests/test_orders.py`；`tests/helpers.py` 的最小設定加上 `[orders]`

**Behaviour:**

- `p1rec orders [--only <id,...>] [--base-url <url>]` 依 `scenarios.toml` 順序逐筆執行，前一筆（含其刪單或平倉）結束才開始下一筆；`--base-url` 覆蓋 `base_url`；`--only` 含未知 id 時在送出任何請求前拋 `ConfigError`。
- 載入時檢查（確保不留部位的前提）：不預期成交的案例須為 LMT Buy，下單與每個 `update_price` 的價格參照皆為 `far_below`、`far_below_plus_tick`、`limit_down`、`limit_down_plus_tick` 之一；其中 ROD 案例須以 `cancel` 結束（否則掛單會跨進下一筆，而資金 preflight 以單一案例計）；`expect_fill` 案例不得 `update_qty`（平倉送出的是原下單量）；`exceed_funds` 只能用於股票且 `price_ref = "limit_down"`。
- 期貨案例只在期交所日盤（08:45–13:45，台北時間）執行：合約檔的漲跌停價只對日盤有效，夜盤的漲跌幅以當日一般交易時段結算價為基準（推論自期貨 API 測試，契約文件 §2）。案例須在日盤內開始，且最長執行時間（每步 `step_wait_s`，再加每次等待委託結束的 `fill_timeout_s + fill_poll_s`：會成交的案例兩次、其餘一次）在 13:45 前結束（`calibrate:` Task 12 查證期交所日盤收盤前是否有集合競價，有則比照證交所提前）；否則記 ERROR、該案例不送任何請求（連模式查詢都不送），其餘案例照跑，結束碼為 2。
- 會成交的股票與零股案例依同樣的算法，須在證交所連續交易時段（09:00–13:25，台北時間；13:25–13:30 為收盤集合競價）內開始並結束，開倉與平倉都在連續交易中成交；否則同樣拒絕。準備查詢也要時間，因此查完帳戶與合約後、送出第一個異動請求前再檢查一次。
- 每個 `place`、`update_price`、`update_qty`、`cancel`、`close` 呼叫前，以同一個 client 執行連鎖檢查；中止時不再送任何請求、`InterlockError` 往上拋（CLI 以 3 結束）；已送出委託的案例另記 CRITICAL（「possible residual position, check manually」）。
- 每筆案例下單前查 `accounts`（同時把帳戶識別加入 log 遮蔽）與該商品的合約；步驟之間不再查詢，步驟間隔只有 `step_wait_s`。`account` 欄位帶 `accounts` 回傳的完整帳戶物件：股票與零股用 `S`、期貨用 `F`（只帶帳號時 `account_type` 預設為 `S`，契約文件 §2）。
- 請求本體由 `body_templates` 以結構代入產生：範本是 JSON，字串值恰為 `{name}` 者換成型別值（價格、數量為 JSON 數字，`account` 為帳戶物件，不適用或未設定的 `order_lot`、`octype`、`custom_field` 為 null）。佔位名稱：`code`、`exchange`、`security_type`、`action`、`price`、`quantity`、`price_type`、`order_type`、`order_lot`、`octype`、`custom_field`、`account`、`trade_id`。載入時檢查每個範本的佔位（下單範本須帶齊案例會變動的欄位與 `account`），任何深度出現 `account_id`、`person_id`、`username` 鍵即拒絕載入。股票與零股用 `place_stock`（`order_lot` 分別為 `Common`、`IntradayOdd`，零股數量即股數），期貨用 `place_futures`（`octype = "Auto"`，期貨 API 測試送出並被接受）。
- 價格由 `resolve_price` 算出；`quantity = "exceed_funds"` 時張數為「股票資金來源中的最大值 ÷（1000 × 跌停價）」無條件進位再加 1，最大值取比對用的來源與每個 `stock_report_only` 來源，確保超過模擬器實際檢查的那一個；任一來源查詢失敗即視為無法準備。
- 步驟之後（含最後一步）等待 `step_wait_s`。`update_qty` 送出減少量（推論自 shioaji `update_order` 的 `qty`；2 單位減 1 時等於剩餘量，兩種解讀下生命週期案例皆成立）。
- 被拒的請求（4xx，或 2xx 但狀態為 `Failed`）照樣記錄，拒單本身是語料：被拒的 `place` 略過該案例其餘步驟並繼續下一個案例；被拒的 `update_*` 照跑後續步驟（讓 `cancel` 仍送出）；被拒的 `cancel` 照樣進入下述的結束確認。
- 被接受的非 `expect_fill` 案例在最後一步之後，同樣每 `fill_poll_s` 查 `trades` 等到委託結束（至多 `fill_timeout_s`）：無成交地結束則照常；有任何成交量（含已結束或尚未結束的部分成交）視為可能殘留部位，記 CRITICAL、略過後續 `expect_fill` 案例、結束碼為 2；逾時仍未結束則記 ERROR（委託可能仍掛著），結束碼為 2。
- `expect_fill` 案例：每 `fill_poll_s` 查 `trades`（`AccountRequest` + `refresh: true`）直到該委託成交、無成交地結束或超過 `fill_timeout_s`。交易所拒單時下單回應仍為 200，`Failed` 只出現在之後的 `trades`（契約文件 §2）；`Failed` 的成交量為 null 或 0 時視為無成交，`Cancelled` 只有成交量為 0 時才視為無成交，null 視為可能已成交。成交後送出反向、以跌停（平多）或漲停（平空）價構成的可成交 LMT ROD 單 `close`，同樣等到成交。逾時、部分或可能成交後結束、平倉被拒或未成交時記 CRITICAL（含案例 id 與「possible residual position, check manually」），不送其他平倉單，之後的 `expect_fill` 案例一律略過，以 2 結束。
- 異動請求回 5xx（或 4xx 以外的任何非 2xx）或拿不到回應（連線中斷、逾時）時，server 可能已處理該請求：記 CRITICAL（含案例 id 與「possible residual position, check manually」），停止整個劇本，以 1 結束。同步拒單只以 4xx 回應（契約文件 §2）。
- 已送出委託後被其他例外中斷（例如等待成交時的 KeyboardInterrupt）時，記 CRITICAL、在 `orders.json` 寫入該案例的 `trade_id` 與 `interrupted`，再把例外往上拋。解析回應本體時的任何例外（例如過深巢狀引發的 RecursionError）都當成無法解析，不中斷劇本。
- 每則關於可能殘留部位或掛單的 CRITICAL／ERROR 都寫出該案例的 `trade_id` 與 `close_trade_id`，以及處置方式（「check trades for <id>; cancel or close it in the simulator before rerunning」）；劇本結束時記一行 INFO，列出結束碼、各結果的件數、需要處理的案例 id（`residual_position`、`order_may_rest`、`unknown_outcome`、`interrupted`，以及送單後被連鎖檢查中止者）與 `orders.json` 的路徑。
- 無法準備的案例（帳戶、合約或資金查詢失敗）記 ERROR、不送任何請求，結束碼為 2。
- `orders.json` 在每個案例之後與劇本結束時原子改寫：暫存檔寫完先 fsync，再 `os.replace`，之後 fsync 所在目錄（執行主機曾遇硬斷電）；中止或中斷時照樣寫，寫入失敗只記 ERROR，不蓋掉原本的例外。內容為 `base_url`、實際使用的 `[orders]` 時間設定（`timings`），以及每個案例的 `id`、`trade_id`、`close_trade_id`、`steps`（每步的 `step`、`op`、HTTP `status`、`accepted`）、`outcome` 與 `reason`，鍵序固定，不含帳戶識別。
- `[orders]` 與其子表出現未知鍵、`orders.exchange` 的值不在 openapi 的 `Exchange` 列舉內時，拒絕載入。委託識別、狀態與成交量的指標由 `[trades]` 提供（`TradeFields`，見 Task 8），狀態比較前以 `order_status.normalize_status` 正規化，與 reconcile 相同。
- 結束碼：全部案例跑完（含拒單）為 0；有部位或掛單可能殘留、案例在交易時段外被拒、案例無法準備時為 2；連鎖檢查中止為 3；異動請求結果未知（5xx 或無回應）為 1。

**Interface:**

```python
@dataclass(frozen=True)
class OrdersConfig:
    body_templates: dict[str, str]  # place_stock、place_futures、update_price、update_qty、cancel -> JSON 範本
    exchange: dict[str, str]        # 市場（stock、odd、futures）-> ContractRequest.exchange
    step_wait_s: float
    fill_poll_s: float
    fill_timeout_s: float           # 須大於 fill_poll_s

def run_orders(cfg: P1Config, cases: Sequence[Case], run_dir: Path, only: set[str] | None, *,
               transport: httpx.BaseTransport | None = None, clock: Clock | None = None) -> int: ...
def exceed_funds_lots(max_available_twd: Decimal, limit_down: Decimal) -> int: ...
@dataclass(frozen=True)
class TradingSession:
    name: str
    open: time   # 台北時間
    close: time

def session_for(case: Case) -> TradingSession | None: ...  # 期貨：期交所日盤；會成交的股票與零股：證交所連續交易
def fits_session(start: datetime, budget_s: float, session: TradingSession) -> bool: ...
def json_number(value: Decimal) -> int | float: ...  # 一律定點表示，無法精確或須用指數表示時拋 ValueError
```

`scenarios.toml` 內容（設計 §4、§6 C）：

| 群組 | 商品 | 案例 |
|---|---|---|
| 遠價 LMT | 2890、00981A（零股）、TMF | LMT × ROD／IOC／FOK，`far_below`，不成交；ROD 之後 `cancel` |
| 生命週期 | 2890、TMF | 2 單位、`far_below`；`update_price:far_below_plus_tick` → `update_qty:1` → `cancel` |
| 市價類 | 2890（整股 1 張）、00981A（零股 1 股）、TMF（1 口） | MKT、MKP × ROD／IOC／FOK，`limit_up`，`expect_fill = true`，逐筆開倉即平倉；不合法組合由 server 拒單，照錄 |
| 資金不足 | 2890 | `exceed_funds`、`limit_down`、LMT ROD，之後 `cancel` |
| 委託標記 | 2890 | 遠價 LMT ROD；`custom_field` 分別為 6 字元 base62、7 字元 base62、ADR-0004 候選格式（1 字元 node + 5 字元雜湊）；之後 `cancel` |
| harness 探測 | 2890 | id `harness_probe`，遠價 LMT ROD，之後 `cancel` |

TMF 用 `TMFJ6`（`calibrate:` Task 12 前一個交易日換成近月代號）。

**Tests:**

- `test_interlock_before_every_mutation` — 假 server 第二次查詢模式時回 Production，劇本在第二個請求前中止並拋 `InterlockError`，已送單的案例記 CRITICAL；`test_cli_interlock_abort_exits_3` — CLI 以 3 結束。
- `test_every_mutation_follows_its_own_mode_query` — 模式查詢數等於異動請求數，且每個異動請求緊接在一次模式查詢之後。
- `test_exceed_funds_quantity` — 餘額 100000、跌停 20 時張數為 6；`test_exceed_funds_sized_from_largest_funds_source` — 取各股票資金來源的最大值。
- `test_fill_then_close_sequence` — 成交後送出反向平倉；下一個 `expect_fill` 案例在平倉成交後才開始。
- `test_fill_timeout_logs_critical_and_exits_2` — 逾時時不送平倉、記 CRITICAL、略過後續 `expect_fill` 案例、回 2。
- `test_order_ending_unfilled_needs_no_close` — `trades` 顯示 `Failed` 時不平倉、不記 CRITICAL，下一個案例照常成交平倉；`test_ended_order_is_flat_only_on_evidence` — `Failed` 成交量 null 視為無成交，`Cancelled` 成交量 null 視為可能殘留部位。
- `test_close_that_does_not_fill_is_a_residual_position`（平倉逾時、平倉被拒）、`test_interlock_abort_before_the_close`、`test_interrupt_during_fill_wait`。
- `test_rejection_recorded_and_continues` — 422 被記錄，下一個案例照常執行；`test_rejected_place_skips_remaining_steps`；`test_rejected_cancel_of_resting_order_exits_2`；`test_rejected_cancel_of_filled_order_is_a_residual_position`；`test_no_response_to_a_mutation_stops_the_run`；`test_server_error_on_a_mutation_stops_the_run`（下單、刪單、平倉回 5xx）；`test_unparseable_place_body_counts_as_accepted_without_trade_id`；`test_unprepared_case_sends_nothing_and_exits_2`。
- `test_session_bounds`、`test_futures_refused_outside_day_session_sends_nothing`、`test_futures_fill_case_refused_when_it_could_outlast_the_session`、`test_stock_fill_case_refused_when_it_could_outlast_the_twse_session`。
- `test_rod_case_without_cancel_rejected_before_any_request`、`test_case_not_meant_to_fill_must_rest_far_from_the_market`、`test_fill_case_cannot_update_qty`、`test_exceed_funds_needs_stock_at_limit_down`、`test_only_filter`（`--only harness_probe` 只執行該案例）、`test_unknown_only_id_rejected_before_any_request`。
- `test_order_not_meant_to_fill_that_fills_is_a_residual_position`、`test_fill_case_ending_partly_filled_is_a_residual_position`、`test_accepted_fill_case_without_trade_id_is_a_residual_position`、`test_rejected_cancel_of_failed_order_completes`、`test_failed_status_on_a_2xx_place_counts_as_rejected`、`test_session_checked_again_after_preparation`、`test_session_bounds_take_any_timezone`。
- `test_orders_json_rewritten_after_every_case`、`test_write_failure_does_not_hide_the_interlock_abort`。
- `test_place_bodies_carry_typed_values_and_full_account`、`test_render_template_substitutes_typed_values`、`test_template_naming_account_identifier_rejected`、`test_template_placeholders_validated`、`test_orders_json_content`。

**Numbers:**

- 生命週期案例用 2 單位：台灣市場的改量只能減少，至少 2 單位才能減 1 後仍保有委託以測刪單。
- 市價類案例用 1 張、1 股、1 口：各市場的最小交易單位，資金需求最低。
- 期交所日盤 08:45–13:45、證交所 09:00–13:30，其中 13:25–13:30 為證交所收盤集合競價（台北時間）：設計 §4。
- `step_wait_s`、`fill_poll_s`、`fill_timeout_s`：`p1.toml` 為初值，`calibrate:` Task 12 第一個生命週期案例量出「送出到回報出現」的延遲後設定，量測值記入契約文件。
- 1000 股／張：同 Task 5。

**Verification:**

Run: `uv run pytest -q tests/test_orders.py`
Expected: 全部 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Run simulated order scenarios behind the interlock"
```

---

### Task 8：SSE 分析：對帳、client 覆蓋與串流統計

**Files:**
- Create: `src/p1rec/records.py`（JSONL 與 SSE 檔讀取、數值與時間換算、結束碼）、`src/p1rec/reconcile.py`、`src/p1rec/stats.py`
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`、`p1.toml`（`[events]`）
- Test: `tests/test_analysis.py`（涵蓋三個模組）

**Behaviour:**

- `p1rec reconcile --sse-run <run_id> [--rest-run <run_id>] [--trading-day YYYY-MM-DD]`：run_id 為 `results_root` 下的目錄名，含路徑分隔字元時拒絕。未給 `--rest-run` 時，連鎖檢查後依 `[trades] account_types` 逐一以該帳戶的 `AccountRequest` 加 `refresh: true` 即時查 `trades`（不帶帳戶時只回預設的證券帳戶，10/1 live 實測），全部記在本次 run 的 `rest.jsonl`，以聯集比對；給了則用該 run 每個帳戶類型最後一次 `trades` 回應，缺任一類型或請求不帶帳戶時失敗（非 2xx 時失敗，不退回較舊的回應）。先取 REST 再讀 SSE 檔。
- 日期窗口（兩側皆套用）：`--trading-day` 是 Asia/Taipei 的日曆日期，不是期交所的交易日（夜盤委託屬於下一個期交所交易日，但依下單當下的日曆日期計）。SSE 側只有第一筆報告的交易所時間（委託取 `order_exchange_ts`、成交取 `deal_exchange_ts`，epoch 秒；不大於 0 或沒有時視為沒有；沒有任何報告帶交易所時間時以第一筆的收到時間代替並記 WARN）落在該日期的委託參與對帳，其餘依 client 計入 `excluded_other_days`；REST 側以同樣的台北日期檢查 `trades.order_ts`，其他日期的委託計入 `rest_excluded_other_days`，沒有委託時間（缺少、null 或不大於 0）的委託保留並記 WARN。兩側皆不默默丟棄。時間落在夜盤（台北 15:00 至次日 05:00）的委託記 WARN。未給 `--trading-day` 時取 REST 快照各委託 `trades.order_ts`（不大於 0 視為沒有）的台北日期；快照為空清單時不推斷日期、直接依下方規則回 2，有委託但沒有任何時間或跨越兩個以上日期時回 1，要求指定。
- 依 `EventFields` 從各 client 的全部 SSE 檔（`order_event` 串流與可能也帶委託事件的綜合串流）挑出事件名稱為 `order_event_name` 的 frame；報告在 `data/<state>`（OpenAPI `OrderEvent`），`state` 屬 `order_states` 為委託、屬 `deal_states` 為成交。每筆報告的識別為 `event_id`（空字串時為 data 原文的 SHA-256）；同一 client 內以識別去重並保留最早收到的一筆（同一 `event_id` 內容不同時記 WARN），依收到時間整理為「委託識別 → 最終狀態、累計成交量」，並記下該 client 看到的報告識別。只解碼含事件名稱的行。
- 官方委託事件不帶狀態字串，SSE 端狀態由 `derive_status` 推得，依序取第一條成立者：New 的 `op_code` 不等於 `op_code_success` → Failed；沒看到成功的 New 或沒有委託數量 → Unknown（缺了 New，其餘歷史不完整）；成交量大於 0 且不小於「委託數量 − 成功 UpdateQty 的 `cancel_quantity` 合計」→ Filled；有成功的 Cancel 且成功 UpdateQty 與 Cancel 的 `cancel_quantity` 合計加成交量不小於委託數量 → Cancelled；成交量大於 0 → PartFilled；其餘 → Submitted。委託數量取自成功 New 的 `order_qty`（`/order/quantity`；10/1 live 實測改量後仍為下單數量）；`cancel_quantity` 是該次操作取消的量（實測不累計），依報告識別去重後加總；被拒的操作不更新任何數量（可能帶的是請求的數量）。UpdatePrice、UpdateQty 只更新數量，其他 op_type 記 WARN。`calibrate:` 以下為尚未錄到事件的假設，錯了會以不一致呈現：減量後成交量補滿剩餘時 REST 為 Filled；IOC／FOK 逾時以成功的 Cancel 送達；Reject 操作不改變狀態。REST 狀態以 `order_status.normalize_status`（去掉 `Status.` 前綴，orders 共用）正規化後比較；`deal_quantity` 缺少或為 null 時視為 0。
- `reconcile.json`：`only_in_rest`（至少一個 client 沒看到的 REST 委託；GO 準則 4 與設計 §6 D1 量的是每個 client 都收齊，任一 client 缺一筆即列入）、`only_in_sse`（任一 client 看到而 REST 沒有的委託）、`mismatched`（`Mismatch(client, sse, rest)`：每個看到該委託而狀態或成交量與 REST 不同的 client 各一筆）、`missing_by_client`（各 client 缺的 REST 委託）、`missing_events_by_client`（任一 client 看到而此 client 沒看到的報告識別；只看到 Cancel 而缺 New 的 client 由此失敗）、`per_client_keys`（各 client 看到的委託識別，設計 §6 D）、`excluded_other_days`（各 client 因不在該日期而排除的委託數）、`rest_excluded_other_days`（REST 因不在該日期而排除的委託數），`rest_order_count`（參與對帳的 REST 委託數）、`sse_order_count_by_client`（各 client 在該日期看到的委託數，沒有時為 0），另記 `sse_run`、`rest_run`、`trading_day`（空快照且未指定時為 null）與 `clean`。`rest_order_count` 為 0 時記 ERROR 並回 2（沒有比對任何委託，不能當作通過）；否則前三者與 `missing_events_by_client` 皆空時回 0，其餘回 2；輸入無法分析時回 1。
- `p1rec stats --sse-run <run_id>` 只讀錄製檔、不連 server，因此不需要密鑰環境變數。寫 `stats.json`：每個 client 各事件名稱的數量；以下皆逐檔（一個 recorder 一檔）計算，同一 client 的綜合串流與 `order_event` 串流才不會重複計數：每秒 frame 數最高的前 10 秒（以 `recv_ts_ns` 取整秒，UTC ISO 時間）；心跳事件（名稱為 `heartbeat_event`）在同一連線內的間隔最小、中位數（`statistics.median`）、最大值；相鄰 frame 間隔超過 `gap_threshold_s` 的清單；斷線到下一次 200 連上的區間（含嘗試次數與第一個錯誤）；`exchange_ts_pointers` 有設定時，收到時間減交易所時間的 p50、p99（nearest-rank）、最大值與無法解析的筆數。
- JSONL 最後一行無法解析時記 WARN 並略過（寫入未 fsync，斷電會留下截斷的末行）；中間行無法解析時失敗。

**Interface:**

```python
@dataclass(frozen=True)
class EventFields:  # 事件名稱與 JSON 指標，取自 2026-09-29 錄到的真實事件
    order_event_name: str             # 官方 1.7.6 為 "order_event"
    order_states: tuple[str, ...]     # StockOrder、FuturesOrder
    deal_states: tuple[str, ...]      # StockDeal、FuturesDeal
    event_id: str                     # 此行起八個指標相對於 data/<state> 的報告
    order_key: str
    op_type: str
    op_code: str
    order_qty: str                    # /order/quantity；Cancel 事件的 status.order_quantity 為 0
    cancel_qty: str
    deal_key: str
    deal_qty: str
    op_code_success: str              # 官方 1.7.6 為 "00"
    order_exchange_ts: str            # 委託報告的交易所時間（epoch 秒），/status/exchange_ts
    deal_exchange_ts: str             # 成交報告的交易所時間（epoch 秒），/ts
    exchange_ts_pointers: tuple[str, ...]  # 空則不算延遲
    exchange_ts_format: str | None    # None：單一指標為 epoch 秒數值；否則各字串以空白串接後以 strptime 解析，時區固定為 Asia/Taipei
    heartbeat_event: str              # 官方 1.7.6 為 "heartbeat"
    gap_threshold_s: float

@dataclass(frozen=True)
class TradeFields:  # [trades]：trades 回應與單一 Trade 的 JSON 指標，orders 與 reconcile 共用
    list: str                         # "" 表示回應本身即清單
    id: str                           # /order/id，update_price、update_qty、cancel 的 trade_id
    status: str                       # /status/status
    deal_quantity: str                # /status/deal_quantity
    order_ts: str                     # 各委託的下單時間（epoch 秒），/status/order_ts

# p1rec.reconcile
@dataclass(frozen=True)
class OrderView:
    key: str
    status: str
    filled_qty: Decimal

@dataclass(frozen=True)
class Mismatch:
    client: str
    sse: OrderView
    rest: OrderView

@dataclass(frozen=True)
class ClientOrders:
    orders: dict[str, OrderView]    # 委託識別 -> 推得的狀態與成交量
    event_ids: frozenset[str]       # 這些委託背後的報告識別
    excluded_other_days: int        # 因不在該日期而排除的委託數

@dataclass(frozen=True)
class RestOrders:
    orders: dict[str, OrderView]    # 委託識別 -> REST 的狀態與成交量
    excluded_other_days: int        # 因不在該日期而排除的委託數

@dataclass(frozen=True)
class ReconcileReport:
    only_in_rest: list[str]                  # 至少一個 client 缺的 REST 委託
    only_in_sse: list[str]                   # 任一 client 看到而 REST 沒有
    mismatched: list[Mismatch]               # 每個不一致的 client 各一筆
    missing_by_client: dict[str, list[str]]  # client -> 缺的 REST 委託
    per_client_keys: dict[str, list[str]]    # client -> 看到的委託識別
    missing_events_by_client: dict[str, list[str]]  # client -> 別的 client 看到而它沒看到的報告識別
    excluded_other_days: dict[str, int]      # client -> 因不在該日期而排除的委託數
    rest_excluded_other_days: int            # REST 因不在該日期而排除的委託數

def derive_status(*, new_ok: bool | None, cancel_ok: bool, order_qty: Decimal | None,
                  cancel_qty: Decimal, filled_qty: Decimal) -> str: ...
def orders_from_sse(paths: Mapping[str, Sequence[Path]], fields: EventFields,
                    trading_day: date | None = None) -> dict[str, ClientOrders]: ...
def trading_day_of(payload: object, fields: EventFields) -> date: ...  # REST 快照委託時間的台北日期
def orders_from_rest(payload: object, fields: EventFields, trading_day: date | None = None) -> RestOrders: ...
def reconcile(sse: Mapping[str, ClientOrders], rest_orders: RestOrders) -> ReconcileReport: ...
def run_reconcile(cfg: P1Config, sse_run: str, rest_run: str | None, run_dir: Path, *,
                  trading_day: date | None = None, transport: httpx.BaseTransport | None = None) -> int: ...

# p1rec.stats
def stream_stats(paths: Mapping[str, Sequence[Path]], fields: EventFields) -> dict[str, object]: ...
def run_stats(cfg: P1Config, sse_run: str, run_dir: Path) -> int: ...

# p1rec.records
class AnalysisError(ValueError): ...
def read_jsonl(path: Path, contains: bytes | None = None) -> Iterator[tuple[int, dict[str, object]]]: ...
def discover_sse_files(run_dir: Path) -> dict[str, list[Path]]: ...  # client -> 該 client 的全部 SSE 檔
```

**Tests:**

- `test_all_match_exit_0` — 兩個 client 與 REST 一致時三份清單皆空。
- `test_missing_in_sse_reported` — REST 多一筆時列入 `only_in_rest` 並回 2。
- `test_status_mismatch_reported` — SSE 為 PartFilled、REST 為 Filled 時列入 `mismatched`。
- `test_client_coverage_difference` — client b 缺一筆（client a 有看到）時列入 `only_in_rest` 與 `missing_by_client`，`per_client_keys` 可看出差集，並回 2。
- `test_stats_runs_without_secret_env` — 未設定密鑰環境變數時 `stats` 照常回 0。
- `test_deals_accumulate_qty` — 同一委託兩筆成交數量加總。
- `test_stats_peak_second_and_gaps` — 合成資料中某秒 50 個 frame、某處間隔 90 秒時，`stats.json` 的尖峰與間隔清單正確。
- `test_derive_status_rules` — 推導規則逐條成立（含部分成交後刪單、只見成交、數量為 0）。
- `test_partial_fill_then_cancel_is_cancelled` — 依真實事件形狀（Cancel 的 `status.order_quantity` 為 0），New(3)、成交 1、Cancel 後為 Cancelled，成交量 1。
- `test_rejected_update_qty_does_not_change_size` — 部分成交後被拒的 UpdateQty 不會讓狀態變成 Filled。
- `test_event_on_two_streams_counted_once` — 同一事件出現在綜合串流與 `order_event` 串流時只算一次。
- `test_two_day_sse_against_one_day_rest_snapshot` — SSE 檔含前一日與當日委託、REST 只有當日時，交易日取自 REST，前一日委託計入 `excluded_other_days` 且不影響結果。
- `test_trading_day_required_without_rest_order_times`、`test_rest_snapshot_spanning_two_days_needs_trading_day` — REST 沒有委託時間或跨兩日時要求 `--trading-day`。
- `test_explicit_trading_day_windows_both_sides` — REST 有 t1（10-01）與 thu（09-30）、SSE 兩個 client 亦同，`--trading-day 2026-10-01` 時 thu 在兩側皆排除，結果一致並回 0。
- `test_undated_rest_order_kept_with_warning`、`test_default_day_ignores_non_positive_order_times` — REST 委託時間缺少或不大於 0 時保留並記 WARN，推導日期時略過。
- `test_night_session_orders_warn_on_both_sides` — 夜盤時間的委託在 SSE 與 REST 兩側皆記 WARN。
- `test_client_missing_reports_fails` — client a 收到 New、UpdatePrice、Cancel，client b 只收到 Cancel 時，b 的委託為 Unknown、`missing_events_by_client` 列出 b 缺的兩筆，回 2。
- `test_lifecycle_case_under_candidate_shapes` — New(2) → UpdatePrice → UpdateQty:1 → Cancel 以 10/1 實測的事件形狀（保留原數量、`cancel_quantity` 為單次）推導為 Cancelled。
- `test_reduced_order_filling_its_remainder_is_filled` — 調降為 1 後成交 1 為 Filled。
- `test_truncated_last_line_skipped_with_warning`、`test_unreadable_middle_line_raises` — 末行截斷略過並記 WARN，中間行損壞則失敗。
- `test_duplicate_across_streams_keeps_earliest_arrival`、`test_repeated_event_id_with_other_content_warns` — 跨串流重複時保留最早收到的一筆；同一 `event_id` 內容不同時記 WARN。
- `test_update_price_keeps_status_and_size`、`test_mismatch_in_one_client_only`、`test_trades_item_without_deal_quantity_counts_zero` — UpdatePrice 不改狀態與數量；只有一個 client 不一致時只列該 client；REST 缺 `deal_quantity` 視為 0。
- `test_outage_ended_by_stop`、`test_last_trades_payload_rejects_non_2xx`、`test_last_trades_payload_decodes_base64_body` — 停止時仍未恢復的斷線照列；最後一次 `trades` 非 2xx 時失敗；`resp_body_b64` 解碼後使用。
- `test_live_reconcile_aborts_before_trades_when_not_simulation` — 模式非模擬時不送出 `trades` 查詢。

**Numbers:**

- 尖峰取前 10 秒：報告篇幅上的選擇，用來看開盤尖峰的形狀。
- `gap_threshold_s = 60`：契約文件 §2 量到的 30 秒心跳的兩倍；`calibrate:` 改為 Task 12 首日量到的心跳最大間隔的兩倍，寫入 `p1.toml`。
- 委託事件的指標、`op_code_success` 與 `exchange_ts_*`：取自 2026-09-29 錄到的委託事件與 tick（契約文件 §2）。
- `deal_key`、`deal_qty`、`deal_exchange_ts`：`calibrate:` 尚未錄到成交事件，暫依 OpenAPI `StockDealEvent`／`FuturesDealEvent`；Task 12 第一批成交事件確認，Task 13 對帳前寫入 `p1.toml`。
- `trades.order_ts`：epoch 秒，已由期貨 API 測試中刪單後的 `trades` 回應確認（`1790677936.694802`，即 2026-09-29 18:32:16 台北時間）。
- 夜盤 15:00 至次日 05:00：設計 §4 的期交所夜盤時段。

**Verification:**

Run: `uv run pytest -q tests/test_analysis.py`
Expected: 全部 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Reconcile SSE events against REST and summarise streams"
```

---

### Task 9：歷史資料查詢與額度量測

**Files:**
- Create: `src/p1rec/history.py`
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`
- Test: `tests/test_history.py`

**Behaviour:**

- `p1rec history`：連鎖檢查後依 `[[history.query]]` 逐一執行 `ticks`、`kbars`、`snapshots` 查詢（三者皆為 POST，請求放在 JSON 本體），每個查詢前後各查一次 `usage`。
- `history.json` 每筆記：查詢參數、狀態碼、回應位元組數、查詢前後的額度用量與差值。額度欄位找不到時，差值記為 `null` 並記 WARN。
- 任一查詢失敗記 ERROR 並繼續；只要有一筆失敗就以 2 結束。

**Interface:**

```python
@dataclass(frozen=True)
class HistoryQuery:
    op: Literal["ticks", "kbars", "snapshots"]
    body: str  # JSON 本體，欄位依 TicksRequest／KBarsRequest／SnapshotRequest

@dataclass(frozen=True)
class HistoryConfig:
    queries: tuple[HistoryQuery, ...]
    usage_bytes_pointer: str  # 官方 1.7.6 為 "/bytes"（UsageOut）

def run_history(cfg: P1Config, run_dir: Path) -> int: ...
```

`p1.toml` 的查詢：2330 前一交易日 ticks；2330 前一交易日 kbars（KBarsRequest 只收日期）；TMF 前一交易日 kbars；錄製商品全部的 snapshots。

**Tests:**

- `test_usage_delta_recorded` — 查詢前後 usage 為 100、250 時差值為 150。
- `test_missing_usage_field_null_and_warn` — 找不到額度欄位時差值為 `null` 且記 WARN。
- `test_failed_query_continues_exit_2` — 第一筆 500 時第二筆仍執行，最終回 2。

**Verification:**

Run: `uv run pytest -q tests/test_history.py`
Expected: 全部 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Measure history queries and their quota cost"
```

---

### Task 10：去識別化、語料匯出與洩漏掃描

**Files:**
- Create: `src/p1rec/scrub/`（套件）、`tools/p1-official-server/fixtures.toml`（空清單）
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`
- Test: `tests/test_scrub.py`

**Behaviour:**

- 替換對照的來源：`secret_env` 的全部密鑰（含 API key 前 10 字元與對外 IP）、以 `account_id_pointers` 從 `accounts` 回應找出的帳號、以 `ca_pointers` 從 `ca_status` 回應找出的憑證內容。
- 佔位字串依「類別 + 首次出現順序」決定（例如 `ACCOUNT_1`）；同一原始值永遠得到同一佔位字串；對照表存 `results/p1/scrub-map.json`（不進版控），重跑時沿用。
- 替換依原始值長度由長到短逐一進行（每個原始值的全部寫法一起換），兩個原始值重疊時較長者整段替換；嵌在較長字串中的值（例如 clientname 內的 key 前綴）也要替換；佔位字串不得包含任何原始值。
- 原始值的每種寫法都要處理：原文、去除前後空白的形式、JSON 跳脫後的形式（含 `\uXXXX`，任意跳脫深度）、百分比編碼，以及 `*_b64` 欄位解碼後的內容；掃描另外比對 NFKC 正規化後的文字。帳戶物件的 `username` 也屬於要替換的值。
- 長度（UTF-8 位元組）低於 `MIN_SUBSTRING_MATCH_LEN`（6）的原始值只以完整 token 比對（前後不接英數字或底線；前面緊接 JSON 跳脫如 `\n`、`\u00e9` 或百分比編碼如 `%3A` 也算邊界），避免 `00`、`2330` 之類的值誤中價格與代碼；這只適用於 `SUBACCOUNT` 與 `CA_PASSWD`，身分類別（API key 與其前綴、`SEC_KEY`、`PERSON_ID`、`EGRESS_IP`、`ACCOUNT_ID`、`USERNAME`、`CA_SIGNATURE`）出現過短的值代表指標或鍵選錯欄位，以 `ScrubError` 依類別回報（不印原始值）。各類別低於門檻的數量，以及每筆 fixture 每個類別的替換次數都記 log（不記原始值）。
- `account_id_keys`（`subaccount`）列出的鍵，其非空值在錄製 JSON 的任何深度都視為帳號（類別為鍵名大寫，例如 `SUBACCOUNT_1`）。
- 委託物件的 `ca` 是 CA 簽章（base64 DER，可能內含憑證持有人姓名與身分證字號，字串比對看不到），每個非空值都是原始值，類別 `CA_SIGNATURE`（`CA_SIGNATURE_1`、`CA_SIGNATURE_2`……），與其他類別一樣存入對照表，因此出現在任何形式（例如 log 裡的 Debug 輸出）都會被替換與掃出。
- `ca` 與 `account_id_keys` 的值從 `results_root` 下全部錄製找出：每個 `rest.jsonl` 與 `sse_*.jsonl` 的請求、回應與 frame（含 `*_b64` 欄位解碼後的內容），不限於選取行。掃描另外以結構檢查：任何非空且非佔位字串的 `ca` 欄位回報為 `CA_SIGNATURE`，`account_id_keys` 的欄位同理以其類別回報，即使該值不在對照表中；欄位形式包含任意跳脫深度的 JSON 成員、Python repr（`'key': 'value'`）、Debug 輸出（`key: "value"`、`key: Some("value")`）與 `key=value`（引號可單可雙，`None`、`null` 視為空值）。無法解碼的 `*_b64` 欄位回報為 `UNDECODABLE_B64`。
- `account_id_pointers` 的最後一段與 `account_id_keys` 不得以 `_<數字>` 結尾，否則類別與佔位字串的編號無法區分。
- `p1rec scrub` 依 `fixtures.toml` 從 `results/` 取出指定檔案與行，替換後寫入 `test_data/official/1.7.6/`，並產生 `MANIFEST.toml`（每筆：檔名、來源 run_id、來源檔、行號、錄製時間、商品、對應的 §6 項目）。輸出先在 `results/` 下暫存並掃描，通過才寫入；有洩漏時回 2，fixture 目錄不變。寫入後 fixture 目錄若還有目前選取以外的檔案（`MANIFEST.toml` 與 `openapi.json` 除外），列出後回 2，不刪除任何檔案。
- `results_root` 必須被 git 忽略（`git check-ignore`），且對照表不得已被 git 追蹤（`git ls-files --error-unmatch`），否則匯出與 `--scan-only` 都回 1；log 中的路徑先做 NFKC 正規化再去識別化；`new_run` 以 0700 建立 `results_root` 與每個 run 目錄，既有的 `results_root` 也改為 0700。
- 錄製時間取自第一個選取行：REST 紀錄的 `recv_ts_ns`（連線錯誤為 `error_ts_ns`）、SSE frame 的 `recv_ts_ns`、log 行開頭的 ISO-8601 時間戳（先去除 ANSI 色碼；錄製器 log 不帶時區，視為錄製主機的本地時間）。取不到時該筆須在 `fixtures.toml` 寫明帶時區的 `recorded_at`，否則 `ConfigError`；取得到時不接受 `recorded_at`。不使用檔案時間（主機曾發生 RTC 重置）。
- `fixtures.toml` 某筆會公開的欄位（`name`、`run_id`、`source`、`instrument`、`checklist`）含任何原始值時 `ConfigError`，只以序號指出該筆，不印原始值。
- `p1rec scrub --scan-only` 一律掃描 `fixtures_root` 的上層（`test_data/official/`）與 `contract_doc`（`docs/official-server-contract.md`），`extra_scan_paths` 只能再增加目標（例如 `fixtures.toml`），空清單不會減少掃描範圍；任一目標不存在或與 `results_root` 重疊時回 1。檔名與目錄名也掃描（相對於掃描根目錄，行號記為 0）。發現任何原始值時列出「檔案:行號:類別」（路徑先去識別化，不印原始值）並回 2，否則回 0。
- 指標對應的類別（例如 `ACCOUNT_ID`）在對照表與全部錄製中都沒有值時（尚未錄到 `accounts` 回應），掃描無從檢查這些值，匯出與 `--scan-only` 都記 ERROR 並回 1。

**Interface:**

```python
@dataclass(frozen=True)
class ScrubConfig:
    account_id_pointers: tuple[str, ...]
    ca_pointers: tuple[str, ...]
    account_id_keys: tuple[str, ...]  # 值在任何深度都視為帳號的鍵，例如 subaccount
    map_path: Path
    selection: Path  # fixtures.toml；每筆 name、run_id、source、lines、checklist、instrument，選填 recorded_at
    contract_doc: Path  # 與 fixtures_root 的上層一律掃描
    extra_scan_paths: tuple[Path, ...]  # 只能增加掃描目標

@dataclass(frozen=True)
class Leak:
    path: Path
    line: int
    kind: str

def build_map(secrets: Mapping[str, str], discovered: Mapping[str, Sequence[str]],
              existing: Mapping[str, str]) -> dict[str, str]: ...
def scrub_text(text: str, mapping: Mapping[str, str]) -> str: ...
def export_fixtures(cfg: P1Config, mapping: Mapping[str, str]) -> list[Path]: ...
def scan(paths: Iterable[Path], originals: Mapping[str, str],
         keys: Mapping[str, str] | None = None) -> list[Leak]: ...  # keys：鍵名 -> 類別，另加於 ca
```

**Tests:**

- `test_deterministic_placeholders` — 同一值兩次建表得到同一佔位字串，且沿用既有對照表。
- `test_longest_first_replacement` — 前綴與完整 key 同時出現時，完整 key 整段替換。
- `test_embedded_value_replaced` — `PYAPI/<前綴>/0929/...` 中的前綴被替換。
- `test_scan_reports_without_value` — 有洩漏時回報檔案與行號，輸出不含原始值。
- `test_scan_clean_returns_zero` — 無洩漏時回 0。
- `test_manifest_entry_per_fixture` — 匯出兩筆時 `MANIFEST.toml` 有兩筆，欄位齊全。
- `test_overlapping_originals_replace_longer_whole` — `QQQXYZ` 與 `XYZWWWWWWW` 相鄰重疊時，較長者整段替換。
- `test_spec_targets_scanned_without_extra_paths` — `extra_scan_paths = []` 時，`test_data/official/` 與契約文件中的洩漏仍被回報。
- `test_manifest_recorded_at_for_every_kind` — `.json`、`.sse`、`.log`（官方 server log 與錄製器 log）都帶錄製時間。
- `test_discover_reads_every_recording` — `ca` 與 subaccount 從 SSE 錄製與只存於 `resp_body_b64` 的回應中找出。
- `test_ca_signature_and_subaccount_scrubbed_on_export` — trades 回應與 `order_event` frame 的 `ca` 換成 `CA_SIGNATURE_<n>`，subaccount 換成 `SUBACCOUNT_<n>`。
- `test_non_json_log_values_scrubbed_and_detected` — 只在未選取的錄製中以 JSON 出現的 `ca`（log 中為 Debug 格式）與 subaccount（log 中為純文字）在匯出時被替換，植入已提交檔案後 `--scan-only` 回 2。
- `test_scan_reports_ca_signature_and_account_keys` — 不知道原始值時，非佔位字串的 `ca` 與 `subaccount` 成員在各跳脫深度都被回報。
- `test_short_originals_match_only_whole_tokens` — `A1`、`00`、`2330` 只在完整 token 處替換與回報，不因出現在佔位字串內而拒絕。
- `test_scan_reports_debug_and_key_value_fields` — log 中 Debug 與 `key=value` 形式、從未錄到的簽章被回報。
- `test_scan_reports_names_and_logs_them_scrubbed` — 檔名 `C9_<帳號>_clean.json` 回報為第 0 行，log 中的路徑已去識別化。

**Verification:**

Run: `uv run pytest -q tests/test_scrub.py`
Expected: 全部 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Scrub fixtures and scan for leaked identifiers"
```

---

### Task 11：離線演練（假 server、真實 CLI 入口）

**Files:**
- Create: `tests/stub_server.py`、`tests/data/*`、`tests/test_rehearsal.py`

**Behaviour:**

- `stub_server.py` 以 `ThreadingHTTPServer` 在隨機埠提供全部邏輯操作：`mode`（可切換 Simulation／Production）、帳戶、合約、餘額、保證金、usage、`ca_status`、`trades`、下單／改單／刪單、歷史查詢，以及可推送行情、委託、成交事件並能中途斷線的 SSE。欄位名稱沿用 Task 1 `openapi.json` 的對應 schema，資料為虛構；preflight 查詢的回答沿用 `tests/mock_official.py` 的 `FakeServer`。
- `test_rehearsal.py` 以 subprocess 呼叫 `p1rec` console script，用指向假 server 的測試設定（`tests/data/p1.toml`、`tests/data/scenarios.toml`，在 `git init` 過、`results_root` 被 git 忽略的暫存目錄執行）依序執行：`self-test-interlock` → `preflight` → `stream`（背景）→ `orders` → 中途切斷 SSE → `history` → 停止 `stream` → `reconcile`（不帶 `--rest-run`，連鎖檢查後即時查 `trades`，與 Task 12 相同）→ `stats` → `scrub` 匯出兩筆 → `scrub --scan-only`。
- 例外：`orders` 在測試程序內以 `p1rec.cli.main([...])` 執行，並依 `tests/test_orders.py` 的方式注入時鐘（把 `cli.run_orders` 換成帶 `clock` 的版本；時鐘只把牆上時間移進交易時段，等待與經過時間照實）。`orders` 在期交所日盤與證交所交易時段外拒絕期貨與會成交的股票案例，而正式程式不留測試用的繞道，只有注入時鐘能讓演練在任何時刻執行。`stream` 則必須是 subprocess：它擋下停止訊號，且有其他執行緒（例如假 server 的）在跑時拒絕啟動。

**Tests:**

- `test_full_pipeline_against_stub` — 每一步結束碼符合預期，`reconcile.json` 的 `only_in_rest`、`only_in_sse`、`mismatched` 與 `missing_events_by_client` 皆空（假 server 的 trades 項目要帶 `status.order_ts`），`stats.json` 記到斷線間隔，`MANIFEST.toml` 有兩筆，掃描回 0。
- `test_mode_flip_stops_orders` — 劇本中途切到 Production 後，假 server 收到的下單請求數不再增加，`orders` 回 3。
- `test_scan_catches_planted_leak` — 在匯出的 fixture 植入假帳號的原始值後，`scrub --scan-only` 回 2。

**Verification:**

Run: `uv run pytest -q`
Expected: 全部測試 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Rehearse the full recorder pipeline against a stub server"
```

---

### Task 12（live，週四）：preflight、soak 起點、行情、下單與歷史

**狀態：** 完成。10/1 的下單、歷史與校準見契約文件 §2；soak 起點改由 `2026-10-06-soak-rehost-plan.md` R4 在固定 IP 主機完成（`521a24f`）。

**Files:**
- Modify: `tools/p1-official-server/p1.toml`、`docs/official-server-contract.md`（§2 執行紀錄）

**Behaviour:**

- 排程：soak 於 10/1–10/5 在家用網路執行，因浮動對外 IP 五天內換了兩次、每日重新登入被白名單擋下，10/5 14:45 暫停；改在有固定對外 IP 的 AWS 台北主機上重新執行 Task 12 的 soak 起點、Task 13 與 Task 14，依 `2026-10-06-soak-rehost-plan.md` 的 R4–R8 進行，soak 為 10/6 16:44 第一次登入至 10/12 09:30。10/1–10/5 的紀錄保留為家用網路的證據。
- 離線演練（Task 11）沒有涵蓋、只能在本日驗證的路徑，逐項觀察並記入契約文件：委託在下單回應之後才結算（等待成交、`PendingSubmit` 時刪單、`step_wait_s` 等時間的實際值）、部分成交與多筆成交、成交事件的 `deal_key` 與 `deal_qty`、IOC／FOK 到期的事件形狀、以事件回報的改單或刪單拒絕、不帶帳戶的 `trades` 是否列出所有帳戶的委託、斷線後訂閱是否需要重送、串流流量是否計入每日額度、夜盤與跨日。
- 改動 `p1.toml` 的指標、範本、`[events]` 或 `[trades]` 時，同步改 `tests/data/p1.toml`，否則離線測試的設定比對會失敗。
- 前一個交易日：依當日合約清單校準 `p1.toml` 的近月期貨代號（TXFJ6、MXFJ6、TMFJ6 於 2026-10-21 到期，soak 跨過該日就改用下一個月份）與近月價平 TXO 買權、賣權各一檔，同步更新 `[stream]` 商品、`[history]` 的 snapshots 查詢與 `scenarios.toml`。
- 07:30 前：本日與 Task 13、14 的每個 shell 先執行 `umask 077`，`p1rec` 以外建立的目錄（server log 目錄、第二個實例的 `SJ_HOME_PATH` 與 env 檔）才不會在多人共用主機上讓其他使用者讀到；校時狀態（`timedatectl` 與 `chronyc tracking` 或等效指令）存入 `results/p1/<日期>-ntp.txt`；填入 `futures_margin_twd`；啟動官方 server 並依 Task 1 記下的方式把 log 導向 `results/p1/<日期>-server/server.log`（放在自己的 run 目錄內，Task 15 才能以 `fixtures.toml` 挑選）；`p1rec self-test-interlock` 回 0；`p1rec preflight` 回 0，回 2 即停止並交由使用者取得模擬資金。
- 08:00 前：`p1rec stream --clients a,b --until <下週二 09:30 +08:00>` 於 tmux 啟動，即為 soak 起點（關閉期間最長 `SSE_READ_TIMEOUT_S + retry_s`，這段時間再按 Ctrl-C 不會生效，必要時以 SIGKILL 強制結束）；預先排入週五 10:30 取消 2330 tick 訂閱的動作（§6 D2：訂閱為全域，看兩個 client 是否同時停止收到 2330）。
- 08:30 後（每日維護之後、下單之前）：再跑一次 `p1rec preflight`，以當日更新後的漲跌停價確認資金；回 2 即停止。
- 09:00 後：`p1rec orders --only <第一個生命週期案例>`，量出延遲後填入 `step_wait_s`、`fill_poll_s`、`fill_timeout_s`，再以 `p1rec orders` 跑其餘案例；從第一批真實事件填入 `EventFields`。同時確認四件事：IOC／FOK 未成交時交易所自動刪單的事件形狀（是否以 Cancel 事件送出）、部分成交後刪單時 REST 的狀態字串、成交事件的 `deal_key` 與 `deal_qty`、改量後刪單時 `cancel_quantity` 是累計值還是單次值；與 `derive_status` 的規則不符時先修正，再進 Task 13。
- 13:45 後：`p1rec reconcile --sse-run <stream run_id>` 對當日委託對帳。
- 收盤後：以 `p1rec scrub --scan-only` 的 log 確認每個類別的原始值長度都不低於 `MIN_SUBSTRING_MATCH_LEN`（log 只列類別與數量），有低於門檻的類別就先檢查對應的 JSON 指標再調整。
- 14:00 後：`p1rec history`；`p1rec stats --sse-run <stream run_id>` 取心跳最大間隔，設定 `gap_threshold_s`。
- 之後由持續運行的 `stream` 錄下夜盤；當日不再觸碰官方 server。

**Numbers:**

- soak 起訖與交易時段：設計 §4、§9.2。
- 07:30：在 08:00 soak 起點前預留 30 分鐘完成 preflight，出問題時仍趕得上 08:30 試撮。
- 連線總數不超過 5：設計 §4；`preflight.json` 的 `connections` 即為量測值。

**Verification:**

Run: `jq '.funds_ok, .connections' results/p1/<當日 preflight run>/preflight.json`
Expected: `true`，連線數不超過 5。

Run: `for f in results/p1/<stream run>/sse_*_*.jsonl; do printf '%s ' "$f"; jq -c 'select(.kind=="frame")' "$f" | wc -l; done`
Expected: 每個 client 的行情串流檔皆大於 0，且 15:00 後重跑時數字仍在增加。

Run: `cd tools/p1-official-server && uv run p1rec scrub --scan-only; echo $?`
Expected: `0`，之後才 commit；回 2 時先移除 log 指出的內容。

**Commit:**

```bash
git add tools/p1-official-server/p1.toml docs/official-server-contract.md
git commit -m "Calibrate recorder settings from the first live session"
```

---

### Task 13（live，週一 10/5）：多 client、harness 實例、重啟與對帳

**狀態：** 完成，由 `2026-10-06-soak-rehost-plan.md` R7 於 10/8 執行（`a62ca32`，harness 依使用者決定以文件作答 `bd5df28`）。

**Files:**
- Modify: `docs/official-server-contract.md`（§2 執行紀錄）

**Behaviour:**

- 重跑：本任務改由 `2026-10-06-soak-rehost-plan.md` 的 R7 於 10/8 在固定 IP 主機上執行；以下為家用網路時期的原步驟。
- 10/2（五）10:30 的排程動作已自動執行：兩個 client 的 2330 tick 同時停止（§6 D2），不必重做。其餘步驟原定週五，因排程未觸發改在 10/5（一）同樣時段執行，soak 結束時間不變。
- 11:00：先查 soak 實例的 `usage`；`connections` 加 1（一個官方實例的量測值）超過 5 時，不開 harness 實例，F2 只以文件與模擬模式預設值作答並在契約文件註明。未超過時，以另一份 env（`SJ_HTTP_ADDR=127.0.0.1:18081`、`SJ_AGENT_HARNESS=all`、獨立的 `SJ_HOME_PATH`）啟動第二個官方 server 實例；執行 `p1rec orders --tag harness --only harness_probe --base-url http://127.0.0.1:18081`，錄下回應後停止該實例。soak 實例不受影響。
- 11:30：經 soak 實例本身執行 `p1rec orders --tag pre-restart --only lifecycle_futures_tmf,<週四確認會成交的一個 market_futures_tmf_* 案例>`，讓重啟前的當日委託同時有未成交與成交兩種（設計 §9.1 第 8 步要求重啟時當日已有委託）；harness 實例的委託走另一個實例，不能代替。
- 12:00（重啟排在當日主動測試的最後）：停止並重啟 soak 實例，`stream` 會記下斷線與重連；重啟後執行 `p1rec reconcile --sse-run <stream run_id> --trading-day <當日>`；明確指定日期，重啟後查無委託時才會寫出 `reconcile.json` 並回 2，而不是因推斷不出日期回 1。
- 對帳回 2 時不重試，保留 `reconcile.json` 作為 go/no-go 證據；`rest_order_count` 為 0 表示重啟後的實例查不到 11:30 的委託，即 GO 準則 5 的反證。
- 此後到下週二 09:30 不再觸碰 soak 實例。

**Numbers:**

- `18081`：Task 1 確認未被佔用。
- 一個官方實例佔 1 條連線：Task 1 與複查時 `usage.connections` 的量測值（兩次皆與舊 gateway 同時登入，合計 2）；上限 5 條：設計 §4。
- 各時間點：10/5（一）交易時段內，重啟排在最後（設計 §9.1 第 8 步、§9.2）。
- 11:30：落在 harness 實例（11:00）與重啟（12:00）之間，且在期交所日盤 08:45–13:45 內（`orders` 只在日盤下期貨單）。

**Verification:**

Run: `jq '.rest_order_count, .sse_order_count_by_client' results/p1/<當日 reconcile run>/reconcile.json`
Expected: `rest_order_count` 不小於 11:30 那次 `orders` 下的委託數，每個 client 的數字大於 0；為 0 時依上方規則判讀，不重跑。

Run: `jq '(.only_in_rest | length), (.only_in_sse | length), (.mismatched | length), ([.missing_events_by_client[] | length] | add)' results/p1/<當日 reconcile run>/reconcile.json`
Expected: 產出四個數字並抄入契約文件；GO 準則 4、5 依此判定，不要求為 0。

Run: `cd tools/p1-official-server && uv run p1rec scrub --scan-only; echo $?`
Expected: `0`，之後才 commit。

Run: `jq -c 'select(.op=="place_stock")' results/p1/*-orders-harness/rest.jsonl | wc -l`
Expected: `1`。

**Commit:**

```bash
git add docs/official-server-contract.md
git commit -m "Record multi-client, harness and restart runs"
```

---

### Task 14（live，下週二 09:30）：soak 結束與 log sweep

**狀態：** 完成，由 `2026-10-06-soak-rehost-plan.md` R8 於 10/12 執行（`9b4d0d9`）。

**Files:**
- Modify: `docs/official-server-contract.md`（新增 §4 log sweep）

**Behaviour:**

- 重跑：本任務改由 `2026-10-06-soak-rehost-plan.md` 的 R8 於 10/12 執行，log sweep 一併涵蓋家用網路時期的 server log；以下為原步驟。
- soak 分兩段 run：`20261001T040917Z-stream`（10/1 12:09 至 10/3 16:19，主機重開機中斷）與 `20261003T205132Z-stream`（10/4 04:51 起）；`stats` 兩段各跑一次，server log 涵蓋 `2026-10-01-server/` 與 `2026-10-04-server/`。Task 13 的對帳用後者。
- 09:30 `stream` 自動結束；停止官方 server；以 `p1rec stats --sse-run <stream run_id>` 涵蓋整個 soak。對帳只在下單當日做（Task 12、13）：`trades` 是否只回當日委託（§6 C7）尚未確認，跨日對帳可能把前幾日的委託誤報為遺漏。
- 官方 server log 與全部 `recorder.log` 中的每一類 WARN／ERROR／CRITICAL（依訊息樣板歸類）各記：次數、首末次時間、處置（接受並說明原因，或記為契約發現並寫明對 go/no-go 的影響）；IP 白名單的 `not allow` 為必列類別（設計 §4）。
- 從 server log 量出 session 過期的實際週期與次數，寫入契約文件，作為 P2 watchdog 依據（設計 §9.2 的 `calibrate:`）。
- 說不出原因的類別記為 P2 待辦，不標「P1 未通過驗收」，也不改 Task 15 的 go/no-go 結論（設計 §9.3）。

**Verification:**

Run: `grep -c '處置：' docs/official-server-contract.md`
Expected: 等於 §4 表中列出的類別數。

Run: `cd tools/p1-official-server && uv run p1rec scrub --scan-only; echo $?`
Expected: `0`，之後才 commit；server log 含明文帳號與對外 IP，§4 引用的 log 行須先去識別化。

**Commit:**

```bash
git add docs/official-server-contract.md
git commit -m "Classify every warning and error class from the soak"
```

---

### Task 15：語料、契約文件、go/no-go 與決策紀錄

**狀態：** 完成，結論 GO（`ec9cd86`、`4436093`、`8811e53`）。

**Files:**
- Modify: `tools/p1-official-server/fixtures.toml`、`docs/official-server-contract.md`、`ROADMAP.md`、`docs/adr/0004-order-tag-reserves-node-id.md`（`status: superseded by ADR-0005`）
- Create: `test_data/official/1.7.6/*`、`test_data/official/1.7.6/MANIFEST.toml`、`docs/adr/0005-order-tag-encoding.md`

**Behaviour:**

- 前置：Task 13 的重啟與對帳完成即可開始，不等 Task 14 的 log sweep（設計 §9.4）。
- `fixtures.toml` 為 §6 每個有證據的項目至少挑一筆；`p1rec scrub` 匯出，`--scan-only` 回 0 後才 commit。
- 契約文件 §3 依 §6 A–G 逐項作答並附 fixture 名稱或 log 行，答不出者標「未知」並寫原因；§1 差距表每列標「相同／可轉換／缺」。
- 契約文件 §5 依設計 §8 逐條判定 GO 準則 1–6，寫出結論（GO、GO＋P3 或 NO-GO），GO＋P3 時列出缺口清單。
- ADR-0005 依 §6 C 的 `custom_field` 量測定案委託標記的字元配置，取代 ADR-0004。
- `ROADMAP.md`：P1 移到「完成」並寫明結論，依結論更新 P2、P3。

**Verification:**

Run: `cd tools/p1-official-server && uv run p1rec scrub --scan-only; echo $?`
Expected: `0`。

Run: `grep -c '未作答' docs/official-server-contract.md`
Expected: `0`。

**Commit:**

```bash
git add tools/p1-official-server/fixtures.toml test_data/official docs/official-server-contract.md docs/adr ROADMAP.md
git commit -m "Publish P1 contract findings and go/no-go decision"
```
