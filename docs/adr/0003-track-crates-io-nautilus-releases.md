---
status: accepted
---

# 追 crates.io 最新 nautilus-* 發行版，develop 只做預警

adapter 相依 crates.io 上最新的 `nautilus-*` 發行版，另設一條對 NT develop 編譯的非阻擋 CI；不直接以 git 相依 develop。crates.io 不接受 git 相依，而下游 Rust 執行檔必須讓 adapter 與自己解析到同一份 `nautilus-model`，發行版範圍相依才能讓它們用 `[patch.crates-io]` 接上自己的 fork。

## Considered Options

- **git 相依 develop**：API 最新，但無法發佈到 crates.io，且迫使所有下游跟著升 develop。
- **釘在某個下游 fork 的 rev**：整合最省事，但 adapter 不再是一般 NT 使用者可用的 community adapter。
