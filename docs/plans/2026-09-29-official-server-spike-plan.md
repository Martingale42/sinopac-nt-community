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
    analysis.py    # 對帳、client 覆蓋、串流統計
    history.py     # 歷史資料查詢與額度量測
    scrub.py       # 去識別化、語料匯出、掃描
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
- Create: `src/p1rec/scenarios.py`、`src/p1rec/prices.py`、`src/p1rec/preflight.py`
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`
- Test: `tests/test_scenarios.py`、`tests/test_prices.py`、`tests/test_preflight.py`

**Behaviour:**

- `load_scenarios` 讀 `scenarios.toml`；案例 id 重複、`steps` 含未知動詞、`expect_fill` 案例用了 `exceed_funds` 時拋 `ConfigError`。
- 價格參照 `limit_down`、`limit_up`、`reference` 取自合約回應；`limit_down_plus_tick` 為跌停價加一個升降單位；`far_below` 在股票與零股為跌停價，在期貨為「參考價 × 0.95 向下取到升降單位」；`far_below_plus_tick` 為 `far_below` 加一個升降單位；升降單位依市場、價格區間與 `etf_codes` 查表。
- `preflight`：連鎖檢查；查 `accounts`、`balance`、`margin`、`usage`、`ca_status`；查 `p1.toml` 所列全部商品的合約；計算資金需求並比對可用額度。
- 資金需求以單一部位計（設計 §4）：股票為 `max(會成交股票案例的 張數 × 1000 × 漲停價)`，期貨為 `max(會成交期貨案例的 口數 × 該商品原始保證金)`。
- 不足、或找不到可用額度欄位時記 ERROR、以 2 結束；不論成敗都寫 `preflight.json`，鍵為 `funds_ok`、`required`、`available`、`connections`、`accounts`（帳戶類型 → `signed`）、`limits`（商品 → 漲停、跌停、參考價）。
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
    etf_codes: tuple[str, ...]

@dataclass(frozen=True)
class FundsConfig:
    stock_available_pointer: str
    futures_available_pointer: str
    futures_margin_twd: dict[str, Decimal]  # 商品代號 -> 每口原始保證金

@dataclass(frozen=True)
class FundsRequirement:
    stock_twd: Decimal
    futures_margin_twd: Decimal

def load_scenarios(path: Path) -> list[Case]: ...
def tick_size(market: Market, price: Decimal, *, is_etf: bool) -> Decimal: ...
def resolve_price(ref: PriceRef, limits: ContractLimits, market: Market, *, is_etf: bool) -> Decimal: ...
def required_funds(cases: Sequence[Case], limits: Mapping[str, ContractLimits],
                   margins_twd: Mapping[str, Decimal]) -> FundsRequirement: ...
def run_preflight(cfg: P1Config, scenarios: Path, run_dir: Path) -> int: ...
```

**Tests:**

- `test_duplicate_case_id_rejected`、`test_unknown_step_rejected` — 各拋 `ConfigError`。
- `test_stock_tick_bands` — 9.99→0.01、10→0.05、50→0.1、100→0.5、500→1、1000→5。
- `test_etf_tick_bands` — 49.99→0.01、50→0.05。
- `test_futures_tick_is_one_point` — TMF 任意價格回 1。
- `test_limit_down_plus_tick` — 跌停 9.95 的一般股回 9.96。
- `test_far_below_futures` — TMF 參考價 47781 時回 45391（47781 × 0.95 = 45391.95 向下取到 1 點）；一般股回跌停價。
- `test_required_funds_single_position` — 兩筆會成交的股票案例取較大者，不加總。
- `test_insufficient_exits_2_and_writes_json` — 回 2，`preflight.json` 存在且 `funds_ok` 為 `false`。
- `test_missing_available_field_exits_2` — 找不到可用額度欄位時回 2。
- `test_unsigned_futures_account_warns` — `F` 帳戶 `signed: false` 且劇本含期貨案例時記 WARN、結束碼仍為 0。

**Numbers:**

- 1000 股／張：證交所整股交易單位。
- 股票升降單位（<10：0.01；10–<50：0.05；50–<100：0.1；100–<500：0.5；500–<1000：1；≥1000：5）與 ETF（<50：0.01；≥50：0.05）：證交所升降單位規定。
- 期貨升降單位 1 點：期交所 TXF／MXF／TMF 契約規格。
- 期貨遠價 0.95：期貨 API 測試（契約文件 §2）中以跌停價下單被拒、以參考價 × 0.95 下單被接受。
- `futures_margin_twd`：`calibrate:` Task 12 執行 preflight 前，依期交所當日公告的原始保證金填入。

