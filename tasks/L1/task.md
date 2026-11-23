# L1：單機 bring up 最基本的 5G 核網

> 這份是**給 agent 的任務描述**（「普通」版本）。參考答案在 [reference/](reference/)，agent 看不到。
> 詳細版、模糊版之後再補（見最下方）。

## 給 agent 的描述（普通版）

> 實際送給 agent 的全文是 [prompt-normal.md](prompt-normal.md)（多了「無人值守、不會有人回答問題」，以及 2026-11-18 起指定的版本：free5GC v4.3.0、free5gc-helm commit `07139fc`），以那份為準。

你在一台 Ubuntu 22.04 的機器上，上面已經有一個單節點的 K3s 叢集（`kubectl`、`helm` 都可以直接用，需要 root 時可以用 `sudo`）。這台機器的 kernel 已經載入 gtp5g 模組。

請在這個叢集上用 free5GC 部署一個最基本的 5G SA 網路，並用 UERANSIM 模擬基地台與手機：

- 1 個 gNB、1 個 UE、**1 個 UPF**
- PLMN：MCC 208、MNC 93
- 1 個 slice：SST 1、SD 010203
- 1 個 DNN：`internet`
- UE 的 SUPI：`imsi-208930000000001`

完成條件：UE 要成功註冊並建立 PDU session，而且**能透過 5G 網路（UE 的 tunnel 介面）連到網際網路**，例如 ping 8.8.8.8。

完成後請說明：(1) 你部署了哪些元件、用了哪些設定；(2) 你怎麼確認這個網路是正確的。

## 驗證（agent 看不到）

| 層級 | 條件 |
|---|---|
| V1 | 部署的 Pod 全部 Running／Ready，沒有 CrashLoopBackOff |
| V2 | gNB 與 AMF NG Setup 成功；UE 註冊成功；PDU session 建立 |
| V3 | 從 UE 的 tunnel 介面（UERANSIM 為 `uesimtun0`）ping 8.8.8.8 成功 |
| 需求 | UPF 只有 1 個；UE 的 slice／DNN／SUPI 與描述一致 |

驗證腳本不依賴 agent 怎麼命名資源：它會掃描非系統 namespace，找出有 `uesimtun0` 介面的 Pod 當作 UE。

## 待補

- 詳細版（提示可以用 free5gc-helm、要建 subscriber）
- 模糊版（「幫我在這台 K3s 上架一個最小的 5G 核網，手機要能上網」）
