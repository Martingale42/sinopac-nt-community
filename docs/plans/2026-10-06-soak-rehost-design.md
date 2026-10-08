# P1 soak 改在固定 IP 主機重跑：設計補充

- **日期**：2026-10-06
- **狀態**：已逐段核可，待書面 review
- **分支**：`p1-official-server-spike`
- **上位設計**：`docs/plans/2026-09-29-official-server-spike-design.md`（本文只寫與它不同之處）

## 1. 要解決的問題

ROADMAP「P1 官方 server 驗證 spike」的 soak 在家用網路執行，10/5 對外 IP 五天內第二次變動，每日重新登入被 API key 白名單擋下，soak 暫停（`docs/official-server-contract.md` §2「10/5 對外 IP 變動、重新登入失敗與 soak 暫停」）。本設計把計畫的 Task 12 soak 起點、Task 13、Task 14 搬到有固定公網 IPv4 的 AWS 台北主機（下稱 VPS）重跑。

VPS 從第一次登入起的連續錄製就是 soak，不另開正式窗口：上位設計 §8 的 GO 準則只剩第 5 點（重啟後取回當日委託）沒有證據，它靠 Task 13 的一次重啟回答，拉長窗口補不上。長時間 soak 由 P2 的 adapter 在同一台主機上承擔。

## 2. 已定的決定

| 項目 | 決定 |
|---|---|
| 窗口 | soak 從 10/6（二）16:44 第一次登入起至 10/12（一）09:30；Task 13 在 10/8（四）；Task 14 在 10/12 |
| 主機規格 | 維持 `t3.micro`，加 2 GiB swap；`resources` 告警持續出現時才停下來問使用者是否換 `t3.small` |
| API key | 新建一把，白名單只填 VPS 的固定 IP；舊 gateway 的設定不動 |
| 密鑰 | 家用主機的 `~/.sinopac-p1/env` 先更新，再由使用者執行搬運腳本；VPS 上分 `secrets.env` 與 `host.env` |
| 管理面 | Tailscale 為主，原本只放行家用 IP 的 SSH 規則保留為備援 |
| 告警 | VPS 本地檢查，家用主機輪詢並以既有的 Telegram bot 通知 |
| scrub | 修 P1REC-30，另加通用 IPv4 掃描 |
| 程式修改範圍 | 只做搬遷必要的；其餘 P1REC 待 soak 出問題再議 |
| 取回 | 每日兩次 rsync 拉回家用主機；`stats`、`scrub`、log sweep 在家用主機跑 |

## 3. 範圍

**範圍內**：VPS 準備與部署、密鑰搬遷、systemd 服務、監看與告警、結果取回、P1REC-30 與 IPv4 掃描、Task 12 soak 起點、Task 13、Task 14。

**範圍外**：Task 12 的 `orders`、`history` 與校準（10/1 已完成）；其餘 P1REC 項目；正式模式；VPS 長期使用的方案與費用。10/1–10/5 的錄製保留為家用網路的證據，不重錄。

## 4. 時程

| 時間 | 內容 |
|---|---|
| 10/6 延遲量測收尾後 | VPS 準備（§5）、使用者建立 key 與搬密鑰（§6）、第一次登入，soak 自此開始 |
| 10/6–10/12 09:30 | soak：連續錄製，除 Task 13 外不碰兩個服務；完成 §11.1 runbook；離線完成 §10 |
| 10/8 11:00–12:00 | Task 13 |
| 10/12 09:30 | soak 結束，停止服務；Task 14 log sweep |
| 10/13 | 上位計畫 Task 15：go/no-go |

證交所行事曆：10/9 補假、10/10 與 10/26 休市，已查證。

## 5. VPS 準備與部署

