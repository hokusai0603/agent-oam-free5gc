# L2 參考答案（ULCL + MEC）

對應任務：[../task.md](../task.md)。這份是**人做的正確解法**，用來 (1) 證明任務做得到，(2) 和 agent 的做法比對。agent 看不到這個目錄。

## 一行執行

```bash
cd ~/agent-oam-free5gc
OAM_RESET=1 tasks/L2/reference/deploy.sh   # 還原 S1 → 部署 MEC 應用 → 部署 free5GC（ULCL）→ 建 subscriber → 檢查上網與 MEC 路徑
```

2026-11-20 從 S1 實測一次 **PASS，270 秒**（見 [RUNS.md](RUNS.md)）。

## 架構

```
UE ── gNB ──N3──► I-UPF（iupf1，BranchingUPF）─┬─N9──► PSA-UPF1（psaupf1，AnchorUPF1）──NAT──► 網際網路（預設路徑）
                                              └─N9──► PSA-UPF2（psaupf2，AnchorUPF2）──NAT──► MEC 10.43.200.10:80
```

- MEC 應用不是 agent 的工作，屬於環境：[../setup/mec-app.yaml](../setup/mec-app.yaml)。它是 `traefik/whoami`，固定 ClusterIP `10.43.200.10`，回應內容的 `RemoteAddr` 就是連線的來源 IP。
- 兩個 PSA-UPF 都對 UE 網段做 MASQUERADE，所以 MEC 看到的來源就是轉送流量的那個 PSA-UPF 的 Pod IP。這是 L2 最直接的驗證。
- 同一組 slice／DNN／PLMN／SUPI 與 L1 相同。

## 和 L1 參考答案的關係：沒有直接改 L1

L1 參考答案是 free5gc-helm **v4.2.2**、單一 UPF。trial 用的是 **v4.3.0**（commit `07139fc`），版本不同，所以 L2 不從 L1 的檔案改，而是從同一個 S1 起點**另外做一份**。可以沿用的只有做法：

- MongoDB PV 的修正
- 用 WebUI API 建 subscriber
- 重啟 UE 的方式

v4.3.0 chart 的預設值本來就是 ULCL（I-UPF＋2 個 PSA-UPF）。所以 L1 在 v4.3.0 上要做的事是「把 ULCL 改成單一 UPF」，L2 則是「把預設的 ULCL 修到真的能導流」。

## 做法

| 步驟 | 內容 | 檔案 |
|---|---|---|
| 0 | 環境：部署 MEC 應用（固定 IP `10.43.200.10`） | [../setup/mec-app.yaml](../setup/mec-app.yaml) |
| 1 | 從固定版本的 free5gc-helm `07139fc` 產生 chart，並修改 SMF 的 `_smf-ulcl.tpl`：導流目的地改成 MEC、拿掉用不到的 UE2、加 `ulcl: true`（坑 A）、AnchorUPF2 多宣告 N3（坑 B） | [build-chart.sh](build-chart.sh) |
| 2 | 覆寫值：MongoDB 改用 `local-path`；psaupf2 的 `dnnList.cidr` 改成整個 UE 網段（坑 C） | [values-l2.yaml](values-l2.yaml) |
| 3 | `helm install free5gc-helm`、`helm install ueransim`（namespace `free5gc`） | [deploy.sh](deploy.sh) |
| 4 | 透過 WebUI REST API 建立 subscriber。v4.3.0 的 WebUI 是 ClusterIP，所以從 VM 內呼叫。payload 是 v4.3.0 WebUI 匯出的格式，沒有 flow rule | [subscriber-imsi-208930000000001.json](subscriber-imsi-208930000000001.json) |
| 5 | 重啟 UE，然後檢查三件事：(a) ping 8.8.8.8；(b) 綁 uesimtun0 連 MEC，`RemoteAddr` 必須等於 psaupf2 的 Pod IP；(c) ping 8.8.8.8 時只有 psaupf1 的 NAT 計數增加 | deploy.sh |

## 照 chart 預設做會踩到的坑（v4.3.0，2026-11-20 實測）

三個坑要**全部**修好才會通。只修其中一兩個時，症狀都是「上網正常、MEC 不通或走錯路」，V1～V3 照樣全過。

