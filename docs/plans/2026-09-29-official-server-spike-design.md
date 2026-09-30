# P1：官方 shioaji server 驗證 spike 設計

- **日期**：2026-09-29
- **狀態**：設計已逐段核可，待書面 review
- **分支**：`docs/official-server-spike-design`
- **子專案**：P1（整體拆分見 `ROADMAP.md`）

## 1. 要回答的問題

觸發這整個計畫的是兩個已記錄的故障：

- adapter v0.1.2 在 NT develop 上跑不起來：develop 已移除 v1 Cython 層（`6aa9e9799c`）與 PyCapsule 橋接（`21b242323f`），`execution.py` 等 Python 半部與現行 pyo3 擴充做法都不成立。
- 舊 gateway 停在 shioaji 1.3.2（官方已到 1.7.6），且部署中的容器自 2026-09 中起重啟迴圈（另案處理，不屬本設計）。

P1 是 spike，只回答一個問題：**NT adapter 能不能直接接官方 server 1.7.6？能的話，契約是什麼？** 答案是 P2（adapter v2 化）設計的輸入。

## 2. 已定的跨子專案決定

| ADR | 決定 |
|---|---|
| [0001](../adr/0001-build-on-official-shioaji-server.md) | 以官方 server 為基礎，不自行重寫 SDK |
| [0002](../adr/0002-rust-first-adapter.md) | adapter 以 Rust 為主，Python 版延後 |
| [0003](../adr/0003-track-crates-io-nautilus-releases.md) | 追 crates.io 最新 nautilus-* 發行版，develop 只做預警 |
| [0004](../adr/0004-order-tag-reserves-node-id.md) | 委託標記保留 node 識別位 |

## 3. 範圍

**範圍內**

1. 以模擬模式啟動官方 server，用正式帳戶的 API key 與 CA 完成登入與 CA 啟用。
2. adapter 現行契約（16 列 endpoint／訊息）逐列對應官方 API 的差距表。
3. 真實 payload 語料：行情、委託回報、成交回報、REST 回應。
4. 行為量測（§6 A–G）。
5. `custom_field` 在官方 server 上的往返、長度上限與字元集，作為 ADR-0004 定案的依據。

**範圍外**

- 任何 Rust adapter 程式碼（屬 P2）。
- 正式模式與任何真實委託（含零股）。
- `shioaji-data` CLI、universe 腳本、cron 與舊 gateway 的修復。
- NT 版本相關工作。

## 4. 環境與前置

**安裝**：`uv tool install shioaji==1.7.6`，P1 全程只用這一版；語料目錄帶版本號。

**設定與密鑰**：新建 `~/.sinopac-p1/env`（權限 0600），不進 repo：

| 變數 | 值 |
|---|---|
| `SJ_API_KEY` / `SJ_SEC_KEY` | 沿用正式帳戶的 key |
| `SJ_CA_PATH` | 本機 `.pfx` 憑證的實際路徑（舊 `.env` 的 `CA_PATH` 已失效，不可沿用） |
| `SJ_CA_PASSWD` | CA 密碼 |
| `SJ_HOME_PATH` | `~/.sinopac-p1/home`，隔離 token pool、合約與快取 |
| `SJ_HTTP_ADDR` | `127.0.0.1:18080` |
| `SJ_PRODUCTION` | 不設定 |

舊 gateway 的 `~/.shioaji-server/.env` 不動。

**模式連鎖檢查**：錄製器在啟動時與每張單送出前向官方 server 查詢目前模式，不是 Simulation 就中止並記 ERROR；啟動官方 server 的指令固定不帶 `--production`。

**連線額度**：永豐每人上限 5 條連線。官方 server 一個實例佔 1 條（Task 1 與複查以 usage 量得），舊 gateway 登入時 1 條；總數維持不超過 5，因此 harness 第二實例只在額度足夠時啟動。

**IP 白名單**：永豐 API key 綁定對外 IP 白名單，對外 IP 改變時登入回 400 `ip: ... not allow`。官方 server 必須在白名單內的主機上執行；`preflight` 登入失敗時先比對目前對外 IP 與白名單，此錯誤類別在 log sweep 中須有明確處置。

**錄製商品**

| 類別 | 商品 |
|---|---|
| 股票 | 2330、0050、2890、00981A、00631L |
| 期貨 | 近月 TXF、MXF、TMF（微台） |
| 選擇權 | 近月價平附近 TXO 買權與賣權各一檔 |
| 盤中零股 | 00981A、00631L（模擬模式照錄） |

近月契約的確切代號以官方 server 回傳的合約清單為準。

**模擬資金**：模擬模式的資產查詢走 `api/v1/paper/*`，官方執行檔內找不到儲值或重設資金的 endpoint，因此：

