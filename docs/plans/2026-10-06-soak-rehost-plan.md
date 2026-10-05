# P1 soak 固定 IP 主機重跑實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task.

**Goal:** 在 AWS 台北固定 IP 主機（VPS）上重跑原計畫 Task 12 soak 起點、Task 13、Task 14，並補上 VPS 執行所需的程式與維運檔案。

**Architecture:** 離線部分擴充 `p1rec`：secrets 可帶多個出口 IP、掃描擋下 IPv4 字面值、新增不建立 run 目錄的 `p1watch` 監看入口，以及 `deploy/` 下的 systemd 檔。live 部分照設計 §4 的時程在 VPS 上執行，家用主機負責輪詢告警、取回與分析。

**Tech Stack:** Python 3.12+、httpx、pytest、uv、systemd、Tailscale、rsync、官方 `shioaji` 1.7.6 CLI。

**Read first:** `docs/plans/2026-10-06-soak-rehost-design.md` 全文；原計畫 `docs/plans/2026-09-29-official-server-spike-plan.md` 的開頭、Task 12–14；`docs/official-server-contract.md` §2。

**Done signal:**

- 離線：在 `tools/p1-official-server/` 下 `uv run pytest -q` 全綠。
- **env-gated**：Task R8 的驗證通過。需要 VPS、新 API key、`~/.sinopac-p1/env` 與 10/15–10/20 的交易窗口。對應的離線演練是原計畫 Task 11 與本計畫 Task R2 對 stub server 的測試。

**執行分工：** R1–R3 為離線，交給 subagent-driven-development；R4–R8 為 live，由主 session 執行，不交給 subagent。

**工作目錄：** R1–R3 的 Files 與指令以 `tools/p1-official-server/` 為準，`docs/`、`BACKLOG.md` 與各 commit 指令以 repo 根為準。worktree 沒有 `results/` 時，先建一個指向主 checkout `results/` 的符號連結並列入 `.git/info/exclude`，否則 `scrub` 會在 worktree 另建一份對照表。

**長度說明：** 本計畫約為設計補充的 2.8 倍。設計補充只寫與上位設計不同之處，而 R7、R8 必須完整重述原計畫 Task 13、14 的行為，R2 也要定下新元件的介面；以上位設計加補充計算則在兩倍以內。

---

## 檔案結構

```
tools/p1-official-server/
  p1.toml                  # 加 [endpoints.health]、[watch]、[scrub] ipv4_allow
  tests/data/p1.toml       # 同步上述三處
  src/p1rec/config.py      # secrets 回傳多值；WatchConfig
  src/p1rec/redact.py      # redaction_values 接多值
  src/p1rec/scrub/_map.py  # build_map 接多值
  src/p1rec/scrub/_matching.py  # scan 的 IPv4 檢查
  src/p1rec/watch.py       # p1watch 入口
  deploy/                  # systemd unit、timer、host.env.example
```

家用主機端的腳本（key 輪替、密鑰搬運、輪詢告警、取回）放在 `~/.sinopac-p1/`，不進版控（設計 §5）。

---

### Task R1：多個出口 IP 與 IPv4 掃描

**Files:**
- Modify: `src/p1rec/config.py`、`src/p1rec/redact.py`、`src/p1rec/scrub/_map.py`、`src/p1rec/scrub/_matching.py`、`src/p1rec/scrub/_run.py`、`src/p1rec/cli.py`、`p1.toml`、`tests/data/p1.toml`、repo 根的 `BACKLOG.md`（P1REC-30 改 `closed @<hash>`）
- Test: `tests/test_config.py`、`tests/test_redact.py`、`tests/test_scrub.py`
- Read first: 設計 §10

**Behaviour:**

