# Sinopac Adapter 抽離設計 Spec

- **日期**:2026-06-26
- **狀態**:待 review(brainstorming 產出,尚未進 writing-plans)
- **來源 source of truth**:`sinopac-adapter-clean` @ `034c70e788`(備份:tag `backup/sinopac-adapter-clean-2026-06-26`、`origin/sinopac-adapter-clean`)
- **目標 repo**:`/home/cy/Code/MT5/sinopac-nt-community`(建立於 nautilus_trader 樹**外**)
- **companion gateway**:`github.com/Martingale42/shioaji-server`(獨立 repo,live 執行才需要;本 spec 的 build/test 不依賴它)

---

## 1. 背景與目標

把目前深嵌在 NautilusTrader monorepo 的 Sinopac(永豐金/台灣市場)broker adapter,抽離成一個**獨立、可被列為 Community adapter** 的外部 repo。上游創辦人已婉拒官方納入(RFC #3750),提供的路徑是依 `ADAPTERS.md` + `TRADEMARK.md` 的社群列名。因此 adapter 必須成為自有外部專案,依賴**已發佈**的 nautilus_trader,而**不**併回 fork 的貢獻路徑。

非目標(明確排除):
- 不改 monorepo 的 `develop`(Phase 1 已使其 == `upstream/develop`)。
- 不在此 spec 內做 live 真實券商驗證(見 §10 完成標準)。
- 不代為去開 GitHub listing(Phase 3,使用者手動)。

---

## 2. 關鍵決定(已拍板)

| 項目 | 決定 |
|---|---|
| pin 版本(Python) | PyPI `nautilus_trader == 1.228.0`（最新且 `python` feature 可編；見下方 M0 結果） |
| pin 版本(Rust) | crates.io `nautilus-core / -model / -common / -network == 0.58.0`（pyo3 0.28.3） |
| adapter 原始基準 | Python 1.226.0 / Rust 0.56.0 → 需吃 **2 個 minor 的 API drift**(0.56→0.58 / 1.226→1.228) |

### M0 結果（2026-06-26，已驗證）

- **VERDICT = Approach A（採用）**。最小 standalone pyo3 ext（連 `nautilus-model` 0.59.0）與 PyPI `nautilus_trader 1.229.0` wheel 之間，`Price`/`Quantity` 的 raw 定點整數**逐位元組相同**、兩 ext 同 process 共存無衝突 → 跨獨立編譯 ext 的核心互通成立。
- **精度模式 = HIGH（1e16 / i128）**；ext 必須開 `high-precision` 才吻合（關閉差 1e7）。`default = ["high-precision"]` 正確。
- **為何 pin 從 0.59.0 退到 0.58.0**:`nautilus-model 0.59.0` 的 `python` feature 同時拉 `pyo3 ^0.29.0` + `pyo3-stub-gen =0.20.0`(不相容,stub-gen 用了 pyo3 0.29 移除的 `downcast`)→ **0.59.0 的 python feature 無法編譯**(上游發版疏失)。0.56/0.57/**0.58** 用 pyo3 0.28.3,python feature 可編。選 0.58.0 = 最新可編版,且其 wheel 與 ext 用**相同 pyo3 0.28.3**,互通更穩。
- **保留 Approach A**(Rust 端發 capsule 的忠實移植);Approach C(raw-only 重設計)不需要。
- spike 為 throwaway,位於 `/home/cy/Code/MT5/sinopac-spike/`(M1 時清除)。
| git 歷史 | **clean copy**,全新歷史;首 commit 註明來源 `034c70e788`。完整 61-commit 歷史保存在 backup tag,不帶進新 repo |
| 完成標準 | **build + 單元/整合測試綠燈**(對 pin 版),不連真實券商 |
| 互通風險 | **M0 spike 先 de-risk**;通過走方案 A,失敗退方案 C |
| 套件/crate 命名 | TRADEMARK:套件不得 `nautilus_*`、crate 不得 `nautilus-*`;**venue 字串 `"SINOPAC"` 維持** |
| 授權 | 沿用 `LGPL-3.0-or-later` |

命名對照:

| 角色 | monorepo 現況 | 標準版 |
|---|---|---|
| Rust crate `[package].name` | `nautilus-sinopac` | `sinopac-nt-community` |
| Rust `[lib].name` / pymodule | `nautilus_sinopac` / `#[pymodule] sinopac` | `sinopac_nt` / `#[pymodule] _sinopac` |
| 編譯 ext 的 Python 路徑 | `nautilus_trader.core.nautilus_pyo3.sinopac`(母聚合器注入) | `sinopac_nt._sinopac`(自建) |
| Python 套件 | `nautilus_trader.adapters.sinopac`(8 檔) | `sinopac_nt`(8 檔) |
| 內部 venue 常數 | `"SINOPAC"` | `"SINOPAC"`(不變) |

---

## 3. 來源盤點與耦合點(抽離要切斷/替換的接點)

Rust 核心 `crates/adapters/sinopac/`:`src/{common,http,websocket,python}/`、`lib.rs`、`tests/{http,websocket}.rs`、`test_data/`(17 個 json fixture)。crate-type 已含 `["rlib","cdylib"]`,且 `src/python/mod.rs` 已有 `#[pymodule] pub fn sinopac(...)` —— **本身就接近可獨立**。

| 耦合點 | 檔案/位置 | 抽離動作 |
|---|---|---|
| workspace 成員 | 根 `Cargo.toml:20` `"crates/adapters/sinopac"` | 移除(新 repo 為獨立 crate) |
| workspace deps(path) | 根 `Cargo.toml` `nautilus-* = { path=..., version="0.56.0" }` | 改成 crates.io `version = "0.59.0"` |
| 母 pyo3 註冊 | `crates/pyo3/src/lib.rs:288-293` wrap `nautilus_sinopac::python::sinopac` | 移除依賴;改由 maturin 直接建 `sinopac_nt._sinopac` |
| 母 pyo3 Cargo | `crates/pyo3/Cargo.toml`(dep + features) | 不適用(新 repo 不含母 pyo3) |
| `workspace = true` 第三方 deps | sinopac `Cargo.toml`:anyhow, arc-swap, chrono, dashmap, log, serde, serde_json, thiserror, tokio, tokio-tungstenite, ustr, pyo3, pyo3-async-runtimes;dev:axum, futures-util, rstest | 全部換成具體版號(由 `v1.229.0` tag 的 `Cargo.toml` `[workspace.dependencies]` 讀出對應值,確保與 0.59.0 相容) |
| Python ext import | `factories.py:1`、`execution.py:21-29` 等 `from nautilus_trader.core.nautilus_pyo3.sinopac import ...` | 改寫成 `from sinopac_nt._sinopac import ...` |
| Python 框架 import | 各檔 `from nautilus_trader.{model,live,common,config,core,cache} import ...` | **保留**(對 PyPI `nautilus_trader` 的合法外部相依) |
| namespace | `nautilus_trader/adapters/sinopac/` | 改 home 到 `python/sinopac_nt/` |
| build orchestration | 根 poetry + cython `build.py` | 改用 **maturin**(範本:`python/pyproject.toml`) |
| lockfile | 根 `Cargo.lock` / `python/uv.lock` | 新 repo 自有 lockfile |
| CI | `.github/workflows/build-fork-wheels.yml` | 改寫成 maturin 建 wheel + 跑測試 |
| docs | `docs/integrations/sinopac.md`、`docs/api_reference/adapters/sinopac.md` | 複製到新 repo `docs/` |
| 測試/範例 | `tests/integration_tests/adapters/sinopac/`、`examples/live/sinopac/` | 複製到 `python/tests/`、`examples/` |

Python 套件對框架的相依分類(保留指向 PyPI nautilus_trader):config(`LiveDataClientConfig`/`LiveExecClientConfig`/`NautilusConfig`/`InstrumentProviderConfig`)、model(`Price`/`Quantity`/`Money`/`Bar`/`TradeTick`/`QuoteTick`/`Instrument`/`Order`/各 enum/各 Id/`capsule_to_data`…)、live(`LiveMarketDataClient`/`LiveExecutionClient`/`cancel_tasks_with_timeout`…)、common(`LiveClock`/`MessageBus`/`LogColor`)、cache(`Cache`)、data/exec message types。

---

## 4. 目標 repo 架構

```
sinopac-nt-community/
├── README.md            # 頂部非附屬聲明 + 安裝/用法 + 維護者聯絡
├── LICENSE              # LGPL-3.0-or-later
├── CHANGELOG.md
├── Cargo.toml           # 獨立 crate(非 workspace member)
│                        #   [package] name=sinopac-nt-community
│                        #   [lib] name=sinopac_nt, crate-type=["rlib","cdylib"]
│                        #   nautilus-* = "0.59.0"(crates.io);其餘第三方具體版號
│                        #   [features] default=["high-precision"], python, extension-module, high-precision
├── Cargo.lock
├── rust-toolchain.toml  # pin 對應 v1.229.0 的 rust-version(由該 tag 讀出)
├── src/                 # Rust 核心(common/ http/ websocket/ python/ lib.rs)攤平到根
├── tests/               # Rust:http.rs / websocket.rs
├── test_data/           # 17 個 json fixture
├── pyproject.toml       # build-backend = maturin
│                        #   [tool.maturin] manifest-path="Cargo.toml"
│                        #     module-name="sinopac_nt._sinopac", python-source="python"
│                        #     features=["extension-module","high-precision"]
│                        #   [project] dependencies=["nautilus_trader==1.229.0"]
├── python/
│   ├── sinopac_nt/      # 改 home 的套件(8 檔)+ maturin 編入的 _sinopac.*.so
│   │   ├── __init__.py config.py constants.py data.py
│   │   ├── execution.py factories.py providers.py tags.py
│   └── tests/           # 整合測試:conftest.py test_{config,execution,factories,tags}.py
├── examples/            # sinopac_data_tester.py / sinopac_exec_tester.py
├── docs/                # sinopac.md(整合指南)+ 本 spec + API 說明
└── .github/workflows/   # CI:maturin build wheel(linux/mac/win)+ pytest/cargo test 對 1.229.0
```

設計取捨:單 crate repo,Rust 攤平到根(無需 `crates/` 子層);Python 走 maturin 的 mixed layout(`python-source="python"`),編譯 ext 落在 `sinopac_nt._sinopac`,套件以 `from sinopac_nt._sinopac import ...` 取用。

---

## 5. 核心技術風險:雙 extension 互通(方案 A vs C)

**問題**:標準版 `sinopac_nt._sinopac` 會**自編一份** `nautilus-model 0.59.0`;PyPI `nautilus_trader 1.229.0` wheel **內含另一份**(獨立編譯)。兩者版本相同,但 pyo3 型別物件是 per-module 身份;跨 extension 傳遞框架型別物件可能 `isinstance`/extract 失敗。

**為何有機會成立**:adapter 跨界主要走 **C-ABI / PyCapsule**(`capsule_to_data`)與 **raw-value 建構子**(checked Price/Quantity from raw),這些是**版本相依**(同 0.59.0 → C layout 相同)而**非編譯身份相依**;crate 自有的 enum(`SinopacAction`…)只存在於本 ext,不需被框架辨識。

**方案 A(主)**:忠實移植,維持 Rust↔Python 現有邊界,靠 capsule + raw 建構子互通。
**方案 C(備援)**:若 M0 暴露型別身份問題,把邊界改成**只傳 primitive/capsule**,不依賴跨 ext 型別身份。

**M0 spike(de-risk 關卡)**:
1. 用 maturin 建出最小 `sinopac_nt._sinopac`(連 crates.io `nautilus-model 0.59.0`)。
2. `uv` 環境內 `pip install nautilus_trader==1.229.0`。
3. 驗證:(a) ext 與 nautilus_trader 可同時 import 不衝突;(b) crate 的 checked Price/Quantity 建構子產出的值,透過 raw 能被框架 `Price`/`Quantity` 接受;(c) WS parser 產出的 Data/Tick 經 `capsule_to_data` 能被框架接受且型別/數值正確。
4. **Gate**:全數通過 → 走 A;任一失敗 → 退 C,並把失敗點記錄為設計修正。

---

## 6. 解耦細節

- **Cargo**:`workspace = true` 全部換具體值。`nautilus-* = "0.59.0"`(拿掉 path)。第三方(tokio/serde/pyo3…)版號由 `git show v1.229.0:Cargo.toml` 的 `[workspace.dependencies]` 抄出,確保與 0.59.0 一致。`[workspace.package]` 繼承的欄位(version/edition/rust-version/license…)改成具體值。
- **pyo3 module**:把 crate 對外的 pymodule 由 `sinopac` 對齊 maturin `module-name` 的末段 `_sinopac`(rename fn 或設定 `lib.name`)。`extension-module` feature 維持(pyo3 的 `extension-module`)。
- **Python re-home + import 改寫**:8 檔搬 `python/sinopac_nt/`;`nautilus_pyo3.sinopac` 系列 import → `sinopac_nt._sinopac`;框架 import 不動。`__init__.py` 的對外 API 視需要保留(factories/config/clients)。
- **build**:maturin;`maturin develop` 本機開發、`maturin build --release` 出 wheel。`pyproject.toml` 的 `[project].dependencies` pin `nautilus_trader==1.229.0`。

---

## 7. API drift 處理(0.56→0.59 / 1.226→1.229)

預期破裂面:(a) Rust 端 nautilus-core/-model/-common/-network 的 API 簽章變動;(b) Python 端 nautilus_trader 的 client/config/model API 變動。處理流程:M4 對 pin 版逐一 build,收集編譯/匯入錯誤,逐項修;**無法靠重命名/簽章調整解決的破裂(語意改變、移除的 API)列為 blocker 回報使用者決定**(符合任務「Report blockers (API drift)」)。

---

## 8. 合規(供 Community listing)

- **README 頂部非附屬聲明**(逐字):
  > This is an independent community project. It is not affiliated with, endorsed by, or supported by Nautech Systems Pty Ltd or the official NautilusTrader project.
- **LICENSE**:沿用 `LGPL-3.0-or-later`。
- **命名**:套件 `sinopac_nt`、crate `sinopac-nt-community`(無 `nautilus` 前綴);venue 常數 `"SINOPAC"` 維持。
- **docs**:安裝(`pip install` 指向已發佈 nautilus_trader + 本套件 wheel)、用法、與 shioaji-server 的關係、維護者/聯絡行。
- 抽離後須複查:程式碼/註解/docs 內**不得**宣稱官方背書;移除任何 fork 專屬、會誤導為官方的字樣。

---

## 9. 里程碑(每個都是檢查點;跨 session 友善)

| M | 內容 | Gate / 產出 |
|---|---|---|
| **M0** | 互通 spike(§5) | 通過 → A;失敗 → C |
| **M1** | scaffold 新 repo:目錄、`git init`、LICENSE、README(聲明)、pyproject(maturin)、Cargo.toml(具體版號骨架)、CI 骨架。首 commit 註明 `034c70e788` | repo 可 `git log` 出首 commit |
| **M2** | 移植 + decouple Rust:複製 crate、改名、`workspace=true`→具體版號、暴露 `#[pymodule] _sinopac` | `cargo build`(含 `--features python,extension-module`)綠 |
| **M3** | 改 home Python + 改寫 import | `maturin develop` 成功;`import sinopac_nt` 與 ext OK |
| **M4** | API drift 修復 | 對 1.229.0/0.59.0 可 build;blocker 列表 |
| **M5** | 測試/examples/docs 移植齊全 | `uv run pytest` + `cargo test` 全綠(= 完成標準) |
| **M6** | 合規與打包:聲明/LICENSE/docs/聯絡人;CI 建 wheel + 跑測試 | CI 綠 |
| **M7** | 收尾回報;提醒手動 listing(Phase 3) | 狀態 + blocker 報告 |

---

## 10. 完成標準(Definition of Done)

- `maturin build` 能在 pin 版(`nautilus_trader==1.229.0` / `nautilus-* 0.59.0`)下產出 wheel。
- 既有 Python 測試(`test_config/execution/factories/tags`)與 Rust 測試(`http/websocket`)全綠。
- 合規四項齊備(聲明、LICENSE、docs、聯絡人)。
- **不含** live 真實券商驗證(留給使用者跨環境手動 smoke test)。

---

## 11. 範圍外 / Phase 3 交接

- live 連線 shioaji-server 的端到端 smoke test(需 gateway + 憑證)。
- 在 `nautechsystems/nautilus_trader` 開 issue/discussion 做 Community listing(使用者手動)。
- monorepo 端 `sinopac-adapter-clean` / `sinopac-adapter` / `mt5-adapter` / `dev-all` / `shioaji-adapter` 的去留(Phase 3 提案,不在本 spec 執行)。

---

## 12. 未決 / 風險

- **R1**:M0 spike 可能失敗 → 觸發方案 C(邊界改 ABI-only),增加工作量。已有備援設計。
- **R2**:3 minor 的 API drift 規模未知;M4 才會量化。可能出現語意性 blocker。
- **R3**:maturin `module-name` 與 crate pymodule 對齊細節需在 M2/M3 確認。
- **R4**:Rust `rust-version` / 第三方版號須以 `v1.229.0` tag 為準抄出,避免與 0.59.0 不相容。