1. 第 0 步先查模擬帳戶的股票餘額與期貨保證金，結果存為 fixture。
2. 錄製器依劇本算出最壞情況資金需求；不足就不下任何單，記 ERROR 後停下，由使用者取得資金。`calibrate:` 各商品保證金以第 0 步實查為準。
3. 委託生命週期一律用不會成交的遠價限價單錄製。會成交的委託只下在兩檔商品上：最便宜的一張股票與一口 TMF；市價類組合都在這兩檔上逐筆來回，開倉後立即平倉，同一時間最多持有一個部位，資金需求因此以單一部位計。
4. 刻意送一張超過餘額的委託，錄下資金不足的拒單 payload。

**時段**：證交所 09:00–13:30（試撮 08:30–09:00，13:25–13:30 為收盤集合競價，會成交的股票劇本須在 13:25 前結束），期交所日盤 08:45–13:45、夜盤 15:00–次日 05:00。下單劇本排在對應時段內。

## 5. 錄製器

方案：黑箱錄製器（捨棄的方案見 §11）。

- **位置**：`tools/p1-official-server/`，自帶獨立的 `pyproject.toml`（套件 `p1rec`，`requires-python >= 3.12`）與 `p1.toml`、`scenarios.toml`；以 `uv run --project tools/p1-official-server p1rec <子命令>` 執行，不動 adapter 的 maturin `pyproject.toml`。
- **子命令**：

  | 子命令 | 作用 |
  |---|---|
  | `self-test-interlock` | 餵入偽造的 Production 回應，確認錄製器中止 |
  | `preflight` | 第 0 步：模式檢查、資金盤點、額度檢查、連線數 |
  | `stream` | 開 N 個 SSE client，原樣錄下每個 frame |
  | `orders` | 依 `scenarios.toml` 的劇本下模擬單，錄 REST 請求與回應 |
  | `reconcile` | 官方 server 重啟後以 REST 拉當日委託與成交，對照 SSE 紀錄 |
  | `stats` | 串流統計：事件數、每秒尖峰、心跳間隔、斷線缺口、延遲分位數 |
  | `history` | 歷史 ticks／kbars／snapshots 查詢，前後以 usage 量額度消耗 |
  | `scrub` | 去識別化並掃描要 commit 的檔案 |

- **輸出**：`results/p1/<run_id>/` 下的 `sse_<client>.jsonl`、`rest.jsonl`、`server.log`、`recorder.log`。每行 JSONL 含收到時間（奈秒）、client 名稱、種類與原始內容；原始位元組不經解析改寫。`results/` 加入 `.gitignore`。
- **規範**：型別標註、TOML 設定、明確輸入輸出路徑、`logging` 做診斷、不以 `print` 輸出結果、密鑰只從環境變數讀取且不寫入任何 log。

## 6. 量測清單

每項要有答案與證據（fixture 檔名或 log 行）；答不出來標「未知」並寫原因。

**A. 契約對應**

- 從執行中的 server 取得完整 OpenAPI 規格，存為 `test_data/official/1.7.6/openapi.json`。
- adapter 現行契約 16 列逐列標為「相同／可轉換／缺」。

**B. 行情**

- SSE 訂閱流程、事件類型、frame 格式與心跳間隔。
- 斷線後能否以 `Last-Event-ID` 從斷點接續。
- 五檔深度的檔數、時間戳格式與時區（舊 gateway 的 HTTP 是 UTC 奈秒、WebSocket 是台北本地字串，兩者不一致）。
- 試撮行情是否帶標記。
- 盤中零股與期貨夜盤行情的格式。
- 09:00 開盤尖峰的每秒訊息量，以及收到時間減交易所時間的差距；量測前先記錄本機 NTP 狀態。

**C. 委託與成交**

- 新單、改價、改量、刪單的同步回應與非同步事件順序。
- ROD、IOC、FOK 與 LMT、MKT、MKP 的每個合法組合，在股票、零股、期貨各錄一輪；不合法組合（例如零股市價單）各錄一次拒單回應。
- `trade_id`、`ordno`、`seqno` 在下單回應、委託回報、成交回報之間的對應。
- `custom_field` 送 6 與 7 字元時的行為（截斷或報錯）、base62 字元集是否可用、回報是否原樣帶回。
- 股票成交數量單位是張還是股（舊 gateway 以未驗證的假設乘 1000）。
- 資金不足拒單 payload，以及 IOC／FOK 未成交時的取消事件。
- 官方 server 重啟後，查詢當日委託能否拿回完整狀態與成交。

**D. 多 client**

- 兩個 SSE client 並存時，client X 下的單，client Y 是否收到回報。
- 同一檔行情兩個 client 都訂閱時，其中一個取消訂閱是否影響另一個。

**E. 韌性**