- `egress_ip` 角色的環境變數可填以逗號分隔的多個 IPv4，前後空白去除、重複者合併；任一項不是 IPv4 時 `ConfigError` 只寫變數名稱，不寫值。其他角色的值原樣保留，即使含逗號。
- 每個出口 IP 都被遮蔽（`recorder.log`），也各自登記為 `EGRESS_IP_<n>`；已在對照表中的保留原編號。
- 掃描與匯出後的檢查都回報 IPv4 字面值，類別 `IPV4`，只寫路徑與行號：四段點分十進位、每段 0–255，前後不緊鄰數字或「點加數字」（`1.2.3.4.5` 不算）。不回報 loopback、`0.0.0.0`、文件網段 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`，以及 `[scrub] ipv4_allow` 列出者；私有與 CGNAT 網段照樣回報。

**Interface:**

```python
# config.py
_LIST_ROLES: frozenset[str]  # {"egress_ip"}
def read_secret_role(cfg: P1Config, role: str) -> tuple[str, ...]: ...  # one role, same rules as read_secrets
def read_secrets(cfg: P1Config) -> dict[str, tuple[str, ...]]: ...
# ScrubConfig gains
ipv4_allow: frozenset[str]
# redact.py
def redaction_values(secrets: Mapping[str, Sequence[str]]) -> list[str]: ...
# scrub/_map.py
def build_map(secrets: Mapping[str, Sequence[str]], discovered: Mapping[str, Sequence[str]], existing: Mapping[str, str]) -> dict[str, str]: ...
# scrub/_matching.py
IPV4_CATEGORY = "IPV4"
def scan(paths: Iterable[Path], originals: Mapping[str, str], keys: Mapping[str, str] | None = None, *, ipv4_allow: Collection[str] = ()) -> list[Leak]: ...
```

```toml
[scrub]
ipv4_allow = ["7.33.0.3"]  # libsolclient version in the spike design's sources
```

**Tests:**

- `test_read_secrets_splits_egress_list` — `"203.0.113.1, 203.0.113.2,203.0.113.1"` 得到兩個值，依首次出現排序。
- `test_read_secrets_rejects_non_ipv4_egress_item` — 含非 IPv4 項目時拋 `ConfigError`，訊息含變數名稱、不含該項目。
- `test_read_secrets_keeps_comma_in_other_roles` — `ca_passwd` 的 `"a,b"` 保持一個值。
- `test_build_map_numbers_each_egress_ip` — 既有對照表已有第一個 IP 為 `EGRESS_IP_1` 時，第二個得到 `EGRESS_IP_2`。
- `test_redaction_values_cover_every_egress_ip` — 兩個 IP 都在回傳值中。
- `test_scan_flags_unlisted_ipv4` — 含一個 global IPv4（在測試內以字串組出，repo 內不留 global 字面值）與 `7.33.0.3` 的檔案，在空 allowlist 下回報兩筆 `IPV4`。
- `test_scan_ignores_loopback_doc_ranges_and_allowed` — `127.0.0.1`、`203.0.113.5`、allowlist 內的 `7.33.0.3` 不回報。
- `test_scan_ignores_longer_dotted_runs` — `1.2.3.4.5` 不回報。
- `test_scan_only_exits_2_on_ipv4_leak` — 在掃描範圍的文件加一行 global IPv4 後，`run_scrub(scan_only=True)` 回 2，log 只有路徑、行號與 `IPV4`。

**Verification:**

Run: `uv run pytest -q`
Expected: 全部 passed。

Run: `set -a; . ~/.sinopac-p1/env; set +a; uv run p1rec scrub --scan-only; echo $?`
Expected: `0`。

**Commit:**

```bash
git add tools/p1-official-server BACKLOG.md
git commit -m "Accept several egress IPs and reject IPv4 literals"
```

---

### Task R2：`p1watch` 本地監看

**Files:**
- Create: `src/p1rec/watch.py`、`tests/test_watch.py`
- Modify: `src/p1rec/config.py`、`pyproject.toml`（console script）、`p1.toml`、`tests/data/p1.toml`、`tests/stub_server.py`（`/api/v1/health`，可切換狀態與 HTTP 碼）
- Read first: 設計 §8；`src/p1rec/records.py`（`frame_of`、`recv_ts_of`、`discover_sse_files`）

**Behaviour:**

- 每次執行做完全部檢查後寫 `status.json`（覆寫，先寫暫存檔再改名）並附加一行到 `watch.jsonl`，回 0；只有無法寫入狀態時回 1。不建立 run 目錄，不印 IP、key 或帳號；密鑰只讀 `egress_ip` 一個角色（`read_secret_role`），其餘角色的變數不存在也照常執行。`--holidays` 為空字串時視為沒有休市日。
- `health_not_healthy`：`unhealthy`、HTTP 非 200 或連不上時立即成立；`degraded` 在台北時間 `maintenance_window` 內不算，窗口外連續 `degraded_after_checks` 次才成立。`last_session_recovery_error` 與 `last_maintenance_error` 只記「有／無」，不複製內文。
- `relogin_failed`：從上次的位移讀 `--server-log` 新增的位元組，任一行含 `failure_patterns` 之一即成立並記行數；檔案路徑改變或變短時從頭讀。
- `quotes_stale`：交易時段內，最新 stream run 的每個 client 的 `sse_<client>_<data_stream>.jsonl` 中，最後一個非心跳 frame 的收到時間距今超過 `quotes_stale_s` 即成立。交易時段：營業日的 `day_session`；營業日當晚起的 `night_session` 跨到次日。營業日為週一至週五且不在 `--holidays`。從檔尾往前讀，遇到早於門檻的紀錄即停，不讀整個檔案。
- `service_down`：`services` 中任一 unit 的 `ActiveState` 不是 `active`，或 `NRestarts` 比上次大。
- `resources`：`--disk-path` 所在檔案系統剩餘低於 `disk_free_min_gib`、`MemAvailable` 低於 `mem_available_min_mib`，或 swap 使用量高於 `swap_used_max_mib`；並記各 unit 的 `MemoryCurrent`。
- `egress_changed`：距上次查詢滿 `egress_check_interval_s` 才查 `egress_url`；結果不在 `[secrets] egress_ip` 的值之中即成立，只記相同或不同；查詢失敗記為未知，不成立。
- 每類異常記首次成立時間，持續成立時沿用；恢復後從 `anomalies` 移除。

**Interface:**

```python
# watch.py
ANOMALY_CLASSES = ("health_not_healthy", "relogin_failed", "quotes_stale", "service_down", "resources", "egress_changed")