**Verification:**

Run: `uv run pytest -q tests/test_scenarios.py tests/test_prices.py tests/test_preflight.py`
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
- 之後只有「主 recorder」（第一個 client 的第一條串流）重連時，才重送 `startup_requests` 與目前有效的全部訂閱，並記一次 INFO；其他 recorder 重連不重送。訂閱失敗記 ERROR 並繼續其餘訂閱。
- `[[stream.action]]` 在指定時間送出 `subscribe` 或 `unsubscribe` 並更新有效訂閱集合；啟動時已過期的動作記 WARN 並跳過。
- 到 `--until` 或收到 SIGINT／SIGTERM 時，所有 client 寫出 `stopped` 後以 0 結束。對每個 recorder 的 join 最多等 `sse.SSE_READ_TIMEOUT_S + retry_s`（讀取逾時為三個心跳間隔，契約文件 §2；recorder 的等待從它發出連線起算，stop 可能晚於連線發出，因此加一個 `retry_s` 作為餘裕）；逾時仍未結束的 recorder（例如卡在等待 response header）記 WARN 並列出名稱，結束碼仍為 0。

**Interface:**

```python
@dataclass(frozen=True)
class Instrument:
    code: str
    exchange: str        # TSE | OTC | TAIFEX
    security_type: str   # STK | FUT | OPT
    quote_types: tuple[str, ...]  # Tick | BidAsk | Quote
    intraday_odd: bool

@dataclass(frozen=True)
class StreamAction:
    at: datetime
    op: Literal["subscribe", "unsubscribe"]
    code: str
    quote_type: str

@dataclass(frozen=True)
class StreamConfig:
    streams: dict[str, Endpoint]        # 串流名 -> SSE 端點
    subscribe: Endpoint
    unsubscribe: Endpoint
    subscribe_body: str                 # JSON 範本
    unsubscribe_body: str
    startup_requests: tuple[tuple[str, str], ...]  # (邏輯操作, JSON 本體)
    retry_s: float
    instruments: tuple[Instrument, ...]
    actions: tuple[StreamAction, ...]

def run_stream(cfg: P1Config, clients: list[str], until: datetime, run_dir: Path) -> int: ...
```

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
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`
- Test: `tests/test_orders.py`

**Behaviour:**

- `p1rec orders [--only <id,...>] [--base-url <url>]` 依 `scenarios.toml` 順序執行；`--base-url` 覆蓋 `base_url`。
- 期貨案例只在期交所日盤（08:45–13:45，台北時間）執行：合約檔的漲跌停價只對日盤有效，夜盤的漲跌幅以當日一般交易時段結算價為基準（推論自期貨 API 測試，契約文件 §2），在日盤以外遇到期貨案例一律拒絕並記 ERROR，不送任何請求。
- 每個 `place`、`update_price`、`update_qty`、`cancel`、`close` 呼叫前執行連鎖檢查；中止時以 3 結束，不再送任何請求。
- `account` 欄位帶 `accounts` 回傳的完整帳戶物件：股票與零股用 `S`、期貨用 `F`（只帶帳號時 `account_type` 預設為 `S`，契約文件 §2）。
- 請求本體由 `body_templates` 代入 `{code}`、`{action}`、`{price}`、`{quantity}`、`{price_type}`、`{order_type}`、`{custom_field}`、`{trade_id}`；股票與零股用 `place_stock`，期貨用 `place_futures`。
- 價格由 `resolve_price` 算出；`quantity = "exceed_funds"` 時張數為「可用股票餘額 ÷（1000 × 跌停價）」無條件進位再加 1，價格固定為跌停價，被接受也不會成交。
- 步驟之間等待 `step_wait_s`。
- `expect_fill` 案例：每 `fill_poll_s` 查 `trades` 直到成交或超過 `fill_timeout_s`；成交後送出反向、以漲停或跌停價構成的可成交限價單 `close`，同樣等到成交。逾時未成交時記 CRITICAL（含案例 id 與「可能留有部位，需人工檢查」），不送其他平倉單，以 2 結束。
- 前一個 `close` 確認成交前，不開始下一個 `expect_fill` 案例。
- 被拒的請求照樣記錄並繼續下一個案例；拒單本身是要錄的語料。

**Interface:**

```python
@dataclass(frozen=True)
class OrdersConfig:
    body_templates: dict[str, str]  # 邏輯操作 -> JSON 範本，取自 Task 1
    trade_id_pointer: str           # 下單回應中委託識別的 JSON 指標
    filled_pointer: str             # trades 回應中判定已成交的 JSON 指標
    step_wait_s: float
    fill_poll_s: float
    fill_timeout_s: float

