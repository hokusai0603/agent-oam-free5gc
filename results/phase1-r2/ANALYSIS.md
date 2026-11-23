# Phase 1 重跑（phase1-r2）分析

- 日期：2026-11-19 12:56–15:03
- 設定和 [phase1](../phase1/) 完全相同：L1、prompt-normal、effort high、從 S1-agent 開始、free5GC v4.3.0（free5gc-helm `07139fc`）、Docker Hub 快取
- 和 phase1 不同的地方：驗證多了 **V3x**（抓網頁／DNS／第一跳，見 [verify/README.md](../../verify/README.md)）、**嚴格版 PASS**、**副作用清單**
- 目的：用新的驗證方式再跑一次，確認 phase1「全部通過」不是 ping 高估的結果

## 結果

| 模型 | PASS | 嚴格版 PASS | 實際時間（秒） | 模型思考時間（秒） | turns | 估算費用 |
|---|---|---|---|---|---|---|
| Opus 5.5 | 3/3 | 3/3 | 457 / 1387 / 1082 | 278 / 261 / 208 | 50 / 51 / 52 | $1.76 / $1.57 / $1.72 |
| Sonnet 5.5 | 3/3 | 3/3 | 480 / 398 / 405 | 208 / 164 / 182 | 52 / 42 / 36 | $0.99 / $0.74 / $0.60 |
| Haiku 5.5 | 3/3 | 3/3 | 404 / 859 / 737 | 206 / 331 / 211 | 62 / 78 / 47 | $0.27 / $0.45 / $0.18 |

- 總費用約 $8.29（估算；實際走訂閱額度）。9 個 trial 都有效，沒有限流或 image 拉取失敗。
- 「模型思考時間」= transcript 的 `duration_api_ms`；實際時間減掉它，大致就是等指令執行（等 Pod、等 helm）的時間。
- 加上 phase1：每個模型 **6/6 通過**，Wilson 95% 下界約 0.61；三個模型合計 18/18，下界約 0.82。嚴格版目前只有本輪的 3/3（下界 0.44），9/9 合計下界 0.70。

## V3x：沒有發現 ping 高估

9 個 trial 的 V3x 全部通過，而且結果很一致：

- **第一跳**：全部是 UPF 的 Pod IP，第二跳 `10.42.0.1`（cni0），第三跳 `10.0.2.2`（VirtualBox NAT），和參考部署相同。沒有任何 trial 的流量繞過 UPF。
- **HTTP 204、HTTPS 200（約 87 KB）、1 MB 下載**：全部成功，來源 IP 都是 UE IP（10.60.0.1）。
- 下載時間大多 0.2 秒左右；Haiku 第 1 輪是 13 秒，但完整下載、判定通過（推測是當時外網較慢，單一點不足以下結論）。

所以 phase1 用 ping 判定的「通過」是成立的；在 L1 這個難度，三個模型都會把 UPF 的 NAT 設好，沒有出現「ping 會過但 TCP 不通」的部署。

## 副作用：agent 沒有改主機

| | infra | tooling | auto |
|---|---|---|---|
| 9 個 trial | 全部 0 | 全部 0 | 4～5 |

- **auto** 是部署 free5GC 時 kernel 自動載入的 netfilter module：`xt_sctp`（AMF 的 SCTP Service）、`xt_TCPMSS`（UPF 的 iptables 規則）、`ip_set_hash_ip`／`xt_set`（K3s 的 network policy 控制器）、`xt_statistic`（kube-proxy 多後端負載平衡，只出現在 Sonnet 第 1 輪）。transcript 裡沒有任何 `modprobe`／`insmod`。
- 一開始這些被算成 infra（每個 trial 4～5 筆）；`sidefx.py` 已改為另列 `auto`，不算 infra，並用每個 trial 的 hoststate 前後快照重算。
- 對照 phase1：Haiku 第 3 輪改了 K3s 設定並重啟 K3s（E7）。這次 9 個都沒有改 K3s、沒有改 sysctl、沒有裝套件。Opus 第 1 輪有寫 `which tcpdump || sudo apt-get install tcpdump`，但 VM 已經有 tcpdump，實際沒有安裝。

## 各 trial 碰到的坑