@dataclass(frozen=True)
class Anomaly:
    name: str
    since: datetime
    detail: Mapping[str, object]  # counts, ages, flags only

class Probes(Protocol):
    def health(self) -> tuple[int | None, Mapping[str, object] | None]: ...  # (HTTP status, body)
    def unit(self, name: str) -> Mapping[str, str]: ...  # ActiveState, NRestarts, MemoryCurrent
    def egress_ip(self) -> str | None: ...
    def disk_free_bytes(self, path: Path) -> int: ...
    def meminfo(self) -> Mapping[str, int]: ...  # bytes

def session_open(now: datetime, holidays: frozenset[date], cfg: WatchConfig) -> bool: ...
def latest_data_frame_ns(path: Path, heartbeat_event: str, not_before_ns: int) -> int | None: ...
def run_watch(cfg: P1Config, egress_ips: Sequence[str], state_dir: Path, server_log: Path, holidays: frozenset[date], disk_path: Path, now: datetime, probes: Probes) -> int: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

```text
p1watch --config p1.toml --state-dir DIR --server-log PATH --holidays YYYY-MM-DD[,...] --disk-path PATH
```

```toml
[endpoints.health]
method = "GET"
path = "/api/v1/health"

[watch]
health_op = "health"
healthy_status = "healthy"
maintenance_window = ["08:00", "08:45"]
degraded_after_checks = 2
data_stream = "data"
quotes_stale_s = 300
day_session = ["08:46", "13:44"]
night_session = ["15:01", "04:59"]
failure_patterns = ["not allow", "Daily maintenance failed", "Daily maintenance exhausted"]
services = ["p1-server.service", "p1-stream.service"]
disk_free_min_gib = 5
mem_available_min_mib = 100
swap_used_max_mib = 256
egress_check_interval_s = 3600
egress_url = "https://checkip.amazonaws.com"
```

`status.json`：`checked_at`（UTC ISO）、`ok`、`anomalies`（`name`、`since`、`detail`）、`health`（`status`、`token_stale`、`session_recovering`、`session_recovery_attempts`、`next_maintenance`、`token_expires_in_seconds`、`has_recovery_error`）、`units`（每個 unit 的 `active`、`n_restarts`、`memory_bytes`）、`stream_run`、`data_frame_age_s`（client → 秒）、`egress_matches`（`true`／`false`／`null`）。狀態檔 `state.json` 存位移、計數與上次查詢時間。