def run_orders(cfg: P1Config, cases: Sequence[Case], run_dir: Path,
               only: set[str] | None) -> int: ...
```

`scenarios.toml` 內容（設計 §4、§6 C）：

| 群組 | 商品 | 案例 |
|---|---|---|
| 遠價 LMT | 2890、00981A（零股）、TMF | LMT × ROD／IOC／FOK，`far_below`，不成交 |
| 生命週期 | 2890、TMF | 2 單位、`far_below`；`update_price:far_below_plus_tick` → `update_qty:1` → `cancel` |
| 市價類 | 2890（整股 1 張）、00981A（零股 1 股）、TMF（1 口） | MKT、MKP × ROD／IOC／FOK，`expect_fill = true`，逐筆開倉即平倉 |
| 資金不足 | 2890 | `exceed_funds`、LMT ROD，之後 `cancel` |
| 委託標記 | 2890 | 遠價 LMT ROD；`custom_field` 分別為 6 字元 base62、7 字元 base62、ADR-0004 候選格式（1 字元 node + 5 字元雜湊）；之後 `cancel` |
| harness 探測 | 2890 | id `harness_probe`，遠價 LMT ROD，之後 `cancel` |

**Tests:**

- `test_interlock_before_every_mutation` — 假 server 第二次查詢模式時回 Production，劇本在第二個請求前中止並回 3。
- `test_exceed_funds_quantity` — 餘額 100000、跌停 20 時張數為 6。
- `test_fill_then_close_sequence` — 成交後送出反向平倉；下一個 `expect_fill` 案例在平倉成交後才開始。
- `test_fill_timeout_logs_critical_and_exits_2` — 逾時時不送平倉、記 CRITICAL、回 2。
- `test_rejection_recorded_and_continues` — 422 被記錄，下一個案例照常執行。
- `test_only_filter` — `--only harness_probe` 只執行該案例。

**Numbers:**

- 生命週期案例用 2 單位：台灣市場的改量只能減少，至少 2 單位才能減 1 後仍保有委託以測刪單。
- 市價類案例用 1 張、1 股、1 口：各市場的最小交易單位，資金需求最低。
- `step_wait_s`、`fill_poll_s`、`fill_timeout_s`：`calibrate:` Task 12 第一個生命週期案例量出「送出到回報出現」的延遲後設定，量測值記入契約文件。
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
- Create: `src/p1rec/analysis.py`
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`
- Test: `tests/test_analysis.py`

**Behaviour:**

- `p1rec reconcile --sse-run <run_id> [--rest-run <run_id>]`：未給 `--rest-run` 時，連鎖檢查後即時查 `trades` 並落地；給了則用該 run 最後一次 `trades` 回應。
- 依 `EventFields` 從各 client 的 SSE 檔挑出委託與成交事件，整理為「委託識別 → 最終狀態、累計成交量」。
- `reconcile.json`：`only_in_rest`、`only_in_sse`、`mismatched`、`per_client_keys`（各 client 看到的委託識別，設計 §6 D）。前三者皆空時回 0，否則回 2。
- `p1rec stats --sse-run <run_id>` 寫 `stats.json`：每個 client 各事件名稱的數量；每秒 frame 數最高的前 10 秒與其時間；心跳事件（名稱為 `heartbeat_event`）間隔的最小、中位數、最大值；相鄰 frame 間隔超過 `gap_threshold_s` 的清單；`exchange_ts_pointer` 有設定時，收到時間減交易所時間的 p50、p99、最大值。

**Interface:**