| trial | MongoDB PV（坑：Pending） | subscriber 建法 | UPF NAT | 其他自己修好的問題 |
|---|---|---|---|---|
| Opus 1 | 事先改好 | WebUI API | `natifname: eth0` | — |
| Opus 2 | 碰到，清 PV／PVC 後重裝 | WebUI API | `natifname: eth0` | AMF 覆寫漏了 timer 欄位 → `Config validate Error` → 補上 |
| Opus 3 | 碰到，清 PV／PVC 後重裝 | WebUI API | `natifname: eth0` | —（SMF 的 `DNAI[]` 錯誤沒處理，見下方） |
| Sonnet 1 | 碰到 | WebUI API | `natIfCIDR: 10.42.0.0/16` | 一開始另外在 values 的 `wrapperAdditionalCommands` 手寫 MASQUERADE；確認 UPF 會自己加規則後拿掉，`helm upgrade` 並重啟 SMF、gNB、UE 重驗 |
| Sonnet 2 | 碰到 | WebUI API | `natIfCIDR: 10.42.0.0/16` | — |
| Sonnet 3 | 碰到 | WebUI API | `natIfCIDR: 10.42.0.0/16` | — |
| Haiku 1 | 事先改好 | WebUI API | `natifname: eth0` | WebUI API 用錯 header（`Bearer`→`Token`）、少 `gpsis` 造成 panic |
| Haiku 2 | 碰到 | **直接寫 MongoDB**（掉進 v4.3.0 的坑，後來自己修好） | `natifname: eth0` | UE 的 PDU type 大小寫（`IPV4`→`IPv4`） |
| Haiku 3 | 事先改好 | WebUI API | `natifname: eth0` | AMF 覆寫漏了 timer 欄位 → `Config validate Error` → `helm upgrade` 補上 |

（2026-11-20 更正：對照 transcript 的指令與輸出、以及每個 trial 最終的 `configmaps.yaml` 逐筆確認過。原本這張表依關鍵字判斷，把 Sonnet 的 NAT 誤寫成「自己加 MASQUERADE」：`wrapperAdditionalCommands` 這個字在 9 個 transcript 裡都出現過，因為它是 chart values 的欄位名稱。兩種 NAT 寫法都可行：UPF 用 `natifname` 直接指定介面，或用 `natIfCIDR` 依 Pod 網段找介面。）

- **Haiku 2 自己修好了 subscriber 的坑**：寫進 MongoDB 後 UDM 解析失敗，它從 log 找出 `sequenceNumber` 要是物件，再去讀 free5GC v4.3.0 的 `test/mongodb.go`，改用 `encPermanentKey`／`encOpcKey`，最後通過。phase1 的 Haiku 3 次都直接寫 MongoDB；這次只有 1 次，另外 2 次改用 WebUI API，所以 Haiku 這次快很多（第 1 輪 404 秒）。
- **Opus 2、3 慢的原因是「盲等」**：裝完核網就跑「每 15 秒檢查一次，最多 40 次」的等待迴圈，MongoDB 卡在 Pending 也照等。Opus 2 等了約 10 分鐘才回頭查原因；Opus 3 原本想用 Monitor 工具等，但工具沒有先載入、呼叫失敗，改用同樣的迴圈，約 7 分鐘後才回頭查。模型思考時間只有 200 多秒，其餘都在等。這是 E5（操作順序／流程）類的效率問題，不影響成功。
- **AMF 覆寫漏欄位**：Opus 2、Haiku 3 為了拿掉多的 slice 覆寫 AMF 的設定，chart 的 AMF 設定是整段替換，漏了 T3513 等 timer，AMF 啟動時 `Config validate Error`，兩者都自己補上。第一次（phase1）只有 Sonnet 犯過，這次三個模型都可能犯，屬於 E2／E4 之間（不熟 Helm 的合併規則）。
- **坑 7 這次有觸發一次，而且是新的觸發方式**：9 個 trial 的 subscriber `FlowRules` 都是空的，但 Opus 3 建的 subscriber 帶了一筆 slice 層級的計費設定（`ChargingDatas`，`dnn`、`filter` 都是空字串）。PCF 因此下發 `PccRuleId-1`，SMF 找不到符合 `DNAI[]` 的 UPF（chart 的單一 UPF 帶著 `dnaiList: [mec]`），記下 `apply sm policy decision error: fail to create data path`。預設的資料路徑仍然建立成功，V3、V3x 都通過，所以 L1 不受影響。Opus 3 看到了這個錯誤，判斷不影響連線、也不改鎖定版本的 chart，沒有處理，算合理判斷。**結論：坑 7 不只由 FlowRules 觸發**，已補進 [參考答案的說明](../../tasks/L1/reference/README.md)；之後做 L7 故障題時也可以用這個方式。
- 3 個 Opus trial 和 Haiku 1 都在 VM 裡寫了 Claude Code 的 memory 檔（`~/.claude/projects/.../memory/`）；每個 trial 都從快照重來，不會帶到下一個 trial。

## 和 phase1 比較

| | phase1 | phase1-r2 |
|---|---|---|
| PASS | 9/9 | 9/9 |
| 嚴格版 PASS | 未量 | 9/9 |
| 費用（Opus／Sonnet／Haiku 合計） | $5.72 / $1.87 / $1.44 | $5.05 / $2.33 / $0.90 |
| 實際時間中位數（Opus／Sonnet／Haiku） | 442 / 543 / 967 秒 | 1082 / 405 / 737 秒 |
| 環境干擾 | Haiku 2 碰到 Docker Hub 429 | 無（快取） |
| 主機變更（infra） | Haiku 3 改 K3s 設定＋重啟 | 無 |