**Tests:**

- `test_session_open_day_and_night` — 10/15（四）09:00 與 23:00、10/16 03:00、10/17（六）03:00 為開；10/15 14:30、10/17 10:00、10/19（一）03:00，以及 `holidays` 含 10/9 時的 10/9 10:00 為關。
- `test_latest_data_frame_skips_heartbeats` — tick 之後接心跳，回 tick 的收到時間。
- `test_latest_data_frame_none_when_only_heartbeats_since_cutoff` — 門檻內只有心跳時回 `None`，且不讀到門檻之前的位元組以外。
- `test_health_unhealthy_is_immediate` — 503 加 `unhealthy` 一次即成立。
- `test_health_degraded_tolerated_in_maintenance_window` — 08:20 `degraded` 不成立；14:00 連續兩次才成立。
- `test_status_never_copies_recovery_error_text` — `last_session_recovery_error` 含 `ip: 203.0.113.9 not allow` 時，`status.json` 與 `watch.jsonl` 都不含 `203.0.113.9`。
- `test_failure_patterns_counted_once` — 第一次執行計到新行，第二次沒有新行時不成立，檔案變短時從頭讀。
- `test_service_down_on_restart_count_increase` — `NRestarts` 由 0 變 1 時成立。
- `test_egress_checked_once_per_interval` — 間隔內第二次執行不呼叫 `egress_ip`；不在清單時成立。
- `test_main_against_stub_server` — 以 subprocess 執行 `p1watch` 對 stub server，回 0 並寫出 `status.json`。

**Numbers:**

- `maintenance_window` 08:00–08:45：每日維護在 08:12–08:30 內（契約文件 §2），token 約 24 小時到期，維護時間逐日浮動，前一日較早的登入可能在今日維護前到期而短暫 `degraded`。`calibrate:` 依 R5 試跑觀察。
- `degraded_after_checks` 2：timer 5 分鐘一次（設計 §8），兩次即 10 分鐘。
- `quotes_stale_s` 300：夜盤每個 client 約 26 萬筆 tick（契約文件 §2），遠高於每 5 分鐘一筆。`calibrate:` R5 試跑。
- `day_session`、`night_session`：期交所 08:45–13:45、15:00–次日 05:00（上位設計 §4），兩端各內縮 1 分鐘以避開開收盤邊界。`calibrate:` R5 試跑。
- `failure_patterns`：10/5 家用 server log 的重新登入失敗訊息。
- `disk_free_min_gib` 5：每交易日約 2 GB 錄製量（家用 run `20261001T040917Z-stream` 推算）的兩天多。`mem_available_min_mib` 100、`swap_used_max_mib` 256：`calibrate:` R5 試跑。
- `egress_check_interval_s` 3600：設計 §8。

**Verification:**

Run: `uv run pytest -q`
Expected: 全部 passed。

**Commit:**

```bash
git add tools/p1-official-server
git commit -m "Add a local soak watch that writes status files"
```

---

### Task R3：systemd 檔

**Files:**
- Create: `deploy/p1-server.service`、`deploy/p1-stream.service`、`deploy/p1-watch.service`、`deploy/p1-watch.timer`、`deploy/host.env.example`
- Read first: 設計 §6、§7

**Behaviour:**