```python
@dataclass(frozen=True)
class EventFields:  # 事件名稱與 JSON 指標，取自 Task 12 的真實事件
    order_event_names: tuple[str, ...]
    deal_event_names: tuple[str, ...]
    order_key: str
    order_status: str
    deal_key: str
    deal_qty: str
    rest_list: str
    rest_key: str
    rest_status: str
    rest_filled_qty: str
    exchange_ts_pointer: str | None
    exchange_ts_format: str | None   # strptime 格式；時區固定為 Asia/Taipei
    heartbeat_event: str             # 官方 1.7.6 為 "heartbeat"
    gap_threshold_s: float

@dataclass(frozen=True)
class OrderView:
    key: str
    status: str
    filled_qty: Decimal

@dataclass(frozen=True)
class ReconcileReport:
    only_in_rest: list[str]
    only_in_sse: list[str]
    mismatched: list[tuple[OrderView, OrderView]]
    per_client_keys: dict[str, list[str]]

def orders_from_sse(paths: Mapping[str, Path], fields: EventFields) -> dict[str, dict[str, OrderView]]: ...
def orders_from_rest(payload: object, fields: EventFields) -> dict[str, OrderView]: ...
def reconcile(sse: Mapping[str, dict[str, OrderView]], rest: dict[str, OrderView]) -> ReconcileReport: ...
def stream_stats(paths: Mapping[str, Path], fields: EventFields) -> dict[str, object]: ...
```

**Tests:**

- `test_all_match_exit_0` — 兩個 client 與 REST 一致時三份清單皆空。
- `test_missing_in_sse_reported` — REST 多一筆時列入 `only_in_rest` 並回 2。
- `test_status_mismatch_reported` — SSE 為 PartFilled、REST 為 Filled 時列入 `mismatched`。
- `test_client_coverage_difference` — client b 缺一筆時 `per_client_keys` 可看出差集。
- `test_deals_accumulate_qty` — 同一委託兩筆成交數量加總。
- `test_stats_peak_second_and_gaps` — 合成資料中某秒 50 個 frame、某處間隔 90 秒時，`stats.json` 的尖峰與間隔清單正確。

**Numbers:**

- 尖峰取前 10 秒：報告篇幅上的選擇，用來看開盤尖峰的形狀。
- `gap_threshold_s`：`calibrate:` 取 Task 12 首日量到的心跳最大間隔的兩倍，寫入 `p1.toml`。
- `EventFields` 其餘指標：`calibrate:` 由 Task 12 第一批真實事件決定，Task 13 對帳前寫入 `p1.toml`。

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

`p1.toml` 的查詢：2330 前一交易日 ticks；2330 前一交易日 09:00–13:30 kbars；TMF 前一交易日 kbars；錄製商品全部的 snapshots。

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
- Create: `src/p1rec/scrub.py`、`tools/p1-official-server/fixtures.toml`（空清單）
- Modify: `src/p1rec/config.py`、`src/p1rec/cli.py`
- Test: `tests/test_scrub.py`

**Behaviour:**

- 替換對照的來源：`secret_env` 的全部密鑰（含 API key 前 10 字元與對外 IP）、以 `account_id_pointers` 從 `accounts` 回應找出的帳號、以 `ca_pointers` 從 `ca_status` 回應找出的憑證內容。
- 佔位字串依「類別 + 首次出現順序」決定（例如 `ACCOUNT_1`）；同一原始值永遠得到同一佔位字串；對照表存 `results/p1/scrub-map.json`（不進版控），重跑時沿用。
- 替換由長到短，嵌在較長字串中的值（例如 clientname 內的 key 前綴）也要替換。
- 原始值的每種寫法都要處理：原文、JSON 跳脫後的形式（含 `\uXXXX`），以及 `*_b64` 欄位解碼後的內容；帳戶物件的 `username` 也屬於要替換的值。
- `p1rec scrub` 依 `fixtures.toml` 從 `results/` 取出指定檔案與行，替換後寫入 `test_data/official/1.7.6/`，並產生 `MANIFEST.toml`（每筆：檔名、來源 run_id、來源檔、行號、錄製時間、商品、對應的 §6 項目）。
- `p1rec scrub --scan-only` 掃描 `test_data/official/` 與 `docs/official-server-contract.md`：發現任何原始值時列出「檔案:行號:類別」（不印原始值）並回 2，否則回 0。

**Interface:**

