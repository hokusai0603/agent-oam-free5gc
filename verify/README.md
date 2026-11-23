# 驗證方法

agent 結束後，由 host 上的 runner 執行驗證，判定這次 trial 成功或失敗。

## 原則

- **只看結果，不看 agent 說了什麼**。agent 自己回報「成功」不算數，以實際的網路狀態為準。
- **不依賴 agent 的命名方式**。agent 的 namespace、release 名稱、Pod 名稱每次都不同，所以探針會掃描所有非系統 namespace 的 Pod 和 VM 本機：有 `uesimtun0` 介面的就是 UE，有 `nr-cli` 的就查 UERANSIM 狀態。
- **agent 看不到驗證程式**。腳本放在 host，執行時才經 `vagrant ssh` 送進 VM，跑完就刪掉。
- **agent 結束後等 60 秒再驗**，讓剛重啟的元件有時間穩定。

## 檢查項目

| 層 | 檢查什麼 | 怎麼查 | 通過條件 | 抓得到的問題 |
|---|---|---|---|---|
| **V1** Pod | 非系統 namespace 的 Pod 都正常 | `kubectl get pods -A` | 至少有一個 Pod，而且全部是 Running＋Ready（已完成的 Job、正在終止的 Pod 不算） | Pod 起不來：MongoDB Pending、UPF CrashLoop、image 拉不到 |
| **V2** 控制面 | gNB 連上 AMF；UE 已註冊並建立 PDU session | 在 UE／gNB 所在處執行 UERANSIM 的 `nr-cli`：`status`、`ps-list` | 有 UE 是 `RM-REGISTERED` 並且有 `PS-ACTIVE` 的 session；有 gNB 時 `is-ngap-up: true` | subscriber 沒建、K／OPC 錯、PLMN／slice 對不上、SMF 連不到 UPF |
| **V3** 資料面 | UE 能經 5G 網路上網 | 從 UE 執行 `ping -I uesimtun0 -c 4 8.8.8.8` | 4 個封包至少回 3 個 | 註冊成功但封包不通，例如參考答案的坑 7、UPF 沒做 NAT |
| **V3x** 資料面（擴充） | 經 tunnel 真的能用網路，而且流量從 UPF 出去 | 用 `nsenter` 進 UE 的網路空間，借 VM 的工具測（見下方） | 下面每一項都通過或無法判定 | ping 會過、但 TCP／大封包不通（UPF 擋 TCP、MTU 黑洞）；流量繞過 UPF |
| **V4** MEC 路徑（L2） | MEC 流量和上網流量從不同的 PSA-UPF 出去，而且都先經過 I-UPF | 經 `uesimtun0` 連 MEC、ping 8.8.8.8，比較每個 UPF 的 `upfgtp` 位元組和 GTP-U 封包數（見下方） | spec 有 `expect.mec` 才做；連得到 MEC，且兩種流量的 anchor 不同 | ULCL 沒生效（MEC 走預設 PSA）、MEC PSA 沒有回 UE 的路由（MEC 不通） |
| **需求** | 部署符合任務描述 | 依 `tasks/<task>/spec.json` 的 `expect` | 每一項都符合 | 部署了錯的拓樸或參數 |
| **V5** 跨 NF 設定一致性 | PLMN、TAC、S-NSSAI、DNN、UE IP pool、拓樸、位址參照、安全演算法、subscriber 在各 NF 之間對得上，而且符合規格 | 靜態分析 `configmaps.yaml`（＋V6 匯出的 MongoDB subscriber）（見下方） | 沒有 `error`；`exact` 再要求沒有 `extra`（多設了規格外的東西） | 某個 NF 漏改、YAML 之間互相衝突、多留了 chart 預設的 slice |
| **V6** NF 間 dataflow 與封包內容 | 註冊與 PDU session 建立的訊息順序、收發的 NF、回應、訊息內容，以及 NGAP／PFCP／GTP-U 的 TEID 一致 | UE 去註冊→自動重新註冊，同時在 `cni0` 抓封包，host 上用 tshark 解碼（見下方） | 同 V5 | SBI 回 4xx／5xx、PFCP 規則不完整（坑 7 的空 PDI）、下行不通、Allowed NSSAI 多給、TEID 對不上 |
| **副作用** | agent 對 VM／K3s 做了哪些變更 | agent 開始前、結束後各拍一次 VM 狀態，再比對差異 | **不判分**，只記錄 | 擅自改 K3s 設定、重啟 K3s、改 sysctl、裝套件 |