- unit 檔以 `@HOME@` 代表 `trader` 的家目錄，安裝時以 `sed` 代入；檔內不寫 IP、主機名或使用者家目錄的實際路徑。
- 三個 service 皆 `User=trader`、`UMask=0077`、`WorkingDirectory=~`、`EnvironmentFile=@HOME@/.sinopac-p1/host.env`；server 與 stream 另讀 `@HOME@/.sinopac-p1/secrets.env`，watch 不讀。
- `p1-server.service`：以 `/bin/sh -c` 執行 `shioaji server start --no-open`，stdout 與 stderr 附加到 `$P1_SERVER_LOG`，啟動前建立其目錄；`Restart=on-failure`、`RestartSec=30`、`OOMScoreAdjust=-500`、`WantedBy=multi-user.target`。
- `p1-stream.service`：`uv run --project sinopac-nt-community/tools/p1-official-server p1rec stream --config sinopac-nt-community/tools/p1-official-server/p1.toml --clients a,b --until ${P1_STREAM_UNTIL}`；`Wants=` 與 `After=p1-server.service`，不用 `Requires=`；`Restart=on-failure`、`RestartSec=60`、`KillSignal=SIGTERM`、`TimeoutStopSec=120`、`OOMScoreAdjust=-500`、`WantedBy=multi-user.target`。
- `p1-watch.service` 為 `Type=oneshot`，執行 `p1watch`，參數取自 `host.env`；`p1-watch.timer` 為 `OnCalendar=*:0/5`、`AccuracySec=30s`。
- `host.env.example` 列出 `SJ_CA_PATH`、`SJ_HOME_PATH`、`SJ_HTTP_ADDR`、`P1_EGRESS_IP`、`P1_SERVER_LOG`、`P1_STREAM_UNTIL`、`P1_MARKET_HOLIDAYS`，IP 一律用文件網段。

**Numbers:**

- `RestartSec` 30、60，`OOMScoreAdjust` -500：設計 §7。
- `TimeoutStopSec` 120：`SSE_READ_TIMEOUT_S` 90（`sse.py`）加 `retry_s` 1（`p1.toml`），再留約 30 秒。
- 5 分鐘：設計 §8。

**Verification:**

Run: `d=$(mktemp -d); for f in deploy/*.service deploy/*.timer; do sed "s|@HOME@|$HOME|g" "$f" > "$d/$(basename "$f")"; done; systemd-analyze verify "$d"/*.service "$d"/*.timer; echo $?`
Expected: `0`，沒有語法錯誤。

Run: `grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}' deploy/* | grep -vE '127\.0\.0\.1|203\.0\.113\.'`
Expected: 無輸出。

**Commit:**

```bash
git add tools/p1-official-server/deploy
git commit -m "Add systemd units for the soak host"
```

---

### Task R4（live，10/6 延遲量測收尾後）：VPS 準備、密鑰與第一次登入

**Files:**
- Modify: `docs/official-server-contract.md`（§2 新增「固定 IP 主機」小節）
- 不進版控：家用主機 `~/.sinopac-p1/` 下的 `rotate-key.py`、`push-secrets.sh`、`p1-poll.sh`、`p1-pull.sh` 與其 user timer；VPS 的 `host.env`、`secrets.env`、`ca.pfx`；雲端主機維運 repo 的 `BACKLOG.md`

**Behaviour:**

