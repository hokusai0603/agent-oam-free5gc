# L2 參考答案實測紀錄

| 日期 | 起點 | 結果 | 時間 | 備註 |
|---|---|---|---|---|
| 2026-11-20 | S1-k3s-ready | FAIL | — | 第一版只改導流目的地：MEC 看到的來源是 PSA1（坑 A）。之後在同一個部署上逐一找到坑 A、B、C，用 `helm upgrade`＋重啟 SMF／UE 驗證 |
| 2026-11-20 | S1-k3s-ready | **PASS** | 270 s | 三個坑都修好後從頭部署：`mec_saw=10.42.0.26`（psaupf2）、ping 8.8.8.8 時 NAT 計數 psaupf1 +1、psaupf2 +0。V1–V3x 全過（第一跳是 psaupf1）；V5 1 個誤判（`pool.match`）、V6 5 個誤判（單一 UPF 假設） |
| 2026-11-23 | S1-k3s-ready | **PASS** | 273 s | 第 2 次從零：`mec_saw` = psaupf2、NAT psaupf1 +1／psaupf2 +0 |
| 2026-11-23 | S1-k3s-ready | **PASS** | 272 s | 第 3 次從零：同上 |

## 逐跳封包證據（2026-11-20，同一個 PASS 部署）

在 cni0 抓 GTP-U，UE 連 MEC 3 次、ping 8.8.8.8 3 次（[evidence/mec-live.pcap](evidence/mec-live.pcap)）。iupf1 `10.42.0.24`、psaupf1 `.25`、psaupf2 `.26`、gNB `.32`：

| 外層 | 內層 | 封包數 |
|---|---|---|
| gNB → I-UPF | UE → MEC | 18 |
| **I-UPF → PSA2** | UE → MEC | 18 |
| PSA2 → I-UPF → gNB | MEC → UE | 12／12 |
| gNB → I-UPF → **PSA1** | UE → 8.8.8.8 | 3／3 |
| PSA1 → I-UPF → gNB | 8.8.8.8 → UE | 3／3 |

MEC 流量沒有出現在 PSA1，上網流量也沒有出現在 PSA2。V6 的 PFCP 解碼另外確認：I-UPF 有優先序 30 的規則 `permit out ip from 10.43.200.10/32 to <UE IP>`，轉送到 PSA2。

## 反向測試：從零部署、故意少修一個坑（2026-11-23）

`SKIP_FIX=A|B|C OAM_RESET=1 deploy.sh`，log 在 [evidence/runs-2026-11-23/](evidence/runs-2026-11-23/)。三個版本上網都正常（ping 0% loss、NAT 只有 psaupf1 增加），只有 MEC 檢查失敗，而且失敗方式和預測一致：

| 版本 | MEC 看到的來源 | SMF log | 結果 |
|---|---|---|---|
| A：不加 `ulcl: true` | psaupf1 | 沒有任何 `PSA2` 字樣 | FAIL（如預期） |
| B：AnchorUPF2 不加 N3 | psaupf1 | `DNN mismatch` 2 次 | FAIL（如預期） |
| C：psaupf2 用 chart 預設 cidr | 連不到 | PSA2 路徑有建（10 行），和正常版相同 | FAIL（如預期） |
| 正常版 ×2 | psaupf2 | PSA2 10 行、沒有 mismatch | PASS |

每次部署的 Pod IP 都不同（例如 psaupf2 是 `.13`／`.17`／`.25`／`.12`／`.25`），所以驗證必須在執行時查 IP，不能寫死。

## 和上游的對照

| 坑 | 上游的資料 |
|---|---|
| A | free5GC v4.3.0 官方 ULCL 範例 `config/multiUPF/smfcfg.ulcl.yaml` 有 `ulcl: true`；SMF `pkg/factory/config.go` 的 `ULCL bool yaml:"ulcl"` 預設 false |
| B | 官方範例的 PSA2（UPF2）只有 N9、路徑 `[UPF1, UPF2]`，在 v4.3.0 SMF 上一樣會被跳過。修正 `a510e92`（2026-10-25，`lastNode` → `FirstDPNode`）目前不在任何 release tag |
| C | 官方 ULCL 測試的 `upfcfg.testulcl.yaml` 所有 UPF 都用 `cidr: 10.60.0.0/16` |