**PASS = V1、V2、V3、需求全部通過。** 這是 Phase 1 用的定義，為了能互相比較先不改。
**L2 起（spec 有 `expect.mec`）PASS 還要 V4 通過**（無法判定也算不通過），因為「MEC 流量走 MEC 專用 PSA」就是任務的完成條件（2026-11-23）。
**嚴格版 PASS（`pass_strict`）= PASS ＋ V3x 通過**（2026-11-19 起才有；V3x 無法判定時是 `None`）。之後要不要以嚴格版為主，跑過幾次再決定。
副作用只記錄、不影響 PASS，由人工判斷這些變更是否必要（不必要的變更記為錯誤類別 E7）。
**V5、V6（2026-11-20 起）先只記錄在 `meta.json` 的 `verify.v5`／`verify.v6`，不併進 PASS**；跑過幾個 trial、確認沒有誤判後再決定。

V5、V6 每一項的狀態：`ok`、`error`（不符合規格或會出錯）、`extra`（規格要的都有，但多設了別的）、`warn`（潛在問題）、`info`（只記錄）、`skip`（缺資料）。`pass` = 沒有 error；`exact` = 沒有 error 也沒有 extra。

### V3x：抓網頁、DNS、第一跳（2026-11-19，依指導老師建議加入）

只用 4 個小 ICMP 封包判斷「能上網」會高估：DNS、TCP 的 NAT、大封包穿過 GTP-U 的 MTU 問題都測不到。V3x 對每個 ping 得通的 UE 做：

| 項目 | 怎麼測 | 通過條件 |
|---|---|---|
| `first_hop_is_upf` | 經 `uesimtun0` 送 TTL=1 的 ping，看誰回 time exceeded（TTL=2、3 也記錄） | 回應的 IP 是 UPF 的 Pod IP；沒有回應 → 無法判定 |
| `dns` | `dig -b <UE IP> @8.8.8.8` 查測試用的網域 | 查得到 IPv4 |
| `http` | `curl --interface uesimtun0` 抓 `generate_204` | 回 204／200 |
| `https` | 抓 Google／Cloudflare 首頁 | 2xx／3xx，內容 > 1 KB |
| `download_1mb` | 下載 1 MB（Cloudflare，備援 thinkbroadband） | 完整 1,000,000 bytes 以上 |

注意事項：

- **一定要綁 `uesimtun0`**。UE 的 Pod 自己有 eth0 和預設路由，沒綁的話流量直接從 eth0 出去，會變成假的成功。實測 `tracepath`（不能綁介面）的第一跳就是 node 而不是 UPF。每個網頁測試也都檢查 curl 的來源 IP（`local_ip`）必須是 UE IP。
- **UE 的 image 沒有 curl、dig**，所以從 VM 用 `nsenter -t <UE 容器 PID> -n` 進 UE 的網路 namespace，借 VM 的工具。不會改到 agent 部署的 Pod。DNS 要先經 tunnel 查好，再用 `curl --resolve` 帶進去（VM 的 `/etc/resolv.conf` 指向 `127.0.0.53`，在 UE 的 namespace 裡連不到）。
- **對照組**：某項失敗時，從 VM 本身（不經過 5G）再抓一次同樣的網址。對照也失敗就是外網的問題，標成無法判定（`None`），不算 agent 失敗。
- 參考部署的實際路徑：第一跳是 UPF（`10.42.0.66`），第二跳是 node 的 `cni0`（`10.42.0.1`），第三跳是 VirtualBox NAT（`10.0.2.2`），接著是校園網路。只判定第一跳，後面只記錄。
- `uesimtun0` 的 MTU 是 1400：1372 bytes 的不分段 ping 能過，1400 不行。這是 UERANSIM 的預設值，不是錯誤。

### V4：MEC 路徑（[probe.py](probe.py) `run_v4`，L2，2026-11-23）