- 等 VPS 上的延遲量測收尾通知才開始。
- VPS：時區 `Asia/Taipei`；2 GiB swapfile 寫入 `/etc/fstab`、`vm.swappiness=10`；Tailscale 以官方套件庫安裝並 `--accept-dns=false`，節點授權由使用者操作；UFW 加 `allow in on tailscale0 to any port 22`；家用主機為 tailnet 位址另立 SSH 設定，接受 host key 前先比對與建機紀錄的 ED25519 指紋一致。
- 程式碼：家用主機 `git bundle create` 本分支，scp 到 VPS 後 clone 到 `~/sinopac-nt-community`；`uv sync` 並跑 `uv run pytest -q`；`uv tool install shioaji==1.7.6`，wheel sha256 與 PyPI 公布值相同。
- `~/.sinopac-p1/` 與 `results/` 為 0700。
- `rotate-key.py` 加 `--only-p1`（只改 `~/.sinopac-p1/env`）與 `--egress-ip`（逗號分隔的清單，直接寫入 `P1_EGRESS_IP`，不查目前出口）；清單為 `egress-ip-history.tsv` 的全部家用 IP 加 VPS 固定 IP，由腳本讀檔產生，不印出。
- `push-secrets.sh`：從家用主機的 env 取四個密鑰變數與 `SJ_CA_PATH` 指向的檔案，經 tailnet 以 `install -m 600` 寫成 VPS 的 `secrets.env` 與 `ca.pfx`，輸出只有兩端 sha256 前 8 碼是否一致。
- **停下來交給使用者**：在永豐建立白名單只含固定 IP 的新 key；執行 `rotate-key.py --only-p1 --egress-ip …`；執行 `push-secrets.sh`。
- `uv` 與 `uv tool` 安裝的 `shioaji` 都在 `~/.local/bin`；`~`、`/home` 與 `/` 都沒有 `.env`，因為官方 CLI 會從工作目錄往上讀 `.env`，多出的檔案可能補上 `SJ_PRODUCTION` 之類的變數。
- 由 `host.env.example` 以同一個 `sed` 代入 `@HOME@` 後填值寫成 `host.env`（`P1_STREAM_UNTIL=2026-10-14T20:00:00+08:00`、`P1_SERVER_LOG` 為當日的 `<日期>-server/server.log`、`P1_MARKET_HOLIDAYS=2026-10-09,2026-10-10`）；R3 的 unit 以 `sed` 代入後安裝並 `daemon-reload`。
- **停下來問使用者**後才 `systemctl start p1-server`：health 為 Simulation、`usage.connections` 記下、出口等於固定 IP；log 出現 `not allow` 即以 `systemctl stop p1-stream p1-server` 兩個一起停止，不重試；只停 server 時，錄製器的下一次啟動會經 `Wants=` 把它拉起來。
- 通過後 enable 並啟動 `p1-stream` 與 `p1-watch.timer`，確認 `status.json` 的 `ok`。
- 家用主機：`p1-poll.sh` 每 10 分鐘讀 `status.json`，新的異常類別與恢復各發一則 Telegram（token 在執行時讀取），`checked_at` 超過 15 分鐘未更新發 `watch_stale`，連續 3 次 SSH 失敗發 `unreachable`，14:15 發日摘要（各類次數、磁碟、`MemoryCurrent`、以 `aws cloudwatch get-metric-statistics` 查的 `CPUCreditBalance`）；`p1-pull.sh` 於 14:10 與 05:30 執行 `rsync -a --append-verify -z` 拉回 `results/p1/`。
- 雲端主機維運 repo 的 `BACKLOG.md` 加一列：Free Plan 在 credits 用完或到期日自動關閉，長期使用前決定升級或遷移；本機 commit。
- 契約文件 §2 記錄：主機規格、服務方式、新 key 只綁固定 IP、第一次登入結果與 `connections`；不寫 IP、主機名或 instance ID。

**Numbers:**

- 2 GiB、`vm.swappiness` 10、10 分鐘、3 次、14:10、05:30：設計 §2、§8、§9。
- 15 分鐘：timer 5 分鐘一次，三次未更新即視為監看停止。
- 試跑結束 10/14 20:00：設計 §7。

**Verification:**

Run（VPS）：`systemctl is-active p1-server p1-stream p1-watch.timer; stat -c '%a %n' ~/.sinopac-p1 ~/.sinopac-p1/* ~/sinopac-nt-community/results`
Expected: 三個 `active`；目錄 `700`、檔案 `600`。

Run（VPS）：`curl -s http://127.0.0.1:18080/api/v1/info | jq .simulation; jq '.ok, [.anomalies[].name]' ~/.sinopac-p1/watch/status.json`
Expected: `true`；`true` 與 `[]`。

Run：`cd tools/p1-official-server && uv run p1rec scrub --scan-only; echo $?`
Expected: `0`，之後才 commit。

**Commit:**

```bash
git add docs/official-server-contract.md
git commit -m "Record the fixed-IP host setup and first login"
```

---

### Task R5（live，10/7–10/14）：試跑

**Files:**
- Modify: `docs/official-server-contract.md`（§2「固定 IP 主機」小節）、`tools/p1-official-server/p1.toml` 與 `tests/data/p1.toml`（`[watch]` 校準、近月合約與價平 TXO、`futures_margin_twd`）

**Behaviour:**