- 官方 server 重啟時 SSE client 看到什麼、訂閱是否需要重做。
- session 過期時官方 server 是否自動重登、SSE 是否中斷。
- 每日 08:00 與 14:00 前後的合約更新事件格式。
- 不刻意切斷券商連線：執行主機是多人共用，主機層網路隔離會影響他人；只記錄 soak 期間自然發生的斷線。

**F. 限制與權限**

- 不刻意觸發永豐速率上限（超限後果未知），只記錄文件上限與實際遇到的錯誤。
- `SJ_AGENT_HARNESS` 的預設模式；另以 `SJ_AGENT_HARNESS=all` 在模擬模式跑一次，看未經審批的下單回應。
- usage 查詢的連線數與每日資料量額度用量。
- 模擬模式下 CA 是否真的簽章（1.3.2 在模擬模式以 mock 取代 CA）。
- 官方 server HTTP 端有無驗證機制，以及 CORS 預設開啟的影響。

**G. 歷史資料**

- 單日 ticks、kbars（是否僅 1 分 K）、snapshots 的格式與各自消耗的每日額度。

## 7. 產出

1. `docs/official-server-contract.md`：差距表與 §6 各項答案及證據。
2. `test_data/official/1.7.6/`：`openapi.json`、挑選過的 fixture（`<組別>_<情境>.{json,sse}`）、`MANIFEST.toml`（每筆記來源 run_id、錄製時間、商品、對應清單項目）。
3. `results/p1/<run_id>/`（不進版控）：原始錄製、server log、錄製器 log。
4. `tools/p1-official-server/`：錄製器套件 `p1rec`、`p1.toml` 與 `scenarios.toml`。
5. go/no-go 結論寫入 `ROADMAP.md` 與契約文件；`custom_field` 編碼以新 ADR 定案。

**去識別化**：repo 是公開的。帳號、身分證字號、CA 憑證內容、clientname 內的 API key 前綴以確定性替換（同值同佔位字串，保留 ID 之間的對應）；替換對照表只存在 `results/`。`scrub` 掃描所有待 commit 檔案，任何原始值出現即失敗。

## 8. go/no-go 準則

**GO**（P2 直接接官方 server），以下全部成立：

1. P2 第一輪所需功能皆為「相同」或「可轉換」：股票／期貨／選擇權合約、tick 與 bidask 串流、新單／改單／刪單、委託與成交回報、查當日委託、查部位、查餘額。
2. 下單回應、委託回報、成交回報之間的 ID 能確定對應。
3. `custom_field` 至少能往返 6 個 base62 字元；上限更短則依實際上限調整編碼並以 ADR 定案。
4. soak 期間 SSE 回報與同時段 REST 當日委託逐筆比對，沒有遺漏。
5. 官方 server 重啟後，REST 能拿回當日委託與成交。
6. `SJ_AGENT_HARNESS` 有文件記載、不需人工審批的自動化下單方式，或可由設定關閉；模擬模式下以文件與 `all` 模式測試判斷，正式環境確認留給 P2 驗收。

**GO＋P3**：缺口可由一層薄的輔助服務補上（例如多 client 回報分送、K 棒粒度、時間戳正規化），缺口清單即 P3 範圍。

**NO-GO**：下單路徑不可用（ID 對不上、回報會漏、harness 必須人工審批且無法關閉），退回 ADR-0001 的備案：以官方 1.7 Python binding 自建 server，重開設計。

## 9. 驗收

### 9.1 Runbook

1. 安裝釘版 1.7.6，載入 `~/.sinopac-p1/env` 啟動官方 server，health 查詢確認為 Simulation；確認 server log 的實際位置並導入 `results/p1/<run_id>/server.log`。
2. 執行 `self-test-interlock`，確認中止；此步先於任何下單。
3. 執行 `preflight`：資金盤點、額度檢查、連線數。
4. 日盤行情錄製 08:30–13:45（試撮、開盤尖峰、盤中零股、期貨日盤）。
5. 夜盤行情錄製 15:00–次日 05:00。
6. 執行 `orders`：§6 C 全部情境，含資金不足拒單與兩筆成交單。
7. 雙 client 測試（§6 D）。
8. 盤中且當日已有委託時重啟官方 server，執行 `reconcile`。
9. 以 `SJ_AGENT_HARNESS=all` 另跑一次短 run。
10. 執行 `scrub`，通過後才 commit fixture。

### 9.2 Soak

窗口：週四 08:00 至下週二 09:30，約 5 天，期間官方 server 不碰；主動測試集中在週四、週五盤中，重啟測試排在最後。

推導：列出 P1 碰得到的週期現象，每種至少發生一次、盡量兩次。

