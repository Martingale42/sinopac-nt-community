---
status: accepted
---

# 以官方 shioaji server 為基礎，不自行重寫 SDK

永豐自 shioaji 1.7 起已把 SDK 核心改寫為 Rust，並隨附官方 HTTP gateway（`shioaji server`，REST + SSE），因此 adapter 直接建在官方 server 上，不從頭實作 SDK，也不再維護自架 gateway。協定與券商端變動由永豐承擔，本 repo 只維護 NT 這一側。

## Considered Options

- **從頭以純 Rust 實作 SDK**：需逆向 Solace SMF 私有協定、送出官方版本字串冒充官方客戶端、抽取官方二進位檔內嵌的金鑰，且每次官方改版都可能失效，並有違反 API 服務條款的風險。
- **以官方 1.7 的 Python binding 自建 server**：保留自訂契約，但仍要維護一層 Python 服務；保留作為 P1 判定 NO-GO 時的退路。

## Consequences

adapter 的契約改由官方 OpenAPI 規格決定；官方 server 是閉源執行檔，行為只能靠黑箱量測與錄製語料確認。