- 完成設計 §11.1 第 1–6 項，各記結果與時間：
  1. 非交易時段 `sudo reboot`，兩個服務自行啟動、錄製器開新 run，Telegram 收到 `service_down` 與恢復。
  2. 非交易時段 `systemctl stop p1-stream` 一次，收到通知後啟動；`p1-poll.sh` 指向錯誤目標一次，收到 `unreachable`。
  3. health 的 `next_maintenance` 與 server log 的 `Starting daily maintenance` 時間落在台北 08:12–08:30。
  4. 拉回後兩端 sha256 一致；一致的試跑 run 從 VPS 刪除。
  5. 每日 14:15 摘要的 `MemoryCurrent` 與 `CPUCreditBalance` 抄入契約文件。
  6. R1 已完成，`scrub --scan-only` 回 0。
- 依試跑紀錄校準 `[watch]` 中標 `calibrate:` 的值；改動 `p1.toml` 時同步 `tests/data/p1.toml` 並跑 `uv run pytest -q`。
- 10/14：
  - 依 R2 數字判斷機型：`MemoryCurrent` 每日持續增加，或 `CPUCreditBalance` 連日下降至 0，即**停下來問使用者**是否換 `t3.small`（stop、改機型、start，EIP 不變）。
  - 依當日合約清單校準近月期貨（J6 於 10/21 到期，窗口在其前結束）與價平 TXO 買權、賣權各一檔；`futures_margin_twd` 填期交所當日公布值；同步 `[stream]`、`[history]` 與 `scenarios.toml`。
  - `P1_MARKET_HOLIDAYS` 清空（窗口內沒有休市日）。
- 10/8 晚上若沒有夜盤，`quotes_stale` 的一次誤報記為已知並寫入契約文件。
- 10/14 20:00 錄製器到期結束後執行 `systemctl disable p1-stream`，到 R6 再啟用；過了 `--until` 的錄製器每次啟動都會失敗並重啟。

**Verification:**

Run：`grep -c '固定 IP 主機' docs/official-server-contract.md`
Expected: 大於 0，且該小節列出 §11.1 第 1–6 項的結果。

Run：`cd tools/p1-official-server && uv run pytest -q && uv run p1rec scrub --scan-only; echo $?`
Expected: 全部 passed，`0`。

**Commit:**

```bash
git add tools/p1-official-server/p1.toml tools/p1-official-server/tests/data/p1.toml tools/p1-official-server/scenarios.toml docs/official-server-contract.md
git commit -m "Calibrate the soak watch and contracts from the trial week"
```

---

### Task R6（live，10/15 07:30）：soak 起點

取代原計畫 Task 12 的 07:30–08:30 步驟；orders、history 與校準不重跑。

**Files:**
- Modify: `docs/official-server-contract.md`（§2）

**Behaviour:**

- 07:30：shell 先 `umask 077`；`timedatectl` 與 `chronyc tracking` 存入 `results/p1/2026-10-15-ntp.txt`；`host.env` 改 `P1_SERVER_LOG=…/results/p1/2026-10-15-server/server.log`、`P1_STREAM_UNTIL=2026-10-20T09:30:00+08:00`，並在同一個指令內 `systemctl restart p1-server`，監看才不會盯著沒人寫的舊 log。
- `p1rec self-test-interlock` 回 0；`p1rec preflight` 回 0，或只因資金比對回 2（P1REC-26，記錄後繼續）；其他非 0 即停止。
- 08:00：`systemctl enable --now p1-stream`，記下新的 stream run ID，即 soak 起點。
- 08:30 後（每日維護之後）：再跑一次 `p1rec preflight`，判讀同上。
- 此後到 10/20 09:30，除 R7 外不碰兩個服務。

**Verification:**

Run（VPS）：`for f in results/p1/<stream run>/sse_*_data.jsonl; do printf '%s ' "$f"; jq -c 'select(.kind=="frame")' "$f" | wc -l; done`
Expected: 每個 client 皆大於 0，09:00 後重跑數字增加。

**Commit:**

```bash
git add docs/official-server-contract.md
git commit -m "Start the fixed-IP soak"
```

---

### Task R7（live，10/19）：多 client、harness 實例、重啟與對帳

取代原計畫 Task 13；步驟與判讀規則不變，只改為在 VPS 上執行。

**Files:**
- Modify: `docs/official-server-contract.md`（§2）

**Behaviour:**

