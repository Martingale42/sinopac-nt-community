---
status: accepted
---

# 委託標記保留 node 識別位

目前只有一個 node 下單，但委託標記（寫進券商 `custom_field` 的字串）的編碼現在就保留 node 識別位，多 node 過濾邏輯等真的出現第二個 node 再做。標記會隨真實委託送到券商，事後改編碼會讓升級前的在途委託無法被認領，所以編碼必須在第一張單送出前定案。

## Consequences

確切的字元配置取決於官方 server 對 `custom_field` 的長度與字元集限制，由 P1 量測後以新 ADR 定案並取代本 ADR 的候選配置（1 字元 node + 5 字元雜湊）。
