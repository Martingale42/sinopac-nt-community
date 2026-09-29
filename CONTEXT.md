# Sinopac adapter

把永豐金證券（台灣證券、期貨、選擇權）接進 NautilusTrader 的社群 adapter。

## Gateway

**官方 server**:
永豐隨 shioaji 1.7 起發佈的官方 HTTP gateway（CLI 指令 `shioaji server`）。
_Avoid_: shioaji-server, gateway（未加限定時）

**舊 gateway**:
自架的 Python 服務 `Martingale42/shioaji-server`，包裝 shioaji 1.3，是 v0.1.x adapter 的連線對象。
_Avoid_: shioaji server, 官方 server

## Orders

**委託標記**:
adapter 寫進券商 `custom_field` 的短字串，用來在重啟或斷線後把券商端委託認領回 NT 的 client order。
_Avoid_: token, tag（未加限定時）

**node**:
一個執行交易策略的 NT `LiveNode` 程序；同一個永豐帳戶可以有多個 node 共用一個官方 server。
_Avoid_: client, instance

## Market data

**試撮**:
證交所開盤前的模擬撮合揭示，價量不是真實成交。
_Avoid_: 盤前成交, pre-open trade