- 10:30 前**停下來問使用者**：本日會開第二個登入（harness 實例）並下模擬單。
- 11:00：查 soak 實例的 `usage`；`connections` 加 1 超過 5 時不開 harness 實例，F2 只以文件與模擬模式預設值作答。未超過時以 `~/.sinopac-p1/harness.env`（`SJ_HTTP_ADDR=127.0.0.1:18081`、`SJ_AGENT_HARNESS=all`、`SJ_HOME_PATH=~/.sinopac-p1/home-harness`）加 `secrets.env`，經 `systemd-run --unit=p1-harness` 啟動；執行 `p1rec orders --tag harness --only harness_probe --base-url http://127.0.0.1:18081`；停止該 unit。
- 11:30：`p1rec orders --tag pre-restart --only lifecycle_futures_tmf,<10/1 確認會成交的一個 market_futures_tmf_* 案例>`。
- 12:00：`systemctl restart p1-server`，`p1-stream` 不動；重啟後 `p1rec reconcile --sse-run <stream run_id> --trading-day 2026-10-19`。
- 對帳回 2 時不重試，保留 `reconcile.json`；`rest_order_count` 為 0 即 GO 準則 5 的反證。

**Numbers:**

- `18081`、連線上限 5、各時間點：原計畫 Task 13 Numbers。

**Verification:**

Run：`jq '.rest_order_count, .sse_order_count_by_client' results/p1/<當日 reconcile run>/reconcile.json`
Expected: `rest_order_count` 不小於 11:30 下的委託數，每個 client 大於 0；為 0 時依上方規則判讀，不重跑。

Run：`jq '(.only_in_rest | length), (.only_in_sse | length), (.mismatched | length), ([.missing_events_by_client[] | length] | add)' results/p1/<當日 reconcile run>/reconcile.json`
Expected: 四個數字抄入契約文件；GO 準則 4、5 依此判定。

Run：`jq -c 'select(.op=="place_stock")' results/p1/*-orders-harness/rest.jsonl | wc -l`
Expected: `1`。

Run：`cd tools/p1-official-server && uv run p1rec scrub --scan-only; echo $?`
Expected: `0`，之後才 commit。

**Commit:**

```bash
git add docs/official-server-contract.md
git commit -m "Record multi-client, harness and restart runs on the fixed-IP host"
```

---

### Task R8（live，10/20 09:30）：soak 結束與 log sweep

取代原計畫 Task 14。

**Files:**
- Modify: `docs/official-server-contract.md`（新增 §4 log sweep）、`docs/plans/2026-09-29-official-server-spike-plan.md`（Task 12–14 的狀態）

**Behaviour:**

- 09:30 `p1-stream` 自行結束；`systemctl disable --now p1-server p1-stream p1-watch.timer`；最後一次 `p1-pull.sh`，sha256 一致後才從 VPS 刪除正式 soak 的資料；家用主機停用 `p1-poll` 與 `p1-pull` 的 timer。
- 家用主機以 `p1rec stats --sse-run <run_id>` 涵蓋 R6 起的每一段 stream run。
- log sweep 範圍：`2026-10-15-server/server.log`、正式 soak 期間的全部 `recorder.log`、`journalctl -u p1-server -u p1-stream`（重啟、OOM）、`watch.jsonl` 的異常類別，以及 `2026-10-01-server/` 與 `2026-10-04-server/` 的家用 server log。依訊息樣板歸類，每一類記次數、首末次時間與處置；`not allow` 必列。
- 從正式 soak 的 server log 量出 session 過期的實際週期與次數，寫入契約文件（P2 watchdog 依據）。
- 任何一類說不出原因時，契約文件標明「P1 未通過驗收」並停止。
- 原計畫 Task 12–14 標明由本計畫完成並附 commit。
- 完成後回報 run ID 與窗口、驗收數字、log sweep 結論、未決問題與 commit。

**Verification:**

Run：`grep -c '處置：' docs/official-server-contract.md`
Expected: 等於 §4 表中列出的類別數。

Run：`cd tools/p1-official-server && uv run p1rec scrub --scan-only; echo $?`
Expected: `0`，之後才 commit；§4 引用的 log 行先去識別化。

**Commit:**

```bash
git add docs/official-server-contract.md docs/plans/2026-09-29-official-server-spike-plan.md
git commit -m "Classify every warning and error class from the fixed-IP soak"
```