```python
@dataclass(frozen=True)
class ScrubConfig:
    account_id_pointers: tuple[str, ...]
    ca_pointers: tuple[str, ...]
    map_path: Path
    selection: Path  # fixtures.toml；每筆 name、run_id、source、lines、checklist、instrument

@dataclass(frozen=True)
class Leak:
    path: Path
    line: int
    kind: str

def build_map(secrets: Mapping[str, str], discovered: Mapping[str, Sequence[str]],
              existing: Mapping[str, str]) -> dict[str, str]: ...
def scrub_text(text: str, mapping: Mapping[str, str]) -> str: ...
def export_fixtures(cfg: P1Config, mapping: Mapping[str, str]) -> list[Path]: ...
def scan(paths: Iterable[Path], originals: Mapping[str, str]) -> list[Leak]: ...
```

**Tests:**

- `test_deterministic_placeholders` — 同一值兩次建表得到同一佔位字串，且沿用既有對照表。
- `test_longest_first_replacement` — 前綴與完整 key 同時出現時，完整 key 整段替換。
- `test_embedded_value_replaced` — `PYAPI/<前綴>/0929/...` 中的前綴被替換。
- `test_scan_reports_without_value` — 有洩漏時回報檔案與行號，輸出不含原始值。
- `test_scan_clean_returns_zero` — 無洩漏時回 0。
- `test_manifest_entry_per_fixture` — 匯出兩筆時 `MANIFEST.toml` 有兩筆，欄位齊全。

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

- `stub_server.py` 以 `ThreadingHTTPServer` 在隨機埠提供全部邏輯操作：`mode`（可切換 Simulation／Production）、帳戶、合約、餘額、保證金、usage、`ca_status`、`trades`、下單／改單／刪單、歷史查詢，以及可推送行情、委託、成交事件並能中途斷線的 SSE。欄位名稱沿用 Task 1 `openapi.json` 的對應 schema，資料為虛構。
- `test_rehearsal.py` 以 subprocess 呼叫 `p1rec` console script，用指向假 server 的測試設定依序執行：`self-test-interlock` → `preflight` → `stream`（背景）→ `orders` → 中途切斷 SSE → `history` → `reconcile` → `stats` → `scrub` 匯出兩筆 → `scrub --scan-only`。

**Tests:**

- `test_full_pipeline_against_stub` — 每一步結束碼符合預期，`reconcile.json` 三份清單皆空，`stats.json` 記到斷線間隔，`MANIFEST.toml` 有兩筆，掃描回 0。
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

**Files:**
- Modify: `tools/p1-official-server/p1.toml`、`docs/official-server-contract.md`（§2 執行紀錄）

**Behaviour:**

- 排程：選證交所行事曆上週四至下週二皆為營業日（週末除外）的第一週；10/8 那週不行（10/9 補假，依設計 §9.2 推論，排程時查證）。
- 前一個交易日：依當日合約清單校準 `p1.toml` 的近月期貨代號（TXFJ6、MXFJ6、TMFJ6 於 2026-10-21 到期，soak 跨過該日就改用下一個月份）與近月價平 TXO 買權、賣權各一檔，同步更新 `[stream]` 商品、`[history]` 的 snapshots 查詢與 `scenarios.toml`。
- 07:30 前：校時狀態（`timedatectl` 與 `chronyc tracking` 或等效指令）存入 `results/p1/<日期>-ntp.txt`；填入 `futures_margin_twd`；啟動官方 server 並依 Task 1 記下的方式把 log 導向 `results/p1/<日期>-server/server.log`（放在自己的 run 目錄內，Task 15 才能以 `fixtures.toml` 挑選）；`p1rec self-test-interlock` 回 0；`p1rec preflight` 回 0，回 2 即停止並交由使用者取得模擬資金。
- 08:00 前：`p1rec stream --clients a,b --until <下週二 09:30 +08:00>` 於 tmux 啟動，即為 soak 起點；預先排入週五 10:30 取消 2330 tick 訂閱的動作（§6 D2：訂閱為全域，看兩個 client 是否同時停止收到 2330）。
- 09:00 後：`p1rec orders --only <第一個生命週期案例>`，量出延遲後填入 `step_wait_s`、`fill_poll_s`、`fill_timeout_s`，再以 `p1rec orders` 跑其餘案例；從第一批真實事件填入 `EventFields`。同時確認四件事：IOC／FOK 未成交時交易所自動刪單的事件形狀（是否以 Cancel 事件送出）、部分成交後刪單時 REST 的狀態字串、成交事件的 `deal_key` 與 `deal_qty`、改量後刪單時 `cancel_quantity` 是累計值還是單次值；與 `derive_status` 的規則不符時先修正，再進 Task 13。
- 13:45 後：`p1rec reconcile --sse-run <stream run_id>` 對當日委託對帳。
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