| # | 坑 | 症狀與證據 | 錯誤類型 |
|---|---|---|---|
| A | **chart 的 ULCL template 沒有 `ulcl: true`**。SMF 的 `ulcl` 預設是 false，會忽略 `uerouting.yaml` 的 `specificPath` | UE 連得到 MEC，但 MEC 看到的來源是 **PSA-UPF1**。SMF 只建 I-UPF→AnchorUPF1 一條路徑，log 完全沒有 `PSA2`／`ULCL` 字樣。SMF binary 裡有 `yaml:"ulcl"`，但 chart 沒用到 | E2 domain（chart bug）／E6（只驗到「連得到」） |
| B | **free5GC v4.3.0 SMF 的 bug**：`BPManager.SelectPSA2` 檢查的是路徑「最後一個」UPF（PSA2）有沒有 N3 介面，應該檢查第一個（I-UPF）。上游 2026-10-25 才修（[free5gc/smf a510e92](https://github.com/free5gc/smf/commit/a510e92)），v4.3.0 的 image 沒有這個修正 | 開了 `ulcl` 之後 SMF 記 `Skipping path via UPF [PSA2] (DNN mismatch)`、`No more valid PSA2 path found`，還是全部走 PSA1。替代做法：SMF 設定裡 AnchorUPF2 多宣告一個 N3 介面（同一個 IP） | E2 domain（上游 bug，要讀原始碼才找得到） |
| C | **psaupf2 的 `dnnList.cidr` 只有 `10.60.128.0/17`**。UE 的 IP 是從 AnchorUPF1 的 pool（`10.60.0.0/17`）分配，UPF 只替自己的 cidr 加 `dev upfgtp` 路由，所以 psaupf2 沒有回 UE 的路由 | ULCL 建好了：I-UPF 有比對 `permit out ip from 10.43.200.10/32` 的 PDR，用 TEID `0xa` 轉給 PSA2，PSA2 也收到正確的 GTP-U。但 **MEC 完全連不到**：PSA2 的 MASQUERADE 計數是 0。手動加 `10.60.0.0/17 dev upfgtp` 後立刻通 | E3 跨檔不一致（SMF pool ↔ UPF cidr） |
| D | MongoDB PV 寫死給 microk8s（和 L1 同一個坑） | `mongodb-0` Pending | E4 |

另外觀察到的現象（不是坑）：

- traceroute 看不到 I-UPF。從 UE 量到的第一跳是 PSA-UPF1，因為 I-UPF 只重新封裝 GTP-U、不會減 TTL。所以 V3x 的「第一跳是 UPF」在 L2 仍然成立，但**不能拿來證明有經過 I-UPF**，要靠 V6 的 N9 封包和 PFCP 規則。
- chart 預設的導流目的地 `1.0.0.1/32` 是公網位址（Cloudflare DNS），PSA2 只是換一條路出去上網，不是真的 MEC。

## 現有驗證在 L2 的表現（對這份參考答案）

| 層 | 結果 | 要改的地方 |
|---|---|---|
| V1–V3、V3x、需求（`upf_count`=3） | 全部 PASS | 加 MEC 路徑檢查：綁 uesimtun0 連 MEC，比對 `RemoteAddr` 是否為 PSA2；也可以用 TTL=1 送往 MEC IP，看回 time-exceeded 的是不是 PSA2 |
| V5 | 1 個 error 是**誤判**：`pool.match` 假設只有一個 UPF | 改成逐一比對每個 UPF：每個 PSA 的 SMF pool 必須在自己的 cidr 內；specificPath 上的 PSA 的 cidr 必須涵蓋預設 PSA 的 pool（抓坑 C）；有 specificPath 時必須有 `ulcl: true`（坑 A）；specificPath 的 dest 等於規格的 MEC IP，members 包含規格的 SUPI；v4.3.0 時 PSA2 必須有 N3（坑 B） |
| V6 | 5 個 error 都是**誤判**：把 I-UPF 和 PSA 當成同一個 UPF 來配對 PFCP／TEID | 改成依 PFCP 的 node 分開看每個 UPF；N3 的 UPF 從 NGAP 的 UL TEID 決定；加 N9 GTP-U、I-UPF 的 ULCL PDR（SDF filter = MEC IP）和 FAR（指向 PSA2 的 TEID）檢查 |