只有 spec 有 `expect.mec` 時才做，對每個 ping 得通的 UE：

1. 經 `uesimtun0` 連 MEC 5 次（`curl --interface uesimtun0`，進 UE 的網路 namespace 借 VM 的 curl），同時記錄每個 UPF 的變化；
2. 經 `uesimtun0` ping 8.8.8.8 10 次，同樣記錄。

每個 UPF 記兩個數字（進 UPF 的網路 namespace 讀，不改 agent 的 Pod）：

- `gtp`：`upfgtp` 介面的位元組變化。gtp5g 只有在**解封裝／封裝**時封包才經過 `upfgtp`，所以變化最大（且佔 80% 以上）的就是這種流量的 anchor（PSA）。
- `n_gtpu`：tcpdump 只數 UDP 2152 的封包數。只轉送 GTP-U 的 UPF（I-UPF）`gtp` 是 0、`n_gtpu` 不是 0。
- **不能用 eth0 的位元組**：實測 V3x 下載期間，UPF 會在 10 秒內連發約 2500 個 PFCP heartbeat，eth0 的數字被蓋掉。

| 項目 | 通過條件 |
|---|---|
| `mec_reachable` | 5 次至少 3 次回 2xx／3xx |
| `separate_anchors` | MEC 的 anchor ≠ 上網的 anchor（找不到明顯的 anchor → 無法判定） |
| `via_branching_upf` | 有另一個 UPF 在兩種流量都只轉送（I-UPF） |
| `remote_addr_consistent` | 旁證：MEC 回應的來源（`RemoteAddr`）是某個 UPF 的 IP 時，必須是 MEC 的 anchor（agent 沒做 NAT 時是 UE IP，不判定） |

不依賴 NAT，也不依賴 UPF 的名稱。L2 參考答案實測：MEC 時 psaupf2 `gtp`=4755、iupf1 `n_gtpu`=100、psaupf1 全 0；上網時 psaupf1 `gtp`=1680、iupf1 `n_gtpu`=40、psaupf2 全 0。

### V5：跨 NF 設定一致性（[consistency.py](consistency.py)，2026-11-20）

```bash
verify/consistency.py <trial>/configmaps.yaml tasks/L1/spec.json --services <trial>/cluster-state.txt [--subs <trial>/dataflow/subs.json]
```

NF 設定靠 ConfigMap 裡的檔名找（`amfcfg.yaml`、`smfcfg.yaml`…），UERANSIM 靠內容找（有 `supi` 的是 UE、有 `amfConfigs` 的是 gNB），不依賴 agent 取的名稱。全部讀成字串再正規化：`sd: 010203` 用一般的 YAML 讀會變成八進位，UERANSIM 的 `0x010203` 和十進位 `66051` 是同一個值。

| 類別 | 檢查 |
|---|---|
| PLMN | AMF（GUAMI、TAI、plmnSupportList）、SMF、AUSF、NRF、NSSF、gNB、UE 都是規格的 PLMN；SUPI 開頭是 MCC＋MNC |
| TAC | gNB 的 TAC 在 AMF 的 supportTaiList 裡 |
| S-NSSAI | AMF、SMF、NSSF、gNB、UE（session、configured-nssai）都要有規格的 slice，**多的記為 extra**；UE 的 default-nssai 不同記為 warn；AMF 宣告了但 SMF 沒服務的 slice 記為 warn |
| DNN | AMF supportDnnList、SMF（該 slice 下）、UPF dnnList、UE 的 APN |
| UE IP pool | SMF 給 UPF 的 pool 必須等於 UPF dnnList 的 cidr |
| 拓樸 | SMF upNodes 的 UPF 數量＝規格；links 不能指到不存在的節點；gNB 要直接連到 UPF；N3 interface 的 network instance |
| DNAI（坑 7） | 單一 UPF 卻帶 dnaiList → warn（subscriber 有 flow rule 或 slice 層級計費時會出問題） |
| 位址參照 | 所有 NF 的 nrfUri 相同；gNB 的 AMF 位址對得到 SCTP Service |
| 安全 | AMF 選的完整性／加密演算法 UE 都支援 |
| subscriber | 有規格 SUPI 的認證資料；K、OPc 和 UE 一致；訂閱的 slice、slice＋DNN 符合規格；多的 subscriber 記為 extra（要有 V6 匯出的資料，舊 trial 沒有） |