**Commit:**

```bash
git add tools/p1-official-server/p1.toml docs/official-server-contract.md
git commit -m "Calibrate recorder settings from the first live session"
```

---

### Task 13（live，週五）：多 client、harness 實例、重啟與對帳

**Files:**
- Modify: `docs/official-server-contract.md`（§2 執行紀錄）

**Behaviour:**

- 10:30 的排程動作自動執行；之後確認 client a 的 2330 tick 仍持續進來。
- 11:00：先查 soak 實例的 `usage`；`connections` 加 1（一個官方實例的量測值）超過 5 時，不開 harness 實例，F2 只以文件與模擬模式預設值作答並在契約文件註明。未超過時，以另一份 env（`SJ_HTTP_ADDR=127.0.0.1:18081`、`SJ_AGENT_HARNESS=all`、獨立的 `SJ_HOME_PATH`）啟動第二個官方 server 實例；執行 `p1rec orders --tag harness --only harness_probe --base-url http://127.0.0.1:18081`，錄下回應後停止該實例。soak 實例不受影響。
- 12:00（重啟排在當日主動測試的最後）：停止並重啟 soak 實例，`stream` 會記下斷線與重連；重啟後執行 `p1rec reconcile --sse-run <stream run_id>`。
- 對帳回 2 時不重試，保留 `reconcile.json` 作為 go/no-go 證據。
- 此後到下週二 09:30 不再觸碰 soak 實例。

**Numbers:**

- `18081`：Task 1 確認未被佔用。
- 一個官方實例佔 1 條連線：Task 1 與複查時 `usage.connections` 的量測值（兩次皆與舊 gateway 同時登入，合計 2）；上限 5 條：設計 §4。
- 各時間點：週五交易時段內，重啟排在最後（設計 §9.1 第 8 步、§9.2）。

**Verification:**

Run: `jq '(.only_in_rest | length), (.only_in_sse | length), (.mismatched | length)' results/p1/<當日 reconcile run>/reconcile.json`
Expected: 產出三個數字並抄入契約文件；GO 準則 4、5 依此判定，不要求為 0。

Run: `jq -c 'select(.op=="place_stock")' results/p1/*-orders-harness/rest.jsonl | wc -l`
Expected: `1`。

**Commit:**

```bash
git add docs/official-server-contract.md
git commit -m "Record multi-client, harness and restart runs"
```

---

### Task 14（live，下週二 09:30）：soak 結束與 log sweep

**Files:**
- Modify: `docs/official-server-contract.md`（新增 §4 log sweep）

**Behaviour:**

- 09:30 `stream` 自動結束；停止官方 server；以 `p1rec stats --sse-run <stream run_id>` 涵蓋整個 soak。對帳只在下單當日做（Task 12、13）：`trades` 是否只回當日委託（§6 C7）尚未確認，跨日對帳可能把前幾日的委託誤報為遺漏。
- 官方 server log 與全部 `recorder.log` 中的每一類 WARN／ERROR／CRITICAL（依訊息樣板歸類）各記：次數、首末次時間、處置（接受並說明原因，或記為契約發現並寫明對 go/no-go 的影響）；IP 白名單的 `not allow` 為必列類別（設計 §4）。
- 從 server log 量出 session 過期的實際週期與次數，寫入契約文件，作為 P2 watchdog 依據（設計 §9.2 的 `calibrate:`）。
- 任何一類說不出原因時，契約文件標明「P1 未通過驗收」並停止。

**Verification:**

Run: `grep -c '處置：' docs/official-server-contract.md`
Expected: 等於 §4 表中列出的類別數。

**Commit:**

```bash
git add docs/official-server-contract.md
git commit -m "Classify every warning and error class from the soak"
```

---

### Task 15：語料、契約文件、go/no-go 與決策紀錄

**Files:**
- Modify: `tools/p1-official-server/fixtures.toml`、`docs/official-server-contract.md`、`ROADMAP.md`、`docs/adr/0004-order-tag-reserves-node-id.md`（`status: superseded by ADR-0005`）
- Create: `test_data/official/1.7.6/*`、`test_data/official/1.7.6/MANIFEST.toml`、`docs/adr/0005-order-tag-encoding.md`

**Behaviour:**

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