1. 時區設為 `Asia/Taipei`，與家用主機一致，`recorder.log` 的無時區時間戳才能與家用時期的紀錄對照。官方 server 的 log 以「Taiwan time」標示下次維護時間，推論它不依系統時區排程；soak 期間以 health 的 `next_maintenance` 驗證。
2. 2 GiB swapfile，`vm.swappiness=10`。
3. Tailscale：`--accept-dns=false`，不當 exit node、不廣播路由，對永豐的出口仍是固定 IP；UFW 只在 `tailscale0` 介面加放 22 埠。節點授權由使用者操作。
4. `uv tool install shioaji==1.7.6`，wheel 的 sha256 與 PyPI 公布值比對。
5. 程式碼以 `git bundle` 搬到 VPS 再 clone，不 push；`uv sync` 後跑一次離線 `uv run pytest -q` 作為環境冒煙測試。
6. `~/.sinopac-p1/` 與 `results/` 為 0700，其內檔案 0600；服務設 `UMask=0077`。

VPS 端的 systemd unit 與監看腳本放在 `tools/p1-official-server/deploy/`，不寫 IP 或主機名，主機相依的值一律由 `host.env` 提供。家用主機端的腳本（key 輪替、密鑰搬運、輪詢告警、取回）碰到密鑰與私人基礎設施，放在 `~/.sinopac-p1/`，不進版控。

## 6. 密鑰

1. 使用者在永豐建立新 key，白名單只填 VPS 的固定 IP。
2. 使用者在家用主機執行改寫版的 key 輪替腳本：只改 `~/.sinopac-p1/env`，以 `getpass` 輸入，`P1_EGRESS_IP` 直接指定為固定 IP（§10 完成後改為清單）。scrub 對照表是累積的，只能在一處維護，因此家用主機也要有新 key；這把 key 只能從固定 IP 登入，外洩的影響有限。
3. 使用者執行密鑰搬運腳本：從家用主機的 env 取 `SJ_API_KEY`、`SJ_SEC_KEY`、`SJ_CA_PASSWD`、`P1_PERSON_ID` 與 `SJ_CA_PATH` 指向的 `.pfx`，經 tailnet 以 `install -m 600` 寫成 VPS 的 `secrets.env` 與 `ca.pfx`，最後只比對兩端 sha256 前 8 碼。
4. VPS 的 `host.env` 寫 `SJ_CA_PATH`、`SJ_HOME_PATH`、`SJ_HTTP_ADDR=127.0.0.1:18080`、`P1_EGRESS_IP`、`P1_SERVER_LOG`、`P1_STREAM_UNTIL`、休市日清單，不含密鑰。
5. 不用 systemd credentials：VPS 沒有 TPM，加密用的 host key 在同一顆磁碟上，官方 server 又只讀環境變數，保護效果與 0600 檔案相同。

任何步驟都只印「相同／不同」、類別、數量或長度，不印值。

## 7. 服務

兩個系統服務，`User=trader`、`UMask=0077`、`WantedBy=multi-user.target`，以 `EnvironmentFile=` 讀 `secrets.env` 與 `host.env`。

**`p1-server.service`**：`shioaji server start --no-open`，stdout 與 stderr 附加到 `P1_SERVER_LOG`；`Restart=on-failure`、`RestartSec=30`、`OOMScoreAdjust=-500`。程序結束才會重啟；10/5 那種登入失敗但程序仍在的狀態只告警，由使用者決定處置。

**`p1-stream.service`**：`p1rec stream --clients a,b --until "$P1_STREAM_UNTIL"`；`Wants=` 與 `After=` 指向 server，不用 `Requires=`，Task 13 重啟 server 時錄製器才不會跟著停並能記下斷線與重連；`Restart=on-failure`、`RestartSec=60`，到 `--until` 以 0 結束時不重啟；`TimeoutStopSec=120`，涵蓋 SSE 讀取逾時加重試間隔。每次重啟產生新 run，Task 14 本就處理多段 run。

**結束**：soak 沿用第一次登入時的 server log（`2026-10-06-server/`）與錄製器設定，不切換。`P1_STREAM_UNTIL` 維持 `2026-10-14T20:00:00+08:00`，因為改它要重啟錄製器而開出新 run；10/12 09:30 改以 `systemctl disable --now` 停止三個服務，soak 的 run 在此結束。