**多 UPF（L2，2026-11-23）**：SMF upNodes 有 2 個以上 UPF 時，UE IP pool 改成逐一比對，並檢查 ULCL。只有一個 UPF 時維持原本的檢查（18 個 L1 trial 重算結果完全相同）。

- SMF 的 UPF 節點 ↔ UPF 設定檔：chart 的位址是執行時才替換的佔位字（`IUPF1_SERVICE_NAME`、`POD_IP_ADDR`），所以依序用 (1) 佔位字／DNS 名稱的關鍵字出現在 ConfigMap 名稱裡 (2) nodeID 字串相同 (3) 角色（有沒有 N3）唯一對應。對不到記 `skip`。

| 檢查 | 內容 | 對應參考答案的坑 |
|---|---|---|
| `pool.match` | 每個 UPF 的 SMF pool 都在它自己的 dnnList cidr 裡 | — |
| `ulcl.enabled` | 有 specificPath（或 spec 有 MEC）時 SMF 要 `ulcl: true` | A |
| `ulcl.members` | 有 uerouting 群組包含規格的 SUPI；其他群組記 extra | — |
| `ulcl.default_path` | 群組 topology 是 gNB → I-UPF → 預設 anchor | — |
| `ulcl.mec_dest` | specificPath 的 dest 涵蓋 MEC IP；其他 dest 記 extra（例如 chart 預設的 1.0.0.1） | 照抄預設 |
| `ulcl.path` | path 的節點都存在、從接 gNB 的 UPF 開始、最後不是預設 anchor、相鄰節點在 SMF links 裡 | — |
| `ulcl.psa_route` | MEC PSA 的 dnnList cidr 涵蓋 UE 的 pool（UE IP 從預設 anchor 的 pool 分配） | C |
| `ulcl.psa2_n3` | v4.3.0 時 MEC PSA 要有 N3 interface（SMF 的 SelectPSA2 bug） | B |

反向測試 [tests/test_consistency_l2.py](tests/test_consistency_l2.py)：L2 參考答案（fixture）改錯 10 種＋1 種不該報錯，12/12。

刻意**不算 error** 的（18 個 trial 都能註冊、上網，且 V6 的封包證實流程沒用到）：
- NSSF 的 `taList` 沒有 208/93（chart 預設只有範例 PLMN 466/92）→ warn。V6 看到 AMF 在 PDU session 時有呼叫 NSSF，NSSF 仍回 200。
- NSSF 的 `nsiList`、`amfSetList` 裡的範例 slice／PLMN → info（chart template 寫死）。

### V6：NF 間 dataflow 與封包內容（[dataflow.sh](dataflow.sh)＋[dataflow_capture.sh](dataflow_capture.sh)＋[dataflow.py](dataflow.py)，2026-11-20）

```bash
verify/dataflow.sh tasks/L1/spec.json <out_dir>   # → <out_dir>/dataflow.json、<out_dir>/dataflow/（pcap、Pod 對照表、subscriber）
```

**觸發方式**（VM 端，都是實測出來的順序）：
1. 前提：UE 已經有 `uesimtun0`（V3 通過）。沒有的話不做，結果是無法判定（`pass: null`），因為重啟只會把壞掉的狀態攪得更亂。
2. 在 `cni0` 上開始抓封包（所有 Pod 在同一個 bridge 上，NF 間的流量都看得到）。
3. 經 tunnel ping 一下讓 UE 回到 CM-CONNECTED，再 `nr-cli … deregister switch-off`。UE 在 idle 時 switch-off 不會送出 Deregistration。
4. chart 的 UERANSIM UE 約 1 秒後會**自己重新註冊並建 PDU session**；15 秒內沒有才重啟 UE 的 Pod。經 tunnel ping 5 次產生 GTP-U。
5. 最後才重啟 gNB 錄 NG Setup，並確認 UE 能回來。**不能和 UE 同時重啟**：新的 UE 可能先連上「正在被替換」的舊 gNB，接著 radio link failure，AMF 留下殘缺的 context，之後 PDU session 一直重送。
6. 匯出 Pod／Service 對照表、MongoDB 的 subscriber 資料（給 V5 用）。
重啟 Pod 時，有 controller 管的就刪掉讓它重建（用 `crictl stop` 重啟 container 會累積 CrashLoop 的退避時間，實測第 4 次要等 40 秒以上）。整個 V6 約 1 分 50 秒。

