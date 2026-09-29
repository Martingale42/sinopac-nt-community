---
status: accepted
---

# adapter 以 Rust 為主，Python 版延後

NT v2 已移除 v1 Cython 層與 PyCapsule 橋接，外部 Rust 擴充也不能連結 NT 的 model pyclass，Rust 與 Python 兩條路幾乎不能共用 client 邏輯；因此 adapter 先做成純 Rust 的 `DataClient` / `ExecutionClient`，由 Rust `LiveNode` 執行檔使用，Python 版等 NT 2.0 正式版與實際需求出現再另開一輪。

## Consequences

PyPI 上的 v0.1.x 使用者在 Python 版出現前沒有 NT v2 版本可用；community listing 的安裝說明需隨 Rust 版發佈更新。