| 週期現象 | 週期 | 窗口內次數 |
|---|---|---|
| session 過期 | 約 24 小時（舊 gateway 觀察值） | 4 次以上 |
| 合約更新 | 營業日 08:00、14:00（週末是否更新未知） | 平日 7 次 |
| 官方 server 每日維護 | 每日台北時間 08:12–08:30 內加隨機偏移（Task 1 啟動 log） | 6 次（週四至下週二） |
| 跨午夜 | 每日 | 5 次 |
| 期貨夜盤跨日 | 營業日 15:00–次日 05:00 | 3 次（週四、週五、週一） |
| 完整交易日 | 每個營業日 | 2 天（週四、週五） |
| 週末閒置與週一開盤 | 每週 | 1 次 |

- `calibrate:` session 過期的實際週期由本次 soak 量出，作為 P2 watchdog 的逾時依據。
- 週末在 5 天窗口內只能發生一次，第二次觀察留給 P2 的 soak。
- 避開假日週：依補假規則推論 2026-10-10（週六）國慶會使 10/9（週五）補假，排程前以證交所行事曆查證。

### 9.3 Log sweep

soak 結束後，官方 server 與錄製器 log 中每一類 WARN／ERROR 都要列出處置：說明原因後接受（例如 session 過期重登），或記為契約文件中的發現並寫明對 go/no-go 的影響。官方 server 閉源，不存在「修掉」這個處置。任何一類說不出原因，P1 即不通過驗收。

### 9.4 P1 驗收條件

- §6 A–G 每項有答案，或標「未知」附原因。
- fixture 已 commit 且去識別化掃描通過。
- soak 完成，log sweep 沒有未說明的類別。
- go/no-go 結論已寫下。

## 10. 已知缺口與風險

- **正式環境行為量不到**：模擬模式下 CA 可能未真正簽章、session 過期週期與正式行情格式可能不同、harness 在正式模式的攔截只能間接判斷；以上帶入 P2 驗收。
- **對外 IP 變動**：API key 綁定 IP 白名單；soak 期間對外 IP 若改變，session 過期後的重登會失敗，soak 中斷。開始 soak 前確認白名單含當時的對外 IP。
- **週末只觀察一次**：見 §9.2。
- **不做誘發式券商斷線**：只能依賴 soak 期間的自然事件；若 soak 內未發生，§6 E 相關項目標「未知」。
- **官方版本更迭快**：1.7.2 到 1.7.6 間隔約 7 週；P1 結論綁定 1.7.6，P2 需決定追版策略。

## 11. 捨棄的方案

**P1 錄製方式**

- 先寫 P2 的 Rust HTTP/SSE client 當錄製器：可沿用，但在契約未知前就先設計 P2 的 client，且解析後的資料不是原始位元組，當語料不夠忠實。
- 手動以官方 `/docs` 與 curl 操作：無程式碼，但長時間 soak、多 client 時序量不準，也無法重現。

SDK 與 adapter 層級的捨棄方案記在各 ADR。

## 12. 事實來源

以下皆於 2026-09-28 至 09-29 查證。

- **shioaji 1.7.6**（PyPI wheel `cp37-abi3-manylinux_2_17_x86_64`）：`shioaji/_core.abi3.so` 為 PyO3 產物；SBOM 根元件 `shioaji-python 1.7.6`，含 crate `shioaji 1.7.6`、`rsolace 0.3.13`、`rsolace-sys 0.2.1`、`pyo3 0.28.3`；附 `shioaji` CLI。crates.io 上沒有 `shioaji` crate。
- **官方 server**：CLI `--help`（斷網沙箱執行）列出 `SJ_*` 環境變數、預設 `127.0.0.1:8080`、預設模擬模式、`SJ_AGENT_HARNESS` 權限控管；執行檔字串含 `api/v1/paper/*` 與 `list_settlements: simulation mode, returning default empty settlement`。
- **shioaji 1.3.2 內部**：Solace SMF（`libsolclient` 7.33.0.3）僅用 Direct 訊息與 request/reply；登入為 Ed25519 簽章並送出 `version`、`source` 欄位；連線設定以 NaCl Box 加密下載；CA 由 Rust 的 `pyrsca` 以 RSA PKCS#1／PKCS#7 簽章，模擬模式以 mock 取代。
- **NT**：本地 `origin/develop` `cd417b8`（2026-09-26）為 workspace 0.65.0、Python 2.0.0rc6；Python custom adapter 支援於 `e9848a98ca` 加入；crates.io 最新 `nautilus-model` 0.64.0（2026-09-15）；下游 Rust 執行檔以 git 相依某個 0.64.0 fork，須靠 `[patch.crates-io]` 與 adapter 對齊。
- **憑證**：本機 CA 憑證在 P1 期間有效。
- **crates.io `rshioaji`**：以 PyO3 嵌入 Python shioaji 的概念驗證，不可作為基礎。