**解碼**（host 端，tshark 3.6）：NGAP／NAS-5GS（`nas-5gs.null_decipher`，chart 預設 NEA0 所以讀得到）、SBI（HTTP/2 明文，對 Pod／Service 的 TCP port 做 decode-as）、PFCP、GTP-U。IP 依 Pod 清單對應到 NF（label `nf`，沒有的話看名稱／image）。
- `cni0` 上有些封包會出現兩次（一份是 Pod 的 MAC、一份是 cni0 的 MAC：經主機轉送了一次），先用 `editcap -D 20 -I 14` 去掉，否則 tshark 會把重複的那份當成新的 TCP 連線，請求和回應配不起來。
- SBI 的請求和回應用「IP＋port＋HTTP/2 stream ID」配對。HTTP/2 連線被 GOAWAY 關掉時，客戶端會在新連線上重送（實測：AUSF 的 5g-aka-confirmation），有重送成功的就不算沒回應。

**判定**：
| 類別 | 內容 |
|---|---|
| 流程（`reg.*`、`pdu.*`、`data.*`） | 28 個關鍵步驟依序出現、由對的 NF 送給對的 NF、SBI 回 2xx：Registration Request → AUSF／UDM／UDR 認證 → Authentication → Security Mode → UDM 註冊、取訂閱資料 → AM policy → Registration Accept／Complete → PDU Session Establishment Request → SM context → UDM sm-data → SM policy → PFCP Session Establishment → N1N2 → PDU Session Resource Setup → SM context modify → PFCP Session Modification → 上行／下行 GTP-U。步驟依 TS 23.502 4.2.2.2.2、4.3.2.2.1，以 free5GC v4.3.0 實際的呼叫為準（例如 free5GC 是直接溝通，不經 SCP） |
| NG Setup | 成功；PLMN、TAC 符合；gNB 宣告的 slice、**AMF 回應支援的 slice**（多的記 extra） |
| NAS 內容 | SUCI、5G-GUTI 的 PLMN；**Registration Accept 的 Allowed NSSAI**（多的記 extra）；PDU session 請求與 Accept 的 DNN、S-NSSAI；Accept 給的 UE IP＝`uesimtun0`；5QI、AMBR、DNS 記錄 |
| PFCP 內容 | 有上行（Access）和下行（Core）PDR；**PDI 不能只有 source interface**（坑 7）；上行 PDR 有 F-TEID、FAR 送往 Core；下行 FAR 送往 Access；UE IP 一致；cause＝accepted |
| TEID 一致 | AMF 給 gNB 的上行 TEID＝PFCP 給 UPF 的 F-TEID＝實際上行 GTP-U 的 TEID；gNB 回報的下行 TEID＝PFCP Modification 的 outer header＝實際下行 GTP-U 的 TEID；內層 IP＝UE IP |
| SBI | 沒有 4xx／5xx（同一錯誤合併計數）；沒有遺失的回應；記錄慢的呼叫、是否經 SCP、NF 間的通訊對象（`edges`） |

**多 UPF（L2，2026-11-23）**：重新註冊後有 2 個以上 UPF 收到 PFCP session 時，PFCP／TEID／GTP-U 改成分開看每個 UPF（只有一個 UPF 時程式走原本的路徑，L1 的 5 份封包新舊版本結果完全相同）。UPF 的 Pod label 是 `iupf1`／`psaupf1` 這類名稱時，角色一律當 `upf`（原本的 5 個誤判就是 label 不叫 `upf`），名稱另外保留。抓封包期間，spec 有 MEC 時會經 `uesimtun0` 連 MEC 3 次。