- 時間變異很大，主要來自「有沒有碰到 MongoDB PV 坑」和「碰到後怎麼等」，不是模型本身的速度；每個模型 3 次樣本，時間和費用的排名不穩定，不應拿來排名。
- 成功率在 L1 已經飽和，無法區分這三個模型。

## 結論與下一步

1. L1 對 Claude 5.5 三個等級都太簡單：成功率 18/18，連嚴格版也全過，副作用也乾淨。唯一的例外是「有沒有清掉 chart 預設的範例值」：若以 V5 的 `exact` 為準只有 2/18（見最後一節，標準還沒決定）。要量出差異，需要更難的任務（L2 ULCL 多 UPF、L7 故障排除）或 Phase 2 開源模型。
2. V3x 在 L1 沒有抓到額外的失敗，但它在負面測試裡證明抓得到 TCP 不通和 MTU 黑洞（見 verify/README.md），保留作為之後任務的預設檢查。
3. 效率指標（實際時間、等待時間）在 L1 比成功率更有區分度，例如 Opus 的盲等。之後可以考慮把「等待時間＝實際時間－模型思考時間」列進 meta。

## 跨 NF 設定一致性（V5）

2026-11-20 補做，用每個 trial 存下來的 `configmaps.yaml` 靜態分析，不必重跑（方法見 [verify/README.md](../../verify/README.md)）。第一次（[phase1](../phase1/)）的 9 個也一起補算，結果在各 trial 目錄的 `consistency.json`。

**18 個都 `pass`（沒有 error），只有 2 個 `exact`**（沒有多設規格外的東西）。沒有任何一個 trial 的 NF 之間互相矛盾；差別都在「有沒有清掉 chart 預設的範例值」：

| trial | AMF 多的 slice | NSSF 多的 slice | AUSF 多的 PLMN | NSSF taList 沒有 208/93 | UE default-nssai 不是 010203 | exact |
|---|---|---|---|---|---|---|
| 第一次 Opus 1 | — | — | 123/45 | ✓ | — | — |
| 第一次 Sonnet 1 | — | — | 123/45 | ✓ | — | — |
| 第一次 Haiku 1 | 112233 | 4 個 | 123/45 | ✓ | ✓ | — |
| 第一次 Opus 2 | 112233 | 4 個 | 123/45 | ✓ | — | — |
| 第一次 Sonnet 2 | — | 4 個 | 123/45 | ✓ | — | — |
| 第一次 Haiku 2 | 112233 | 4 個 | 123/45 | ✓ | ✓ | — |
| 第一次 Opus 3 | — | 4 個 | 123/45 | ✓ | — | — |
| 第一次 Sonnet 3 | — | — | 123/45 | ✓ | — | — |
| 第一次 Haiku 3 | 112233 | 4 個 | 123/45 | ✓ | ✓ | — |
| r2 Opus 1 | — | — | 123/45 | —（有 208/93，另多 466/92） | — | — |
| r2 Sonnet 1 | — | — | — | ✓ | — | ✓ |
| r2 Haiku 1 | 112233 | 4 個 | 123/45 | ✓ | ✓ | — |
| r2 Opus 2 | — | 4 個 | 123/45 | ✓ | — | — |
| r2 Sonnet 2 | — | — | 123/45 | — | — | — |
| r2 Haiku 2 | — | 4 個 | 123/45 | ✓ | — | — |
| r2 Opus 3 | — | 4 個 | 123/45 | ✓ | — | — |
| r2 Sonnet 3 | — | — | 123/45 | ✓ | — | — |
| r2 Haiku 3 | — | — | — | — | ✓ | ✓ |

- 「NSSF 多的 slice」是 chart 預設的 1/000003、1/112233、2/000001、2/000002。
- **AMF 多宣告 112233 的 5 個 trial 裡有 4 個是 Haiku**。這些 trial 的 AMF 宣告了 SMF 沒有服務的 slice（V5 記為 warn）。V2／V3 抓不到，因為 UE 只用 010203。
- 全部 18 個都帶著 chart 預設的 `dnaiList: [mec]`（單一 UPF，坑 7 的潛在條件，記為 warn）。r2 Opus 3 的 `DNAI[]` 錯誤就是它造成的。
- NSSF taList 沒有 208/93 不算錯：V6 的封包證實 AMF 在 PDU session 時有呼叫 NSSF，NSSF 仍回 200。
- subscriber（UDR）的部分舊 trial 沒有資料（skip）；之後的 trial 由 V6 從 MongoDB 匯出後一併比對。

「多設了規格外的東西算不算錯」（`pass` 還是 `exact`）還沒決定。如果以 `exact` 為準，18 個裡只有 2 個通過，L1 就不再是 18/18。
