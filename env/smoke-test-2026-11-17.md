# 環境 smoke test（2026-11-17）

目的：確認 `S1-k3s-ready` 環境真的跑得起 free5GC＋UERANSIM，做到 UE 端到端連線。由我（人／Claude 在 host 上手動）操作，**不是 agent trial**。做完已還原回 S1。

## 結果：通過

- free5gc-helm v4.2.2 預設設定（ULCL 架構：iupf1、psaupf1、psaupf2）＋ UERANSIM chart。
- 所有 Pod Running（image 已快取時約 165 秒）。
- UE 註冊成功、PDU session 建立，`uesimtun0` = 10.60.128.1。
- `ping -I uesimtun0 8.8.8.8`：4/4，RTT 平均 10.7 ms；用網域名稱 ping 也通（DNS 經 tunnel）。
- `scripts/env.sh reset` 還原 S1：39 秒，還原後沒有任何非系統 namespace。

## 照 chart 預設直接裝會踩到的坑（L1 參考答案和錯誤分類的素材）

| # | 現象 | 原因 | 修法 | 對應錯誤類型 |
|---|---|---|---|---|
| 1 | `mongodb-0` 永遠 Pending，其他 NF 全卡在 Init | mongodb 子 chart 的 `extraDeploy` 寫死一個給 microk8s 用的 PV：storageClass `microk8s-hostpath`、node 名稱 `free5gc`、路徑 `/home/free5gc/mongo` | `mongodb.extraDeploy: []`、`mongodb.persistence.storageClass: local-path` | E4 基礎架構 |
| 2 | **所有 Pod 都 Running，但 UE 註冊被拒（`CONGESTION`）** | 資料庫裡沒有 subscriber；AMF log：`Nausf_UEAU Authenticate Request Error: 404` | 透過 WebUI REST API（NodePort 30500）新增 `imsi-208930000000001`，K/OPC 要和 UERANSIM 的 UE 設定一致（chart 的 UE 是 `opType: OPC`） | E6 驗證不足（只看 Pod 會誤判成功） |
| 3 | UE Pod 裡 curl 失敗 | UE image 沒有 curl／wget | V3 改用 `ping -I uesimtun0`，或之後在 DN 端放自己控制的測試服務 | 驗證腳本設計 |
| 4 | （未實測）L1 要求「1 個 UPF」 | chart 預設是 ULCL 三個 UPF；要單一 UPF 需替換 SMF 子 chart 的 values 與 configmap（見 chart README） | — | E2 domain 知識 |

第 2 點正好證明「Pod Running ≠ 成功」，驗證腳本 V2 一定要看 UE 註冊／PDU session，不能只看 Pod。

## 每次 trial 的 image 下載量

環境還原後，agent 用這個 chart 部署要從 Docker Hub 重新拉約 16 個 image（free5gc/* 13 個、mongodb、busybox、curl 等）。如果一天跑很多 trial，可能撞到 Docker Hub 匿名下載次數限制（校園網路共用對外 IP 更容易撞到）。若發生，就在 host 架 pull-through registry cache，K3s 用 `registries.yaml` 指過去。這樣 VM 內仍然沒有預載 image，環境不受影響。