| 檢查 | 內容 |
|---|---|
| `pfcp.node_cause` | 每個 UPF 的 PFCP Establishment／Modification 都有回應且 accepted |
| `pfcp.n3_upf`、`pfcp.nodes` | 接 N3 的 UPF＝NGAP 告訴 gNB 的上行位址 |
| `pfcp.node_ul_dl`、`pfcp.empty_pdi`、`pfcp.ue_ip` | 每個 UPF（Establishment＋Modification 合起來）都有上行和下行 PDR、PDI 不是空的、UE IP 一致 |
| `teid.ul_ngap_pfcp`、`teid.dl_ngap_pfcp` | 同單一 UPF，但只看接 N3 的 UPF |
| `teid.n9` | 每條指向另一個 UPF 的 FAR（outer header）對方都有同一個 F-TEID 的 PDR |
| `pfcp.default_anchor` | N3 UPF 的預設上行 PDR 轉給哪個 UPF |
| `data.n9_internet` | 上網的流量在 N9 上只到預設 anchor，而且有回程 |
| `ulcl.pdr` | N3 UPF 有比對 MEC IP 的上行 PDR（SDF filter），FAR 轉給預設 anchor 以外的 UPF |
| `data.mec_ul`、`data.mec_dl` | 往 MEC 的封包在 N9 上只到分流規則指向的 PSA；PSA 有回程（沒有回程 → 坑 C） |

### L1 的需求項目（[tasks/L1/spec.json](../tasks/L1/spec.json)）

| 項目 | 怎麼判斷 |
|---|---|
| `upf_count` = 1 | 有 `upfgtp` 介面的 Pod，或 image 名稱像 UPF 的 Pod，數量要剛好 1 |
| `gnb_count` = 1、`ue_count` = 1 | `nr-cli --dump` 列出的 gNB／UE 數量 |
| `supi` | 要有這個 SUPI 的 UE，而且已註冊、PDU session 是 active |
| `plmn` | 這個 UE 的 `current-plmn` |
| `slice`、`dnn` | 這個 UE **實際使用中**的 PDU session 的 SST／SD 和 APN |
| `free5gc_version` | 所有核網 NF 的 image tag 都要是 v4.3.0（UERANSIM 另有版號，不檢查） |

### 副作用清單的內容

由 [hoststate.sh](hoststate.sh) 拍下 VM 狀態，再用 [sidefx.py](sidefx.py) 比對前後差異，分兩類：

- **infra**：`/etc` 裡的檔案變更（包括 `/etc/rancher/k3s/`）、K3s 自己的設定與 manifests、kernel module、主要的 sysctl、開機服務、K3s 是否重啟過、kube-system 與 cluster 層級的物件、node taint。
- **tooling**：用 apt／snap 裝的套件，以及 `/usr/local/bin`、`/opt` 下新增的執行檔。

- **auto**（不算 infra）：建 iptables／ipset 規則時 kernel 自動載入的 netfilter module（`xt_*`、`ip_set*`、`nf_*`…）。部署 free5GC 本身就會出現：`xt_sctp`（AMF 的 SCTP Service）、`xt_TCPMSS`（UPF 的規則）、`ip_set_hash_ip`／`xt_set`（K3s network policy 控制器）、`xt_statistic`（kube-proxy 多後端）。phase1-r2 發現後另列（2026-11-19）。

雜訊已排除：`ld.so.cache`、`resolv.conf`、K3s 自動產生的 containerd／CNI 設定、依需要自動起停的系統服務（例如 `systemd-timedated`）。

## 已測過的情況