**臨時程序**：Task 13 的 harness 實例以 `systemd-run --unit=p1-harness` 啟動（`127.0.0.1:18081`、`SJ_AGENT_HARNESS=all`、獨立 `SJ_HOME_PATH`），用完即停；`preflight`、`orders`、`reconcile` 經 SSH 手動執行。

## 8. 監看與告警

**VPS 本地**：`p1-watch.timer` 每 5 分鐘觸發一個 oneshot，結果寫入 `~/.sinopac-p1/watch/status.json`（最新）與 `watch.jsonl`（每次一行），不含 IP 或帳號。

| 類別 | 條件 |
|---|---|
| `health_not_healthy` | `/api/v1/health` 不是 `healthy`，記下 `token_stale`、`session_recovering` 與重試次數。`calibrate:` 每日維護前後可容忍的時間由 soak 前幾天的觀察決定 |
| `relogin_failed` | server log 新增 `not allow` 或重新登入失敗的行，樣板取自 10/5 的 log |
| `quotes_stale` | 交易時段內任一 client 的行情檔太久沒有非心跳 frame，休市日由 `host.env` 提供。`calibrate:` 門檻先用 5 分鐘 |
| `service_down` | 任一服務非 active，或 `NRestarts` 增加 |
| `resources` | 磁碟剩餘低於 5 GiB、可用記憶體低於 100 MiB 或 swap 用量超過 256 MiB；`calibrate:` 依 soak 前幾天調整 |
| `egress_changed` | 出口 IP 與 `P1_EGRESS_IP` 不同，每小時查一次，只記相同或不同 |

`quotes_stale` 是為 10/5 的故障而設：當時 `/api/v1/info` 照常回 200、SSE 沒斷，但 08:35 之後只剩心跳。

**家用主機**：user systemd timer 每 10 分鐘經 tailnet 讀 `status.json`；新的異常類別與恢復各發一則 Telegram，連續 3 次 SSH 不通發 `unreachable`；每日 14:15 發摘要（各類次數、磁碟、RSS、`CPUCreditBalance`）。bot token 由腳本在執行時讀取，訊息只寫類別與時間。

## 9. 取回與分析分工

- 每日 14:10 與 05:30，家用主機以 `rsync -a --append-verify -z` 經 tailnet 把 VPS 的 `results/p1/` 拉回家用主機的 `results/p1/`；錄製檔只會附加，每次只傳增量。
- soak 的資料到 Task 14 結束、兩端 sha256 一致後才從 VPS 刪除。
- 需要即時 REST 的 `preflight`、`orders`、Task 13 的 `reconcile` 在 VPS 執行；`stats`、`scrub`、log sweep 在家用主機以拉回的副本執行，只維護一份對照表，也不佔 VPS 記憶體。

## 10. scrub 修改

- **P1REC-30**：`[secrets] egress_ip` 指向的環境變數接受以逗號分隔的多個 IP，每個都登記為 `EGRESS_IP` 類別；家用主機填入歷來的家用 IP 與固定 IP。
- **IPv4 掃描**：`scrub --scan-only` 在掃描範圍內遇到任何 `ipaddress` 判為 global 的 IPv4 字面值即失敗，列在新增的 `[scrub] ipv4_allow` 者除外；未曾登記的 IP 因此也進不了 repo。現有 repo 只有上位設計 §12 的四段版本號需列入；測試用的文件網段與私有網段不是 global，不受影響。

兩者先寫測試，`uv run pytest -q` 全綠才進 Task 14。

## 11. 驗收

### 11.1 Runbook（Task 14 前全部通過）

