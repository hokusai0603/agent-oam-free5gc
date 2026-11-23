# L1 參考答案

對應任務：[../task.md](../task.md)。這份是**人做的正確解法**，用來 (1) 證明任務做得到、(2) 和 agent 的做法比對、(3) 產生 L6／L7 的起點 snapshot `S2-l1-ref`。agent 看不到這個目錄。

## 一行執行

```bash
cd ~/agent-oam-free5gc
OAM_RESET=1 tasks/L1/reference/deploy.sh   # 還原 S1 → 部署 → 建 subscriber → ping 測試
```

## 做法

| 步驟 | 內容 | 檔案 |
|---|---|---|
| 1 | 從固定版本的 free5gc-helm v4.2.2 產生單一 UPF 版 chart：SMF 的 `values.yaml` 和 `templates/smf-configmap.yaml` 換成 chart 附的 single-upf 版本，並拿掉 UPF 上的 `dnaiList: [mec]`（見坑 7） | [build-chart.sh](build-chart.sh) |
| 2 | 覆寫值：`global.userPlaneArchitecture: single`、SMF image 改回 v4.2.2、MongoDB 改用 `local-path` | [values-l1.yaml](values-l1.yaml) |
| 3 | `helm install free5gc-helm`、`helm install ueransim`（namespace `free5gc`） | [deploy.sh](deploy.sh) |
| 4 | 透過 WebUI REST API（NodePort 30500）建立 subscriber `imsi-208930000000001` | [subscriber-imsi-208930000000001.json](subscriber-imsi-208930000000001.json) |
| 5 | 重啟 UE，確認 `uesimtun0` 出現並 ping 8.8.8.8 | deploy.sh |

部署後，chart 與 values 會留在 VM 的 `/home/vagrant/free5gc-deploy/`（S2 snapshot 內也有），當作 L6／L7 時「維運人員手上的部署檔」。

## 參數

| 項目 | 值 | 必須一致的地方 |
|---|---|---|
| PLMN | 208 / 93 | AMF、SMF、NSSF、gNB、UE、subscriber |
| S-NSSAI | SST 1、SD 010203 | AMF、SMF、NSSF、UPF、gNB、UE、subscriber |
| DNN | internet | SMF、UPF、UE、subscriber |
| UE IP pool | 10.60.0.0/16 | SMF、UPF |
| SUPI | imsi-208930000000001 | UE、subscriber、SMF ueRoutingInfo |
| K / OPC | 8baf473f… / 8e27b6af…（OPC） | UE、subscriber（UE 是 `opType: OPC`，subscriber 要放在 `opc` 欄位，不是 `op`） |

## 照 chart 預設做會踩到的坑

| # | 坑 | 後果 | 錯誤類型 |
|---|---|---|---|
| 1 | 只設 `global.userPlaneArchitecture: single` | UPF 變一個，但 SMF configmap 仍寫死 ULCL 三個 UPF → PFCP 連不上 | E3 跨檔不一致 |
| 2 | 照 README 換 `single-upf-values.yaml` 但沒注意 image tag | SMF 變成 v4.1.0，其他 NF 是 v4.2.2 | E2 domain／版本 |
| 3 | MongoDB 的 PV 寫死給 microk8s | `mongodb-0` Pending，其他 NF 卡在 Init | E4 基礎架構 |
| 4 | 沒建 subscriber | **Pod 全部 Running，但 UE 註冊被拒**（AMF log：`Nausf_UEAU ... 404`） | E6 驗證不足 |
| 5 | subscriber 的 OP／OPC 放錯欄位 | 認證失敗（MAC failure） | E3 |
| 6 | 用 curl 測 UE 連線 | UE image 沒有 curl／wget，會誤判成網路不通 | 驗證方法 |
| 7 | **chart 的 single-upf 設定把唯一的 UPF 標成 DNAI `mec`** | **UE 註冊成功、拿到 IP，但 ping 100% 掉包**。SMF log：`Can't find UPF with DNN[internet] ... DNAI[]`、`fail to create data path for pcc rule`。subscriber 的 flow rule 產生沒有 DNAI 的 PCC rule，SMF 送出 PDI 為空、precedence 0 的 PDR，uplink 被 UPF 丟掉。ULCL 版沒事，因為 Anchor UPF 沒有 dnaiList | E2 domain（chart 本身的 bug） |
| 8 | 用 `kubectl rollout restart` 重啟 UE | 新舊 UE Pod 短暫並存、同一個 SUPI 互踢，PDU session 一直被建立又刪除；`kubectl exec deploy/...` 也可能進到正在終止的舊 Pod | E5 操作順序 |

補充（2026-11-18 Opus pilot 後確認）：坑 7 **只有在 subscriber 有 flow rule 時才會觸發**。這份參考答案的 subscriber 帶了 `1.1.1.1/32` 的 flow rule（WebUI 的預設範本），所以中招；Opus 建的 subscriber `FlowRules` 是空的，同樣的 SMF 設定也能通。另外 free5gc-helm 的 main（v4.3.0）已經有 `_smf-single.tpl`，坑 1、2 在新版不存在，但多了一個新坑：UPF 預設 `natIfCIDR: 192.168.0.0/16` 在 pod 裡找不到網卡會 crash，要設 `natifname: eth0`。

補充（2026-11-18 Phase 1 正式矩陣）：v4.3.0 還有一個坑：**不能用舊格式直接把 subscriber 寫進 MongoDB**（例如 chart 附的 `dbpython` 或自己寫 mongosh）。v4.3.0 的 UDM 要求 `sequenceNumber` 是物件，而且讀的是 `encPermanentKey`／`encOpcKey`。寫錯時，UE 註冊會被拒（`CONGESTION`），AMF log 是 `Nausf_UEAU ... 500`，UDM log 會出現 `Nil PermanentKey`。用 WebUI API 建立就沒事。Haiku 5.5 三次都踩到。

補充（2026-11-20，phase1-r2 Opus 3）：坑 7 **不只由 flow rule 觸發**。subscriber 若帶一筆 slice 層級的計費設定（WebUI 的 `ChargingDatas`，`dnn`、`filter` 都是空字串），PCF 也會下發一條 PCC rule（`PccRuleId-1`），SMF 一樣記下 `Can't find UPF with DNN[internet] ... DNAI[]`、`apply sm policy decision error: fail to create data path`。這次在 v4.3.0 上預設的資料路徑仍然建立成功，ping、V3x 都通過，所以只留下錯誤訊息、沒有斷線。另外，V6 的故障測試（[verify/tests/dataflow_faults.sh](../../../verify/tests/dataflow_faults.sh) `flowrule`）在 v4.3.0 上加 `1.1.1.1/32` 的 flow rule 時，表現和上面 v4.2.2 不同：SMF→PCF 建 SM policy 回 500、PDU session 根本建不起來。原因還沒確定，可能是測試用的 flow rule／QoS 格式不符合 v4.3.0 的 WebUI。

第 7 點最有代表性：**V1（Pod）和 V2（註冊、PDU session）全過，只有 V3（真的送封包）會抓到**。除錯路徑是 UE 有 IP → 在 VM 上 tcpdump 看到 GTP-U 進 UPF、但沒有解封裝後的 ICMP 出來 → 看 SMF log。這是 L7 很好的故障題材。

## 實測紀錄

見 [RUNS.md](RUNS.md)。