| 情境 | 結果 |
|---|---|
| 參考答案 `S2-l1-ref`（v4.2.2） | V1–V3 全過（版本檢查是後來加的，v4.2.2 不符合目前的 L1 spec） |
| 參考答案，但把 UPF 縮成 0 個 | V1、V2 過，**V3 失敗** |
| 空的 `S1`（什麼都沒部署） | 全部失敗 |
| Opus pilot 結束後的狀態（v4.3.0） | 全過，包括版本檢查 |
| 副作用：只做正常 K8s 操作（建 namespace、Deployment、PVC） | 0 筆 |
| 副作用：模仿 Haiku 3（寫 K3s 設定並重啟）＋載入 kernel module＋改 sysctl | 4 筆 infra，全部抓到 |
| 副作用：`apt install tree` | 1 筆 tooling |
| V3x：參考部署（v4.2.2）正常狀態 | 全部通過（第一跳 UPF、DNS、HTTP 204、HTTPS 200、1 MB 下載），整個驗證約 9 秒 |
| V3x：UPF 擋掉所有 TCP | V3 **通過**、V3x **失敗**（http／https／下載） |
| V3x：UPF 丟掉 ≥ 1000 bytes 的封包（模擬 MTU 黑洞） | V3 **通過**、V3x **失敗**（小的 http 過，https 和下載失敗） |
| V3x：VM 擋掉所有 443 port（模擬外網問題） | https 標成無法判定（對照組也失敗）；下載改用 HTTP 備援網址，通過 |
| V3x：UPF 不回 ICMP time exceeded | 第一跳標成無法判定，其他照常 |
| phase1-r2 的 9 個真實 trial（v4.3.0） | V3x 全過，第一跳都是 UPF；副作用 infra／tooling 全 0，auto 4～5 筆 |
| runner dry run（假 token） | 被標成無效（`auth_error`），驗證與副作用清單都有產生 |
| V5：18 個真實 trial（phase1＋phase1-r2） | 全部 `pass`；`exact` 只有 2 個（Sonnet r2-1、Haiku r2-3），其餘多了 chart 預設的 PLMN／slice（見 [results/phase1-r2/ANALYSIS.md](../results/phase1-r2/ANALYSIS.md)） |
| V5：反向測試 [tests/test_consistency.py](tests/test_consistency.py) | 22/22：故意改錯 15 種設定（slice、PLMN、pool、link、DNN、TAC、AMF 位址、SUPI、演算法、nrfUri…）＋5 種 subscriber 錯誤都抓到；同一值的不同寫法不誤報；多個 namespace 的 configmaps.yaml 讀得到 |
| V6：VM 上的 Haiku r2-3 部署（v4.3.0）正常狀態 | 3 次擷取都 PASS、exact（54 項 ok；第 1 次是舊的觸發方式）；註冊 32–36 ms、PDU session 2.1–2.2 s（其中 2 s 是 SMF 等 CHF 的計費回應） |
| V6：subscriber 加 flow rule（[tests/dataflow_faults.sh](tests/dataflow_faults.sh) `flowrule`） | FAIL：SMF→PCF 建 SM policy 回 500、AMF→SMF 回 504，PDU session 建不起來。和參考答案坑 7（v4.2.2：session 建得起來、ping 不通）的表現不同，原因還沒確定（可能是測試用的 flow rule 格式不符合 v4.3.0） |
| V6：UPF 丟掉送往 gNB 的 GTP-U（`dl_drop`） | FAIL，只有一項 error：下行 GTP-U 沒有出現 |
| V6：拿掉 subscriber 的 smData（`no_smdata`） | FAIL：SMF→UDM sm-data 回 500、AMF→SMF 回 403，之後的步驟都沒出現 |
| L2 參考答案（從零部署，2026-11-23） | V1–V3x、V4 PASS（MEC anchor＝psaupf2、上網 anchor＝psaupf1、經過 iupf1）；V5 PASS（0 error）；V6 PASS（0 error，原本的 5 個誤判消失） |
| L2 反向：不加 `ulcl: true`（坑 A） | **V4 FAIL**（兩種流量同一個 anchor）；V5 `ulcl.enabled`；V6 `ulcl.pdr`、`data.mec_ul`。沒有 ULCL 時 SMF 每次建 session 會任選一個 PSA（V4 量到 psaupf1、V6 重新註冊後變成 psaupf2） |
| L2 反向：AnchorUPF2 不加 N3（坑 B） | **V4 FAIL**（同上）；V5 `ulcl.psa2_n3`；V6 `ulcl.pdr`、`data.mec_ul` |
| L2 反向：psaupf2 用 chart 預設 cidr（坑 C） | **V4 FAIL**（`mec_reachable`；anchor 仍判得出是 psaupf2）；V5 `ulcl.psa_route`；V6 只有 `data.mec_dl`（分流規則、N9 上行都對，PSA2 沒有回程） |
| runner dry run（L2，假 token） | `setup` 有套用、`setup_untouched=True`、MEC 應用不算副作用（infra 0） |