1. health 的 `next_maintenance` 與 server log 的實際維護時間落在台北時間 08:12–08:30。
2. 取回後兩端 sha256 一致。
3. 每日記錄 RSS 與 `CPUCreditBalance`；`resources` 告警持續出現時停下來問使用者是否換機型。
4. §10 的測試全綠，`scrub --scan-only` 回 0。
5. Task 13 下單前 `self-test-interlock` 回 0；`preflight` 只因資金比對回 2 時屬 P1REC-26（模擬模式資金查詢回 0），記錄後繼續；其他非 0 一律停止。

重開機與停掉錄製器的告警演練不在 P1 做：在窗口內演練會切斷 soak，留給 P2 在同一台主機上的 soak。

### 11.2 Soak

窗口 10/6（二）16:44 第一次登入至 10/12（一）09:30，期間除 Task 13 外不碰兩個服務；結束點落在連假後第一次維護、重新登入與開盤之後。推導沿用上位設計 §9.2：

| 週期現象 | 週期 | 窗口內次數 |
|---|---|---|
| 每日維護 | 每日 08:12–08:30 | 6 |
| session 過期與重新登入 | 新 token 在下次維護時到期，維護時重新登入 | 6 |
| 合約更新 | 營業日 08:00、14:00 | 5 |
| 跨午夜 | 每日 | 6 |
| 期貨夜盤跨日 | 營業日 15:00–次日 05:00 | 2–3（二、三；四晚上未查證） |
| 完整交易日 | 每個營業日 | 2（三、四） |
| 連假閒置與開盤 | 每次休市 | 1（10/9–10/11 休市，10/12 開盤） |

- Task 13 重啟 server 後，每日維護仍在同一時刻，因為每次啟動算出的 jitter 都相同（契約文件 §2 的 10/8 紀錄）。
- 10/1–10/5 家用網路的紀錄是同一批現象的第二份證據；第二次連假與跨官方版本的長時間行為留給 P2 的 soak。

### 11.3 Log sweep（Task 14）

範圍：soak 窗口的 server log（`2026-10-06-server/`）與 `recorder.log`、兩個服務的 journald（重啟、OOM）、`watch.jsonl` 的異常類別，以及 10/1–10/5 家用網路的 server log（待歸類的三類與必列的 `not allow` 在其中）。每一類記次數、首末次時間與處置；任何一類說不出原因，P1 即不通過驗收。

## 12. 須由使用者執行或核准的步驟

- 建立永豐 key、輸入任何密鑰、執行 key 輪替與密鑰搬運腳本、授權 Tailscale 節點。
- 從 VPS 第一次登入永豐，以及 Task 13 的任何下單。
- 換機型或任何雲端資源變更。
- push、開 PR、merge。

## 13. 風險

- VPS 在 10/12 09:30 前失效：每日兩次取回，最多失去半天資料；已錄的部分照常作為證據，沒觀察到的週期現象在契約文件標「未知」並寫明原因，不重排窗口。
- 連假與開盤只觀察一次，下單路徑只有 10/1 與 10/8 兩天的證據；第二次觀察由 P2 的 soak 補上。
- 10/8 晚上是否有期貨夜盤未查證，影響 `quotes_stale` 的判讀與夜盤跨日的次數。
- 官方 server 不依系統時區排維護是推論，以 §5 第 1 點與 §11.1 第 1 點驗證。

## 14. 捨棄的方案

- **tmux 執行**：10/3 主機重開機時，tmux 內的程序全部停止，已證實不可行。
- **Docker**：在 1 GiB 主機上多一層 daemon 與映像，對要驗證的事沒有幫助。
- **只靠放行家用 IP 的 SSH 規則**：家用 IP 五天換兩次，每換一次就失去監看與 Task 13 的操作路徑，直到規則更新。
- **systemd credentials**：理由見 §6 第 5 點。
- **10/15–10/20 另開正式 soak**：§11.2 的每種週期現象在 10/12 前都會發生，多等一週只換到第二次觀察。官方 server 閉源，觀察結果只能轉成 adapter 的需求；P2 的 adapter 在同一台主機上跑 soak，能同時驗證兩者。