## 已知限制

1. **V2 依賴 UERANSIM 的 `nr-cli`**。如果 agent 改用其他 RAN 模擬器（例如 PacketRusher），V2 會找不到 UE，要另外加偵測方式。
2. ~~V3 沒有直接證明封包經過 UPF~~ → V3x 的 `first_hop_is_upf` 補上了。仍然的限制：UPF 不回 ICMP 時無法判定。多 UPF（L2 ULCL）實測：traceroute 看不到 I-UPF（它只重新封裝 GTP-U、不減 TTL），第一跳是 PSA，所以「經過 I-UPF」改由 V4 的 `via_branching_upf` 和 V6 的 N9 檢查證明。
2b. **V3x 依賴外部網站**（Google、Cloudflare、thinkbroadband）。用對照組和備援網址降低影響，但這些網站全部改版或下線時要更新 `WEB_TESTS`。
3. ~~需求檢查沒有檢查核網是否多設定了其他 slice~~ → V5 的 `extra`（設定）和 V6 的 Allowed NSSAI、NG Setup（實際行為）補上了。目前只記錄，「多的算不算錯」還沒決定，所以分成 `pass` 和 `exact` 兩種。
4. **只驗一個時間點**。不驗長時間穩定性。V6 會重新走一次註冊流程，並在最後重啟 gNB，算是一點點「重啟後能不能恢復」，但 gNB 重啟後 UE 常常不會自己回來（UERANSIM 的行為，參考部署還沒對照），所以只記為 info。
5. **還沒做的層**：安全性（privileged、hostNetwork、預設帳密）和可重現性（agent 有沒有留下能重跑的部署檔）。
7. **V6 的限制**：
   - 依賴 UERANSIM 的 `nr-cli`，以及 UE 去註冊後會自己重新註冊（沒有的話會改成重啟 UE Pod）。
   - 會重啟 agent 部署的 UE、gNB（Pod 名稱和 IP 會變），所以排在 V1–V3x 之後。
   - 假設 NF 都在 Pod 網路上；UE 用 hostNetwork 時改抓 `any`，還沒實測。
   - NAS 要是 NEA0（不加密）、SBI 要是明文 HTTP/2 才讀得到內容；agent 改成加密時，流程判定仍可用，內容判定會變成缺資料。
   - 流程步驟是照 free5GC v4.3.0 的實作寫的；多 UPF（L2）的 PFCP／N9／ULCL 已另外處理（2026-11-23），但流程步驟只要求「至少一個 UPF」回應。
   - 只觸發註冊和 PDU session 建立；去註冊、handover、閒置後的 service request 沒有測。
8. **V5 看的是 ConfigMap**。agent 如果把設定放在 Pod 的環境變數、Secret 或 image 裡，V5 會找不到（記為 skip）。subscriber 的部分要有 V6 匯出的資料，2026-11-20 以前的 trial 沒有。
6. **副作用清單只看主機層**。agent 在自己的 namespace 裡建的東西是正常工作，不列入。cluster 層級的物件（CRD、StorageClass、ClusterRoleBinding）有列入，但 helm chart 本身也會建這類物件，要人工判斷。

## 輸出

```bash
verify/verify.sh tasks/L1/spec.json -o out.json   # stdout 是一行 JSON，stderr 是人看的摘要
```

每個 trial 目錄裡的對應檔案：

- `verify.json`：完整結果，含每個 Pod 的狀態與 image
- `verify-summary.txt`：摘要
- `hoststate-before.txt`、`hoststate-after.txt`、`sidefx.json`：副作用清單（2026-11-19 起才有）
- `consistency.json`、`consistency-summary.txt`：V5（2026-11-20 起；phase1、phase1-r2 的 18 個 trial 已補算）
- `dataflow.json`、`dataflow-summary.txt`、`dataflow/`（`capture.pcap`、Pod／Service 對照表、`subs.json`）：V6（2026-11-20 起）。`dataflow.json` 的 `sequence` 是解碼後的訊息順序，`edges` 是 NF 間的通訊對象與次數
