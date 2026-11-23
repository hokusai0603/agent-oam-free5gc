# Phase 1 分析紀錄

## Pilot：Opus 5.5 × L1 × 1（2026-11-18，不計入正式結果）

紀錄：`results/phase1-pilot/L1-normal-opus-5-5-high-20261118-171032/`

### 結果

| 項目 | 值 |
|---|---|
| 驗證 | **PASS**（V1 15 個 Pod 全 Ready、V2 註冊與 PDU session、V3 經 uesimtun0 ping 8.8.8.8、需求全部符合） |
| 時間 | agent 817 s（13.6 分）；整個 trial（還原 39 s ＋ 收集 ＋ 驗證）約 16 分 |
| 輪數／工具 | 48 輪，47 次 Bash（沒有用 Edit／Write，檔案都用 heredoc 寫） |
| Token | 輸出 25,347（其中 thinking 8,756）；cache 讀 3,000,417、cache 寫 89,565；一般輸入 96 |
| 費用 | 估計 $1.82（訂閱，依 API 價格估算，非實際扣款） |
| 有效性 | 有效：全程只有 `claude-opus-5-5`，無限流、無認證錯誤 |

### 過程（照時間順序）

1. 盤點環境（K3s、gtp5g、CNI、可上網），**`git clone` free5gc-helm 最新 main**（commit `07139fc`，2026-11-16），讀 chart 的 values 與 template。
2. 自己寫 `free5gc-values.yaml`：`userPlaneArchitecture: single`；把 AMF 預設廣播的第二個 slice（112233）拿掉；修 UE 的 `default-nssai`（chart 預設 sd 是 1，改成 010203）。先 `helm template` 檢查再安裝。
3. **踩到 MongoDB PV 的坑**（chart 寫死 microk8s）：看 `describe` → 找到 `extraDeploy` → 改用 `local-path`，並知道 StatefulSet 的 volumeClaimTemplate 不能就地改，於是整個重裝。
4. **踩到 v4.3.0 的新坑**：UPF 一直 crash，原因是預設 `natIfCIDR: 192.168.0.0/16` 在 pod 裡找不到網卡。它 clone go-upf 原始碼讀 NAT 的實作，改成 `natifname: eth0`。
5. **subscriber**：先確認 PFCP association 成功，再 clone free5GC webconsole 原始碼，讀 API 與 Postman collection，自己寫 subscriber JSON（OPC 放在正確欄位）和建立腳本。
6. **先建 subscriber 才裝 UERANSIM**：因此沒有遇到「UE 重啟、兩個 UE 互踢」的問題。
7. 驗證：看 UE log、ping 8.8.8.8；**還在 VM 上用 tcpdump 確認流量真的走 gNB → GTP-U → UPF → NAT 出去**，並解碼 GTP-U 的 TEID 和內層封包。最後寫了自己的 `verify.sh`。

### 和參考答案比較（[tasks/L1/reference](../../tasks/L1/reference/README.md) 的 8 個坑）

| # | 坑 | Opus 的情況 |
|---|---|---|
| 1 | 只設 `userPlaneArchitecture: single` | 沒遇到：v4.3.0 chart 的 SMF 已有 `_smf-single.tpl`，只設這個就夠 |
| 2 | single-upf values 的 SMF image 是舊版 | 沒遇到（新版 chart） |
| 3 | MongoDB PV 寫死 microk8s | **遇到，自己修好** |
| 4 | 沒建 subscriber | 沒犯：主動讀原始碼建立 |
| 5 | OP／OPC 欄位 | 沒犯：OPC 放在 `opc` |
| 6 | UE image 沒有 curl | 遇到，改用 ping 和 tcpdump 驗證 |
| 7 | SMF 的 UPF 有 `dnaiList: [mec]` | **設定裡仍然有**，但它建的 subscriber `FlowRules` 是空的，不會產生沒有 DNAI 的 PCC rule，所以沒觸發。**是避開，不是發現** |
| 8 | rollout restart 造成雙 UE | 沒遇到：先建 subscriber 再裝 UE |
| 新 | v4.3.0 UPF 的 `natIfCIDR` 預設值會 crash | **遇到，讀 go-upf 原始碼後修好** |

比參考答案多做的：拿掉多餘 slice、修 UE `default-nssai`、用 tcpdump 證明封包路徑、指出「UPF 重啟後 SMF 也要重啟」（SMF 只在啟動時解析一次 UPF IP）。

### 觀察與對實驗設計的影響

1. **L1 在 Opus 5.5 的能力範圍內**，而且做得比參考答案仔細。這和會議前的推測一致。
2. **版本沒有固定**：prompt 沒指定版本，agent 拿了 free5gc-helm 最新的 main。實驗期間 main 若更新，不同 trial 會部署到不同版本，變成干擾變數。→ 需要決定是否在 prompt 裡指定版本（見下方「待決定」）。
3. **停用 WebSearch／WebFetch 擋不住查資料**：agent 用 `git clone` 讀了 free5gc-helm、go-upf、webconsole 的原始碼。這是很合理的工程行為，但代表「模型本身懂多少」和「會不會查原始碼」混在一起。分析時要把「讀了哪些原始碼」當成一個觀察項目。
4. 坑 7 的觸發條件比原本以為的窄：只有 subscriber 有 flow rule 時才會發生。可以拿來做 L7 的故障題（給 subscriber 加 flow rule）。

### 錯誤分類

本次通過，沒有失敗。過程中自己修好的問題：E4 基礎架構 × 2（MongoDB PV、UPF NAT 介面）。

### 決定（2026-11-18）

- 正式矩陣的 prompt 指定 free5GC v4.3.0（free5gc-helm commit `07139fc`），驗證加上 NF image 版本檢查。因此正式 trial 和 pilot 的 prompt 不同（多一行版本），pilot 不併入結果。
- Fable 5.2 先不買額度：Phase 1 只跑 Opus、Sonnet、Haiku 各 3 次。

---

## 正式矩陣：Opus／Sonnet／Haiku 5.5 × L1 × 3（2026-11-18 18:47–20:50）

條件見 [README.md](README.md)：prompt 指定 free5GC v4.3.0（free5gc-helm `07139fc`），effort high，模型交錯執行。紀錄在 `results/phase1/<trial_id>/`，彙整在 `trials.jsonl`、`matrix.log`。

### 結果

**9／9 通過**（V1、V2、V3、需求含版本檢查全過），9 個都有效（沒有換模型、限流或認證錯誤）。

| 模型 | 通過 | agent 時間（分，3 次） | 平均 | 輪數 | 輸出 token（平均） | 估計費用（平均） |
|---|---|---|---|---|---|---|
| Opus 5.5 | 3/3 | 9.1、7.4、5.8 | 7.4 | 75、51、36 | 21.5K | $2.81、$1.60、$1.31（$1.91） |
| Sonnet 5.5 | 3/3 | 9.1、8.5、13.1 | 10.2 | 38、38、38 | 16.5K | $0.71、$0.56、$0.60（$0.62） |
| Haiku 5.5 | 3/3 | 16.1、16.9、14.5 | 15.8 | 89、68、63 | 63.1K | $0.63、$0.37、$0.44（$0.48） |

9 個 trial 估計共 $9.04（訂閱，依 API 價格估算）。Haiku 第 2 次的時間有一部分是在等 Docker Hub 限流解除（見「環境干擾」）。

### 每個 trial 遇到了什麼

「事先處理」= 安裝前讀 chart 或 `helm template` 就發現並改好；「遇到→修好」= 部署後才從錯誤中發現。

| trial | MongoDB PV（坑 3） | UPF `natIfCIDR`（v4.3.0 新坑） | 其他自己修好的問題 | 建 subscriber 的方法 | 資料面驗證 |
|---|---|---|---|---|---|
| Opus 1 | 遇到→修好 | 事先處理 | — | WebUI API（讀 webconsole 原始碼、Postman collection） | ping、tcpdump GTP-U、經 tunnel 的 TCP |
| Opus 2 | 事先處理 | 事先處理 | — | WebUI API（clone webconsole v1.4.6 查格式） | ping、tcpdump、MASQUERADE 計數 |
| Opus 3 | 事先處理 | 事先處理 | — | WebUI API（沒讀原始碼） | ping、tcpdump |
| Sonnet 1 | 遇到→修好 | 事先處理 | 覆寫 AMF 設定時漏欄位 → AMF `Config validate Error` | WebUI API | ping、DNS |
| Sonnet 2 | 事先處理 | 遇到→修好，並重啟 SMF | 事後才把 AMF 多的 slice 拿掉（helm upgrade） | WebUI API | ping、`upfgtp` 計數 |
| Sonnet 3 | 遇到→修好 | 事先處理 | 覆寫 AMF 漏 timer 欄位；行號差一；`pkill -f` 砍到自己的 shell | WebUI API | ping、tcpdump |
| Haiku 1 | 遇到→修好 | 遇到→修好 | 註冊被拒（見下方「新坑」）→ 改用 WebUI API 又遇到 `Token` header、`gpsi` panic | 先直接寫 MongoDB → 失敗 → WebUI API | ping、MASQUERADE、`upfgtp` |
| Haiku 2 | 事先處理 | 遇到→修好（第一次沒套用到） | Docker Hub 429：把 Deployment 縮成 0、自己預拉 image、再逐一恢復 | 直接寫 MongoDB → 失敗 → 從 UDM 二進位檔找欄位名 | ping、`upfgtp` 計數 |
| Haiku 3 | 遇到→修好（PV／PVC 卡 finalizer，手動清） | 事先處理 | **改了 K3s 設定並重啟 K3s**（`allowed-unsafe-sysctls`，非必要） | dbpython → 少 `imsi-` 前綴 → 格式錯 → clone UDM 原始碼 | ping、`upfgtp` 計數 |

### 新坑：v4.3.0 不能直接把 subscriber 寫進 MongoDB

Haiku 三次都想繞過 WebUI，直接寫 MongoDB（自己寫 mongosh 腳本，或用 chart 附的 `dbpython`），然後都卡在同一串錯誤：

1. UE 註冊被拒（`CONGESTION`），AMF log：`Nausf_UEAU Authenticate Request Error: 500`
2. UDM：`sequenceNumber` 型別錯誤。v4.3.0 要求物件，舊格式是字串
3. 改好後 UDM：`Nil PermanentKey`。v4.3.0 的 UDM 讀的是 `encPermanentKey`／`encOpcKey`，不是 `permanentKey.permanentKeyValue`／`opc`

WebUI API 會自動寫成新格式，所以 Opus、Sonnet（全部走 WebUI API）都沒遇到。**這是 Haiku 和另外兩個模型最明顯的差別：知不知道該在哪一層操作。** Haiku 靠讀 log、讀二進位檔或原始碼一路追到底，最後都修好了，但時間和輸出 token 都是 Opus 的 2～3 倍。

### 坑 7（`dnaiList: [mec]`）

9 個 agent 建的 subscriber `FlowRules` 都是空的，所以都沒觸發。L1 的成績不受它影響；要測這個坑，要在 L7 故意加 flow rule。

### 觀察

1. **在版本固定的 L1 上，三個 Claude 5.5 模型都沒有碰到能力邊界**（9/9）。通過率分不出模型強弱，差別在**效率和走的路**：
   - Opus：安裝前會先讀 chart、`helm template` 檢查，已知的兩個坑 6 次中 5 次事先處理；越後面的 trial 越乾淨（輪數 75 → 51 → 36）。
   - Sonnet：最穩定（三次都 38 輪），主要的錯是手寫覆寫檔時漏欄位（E2）。
   - Haiku：會繞遠路（直接改資料庫），也比較會做超出需要的系統變更，但除錯的韌性夠，最後都成功。
2. **Haiku 費用只比 Sonnet 少一點**（$0.48 對 $0.62）：單價便宜很多，但輪數多、讀的 context 多（平均 cache read 720 萬 token，Sonnet 是 200 萬）。
3. **副作用要列入評分**。Haiku 3 為了「預防」UPF 的 sysctl 被拒，改了 `/etc/rancher/k3s/config.yaml` 並重啟 K3s。這次沒造成問題，但在共用的維運環境，這種沒必要的基礎設施變更就是風險（E7）。V1–V3 抓不到這類問題 → 建議驗證加一項「VM／K3s 層的變更清單」（比對 `/etc` 與 K3s 設定的 diff）。 → **已完成（2026-11-19）**：`side_effects` 欄位，見 [verify/README.md](../../verify/README.md)。本矩陣的 trial 沒有基準，無法回溯。
4. **所有模型都會 `git clone` 原始碼來查**（webconsole、UDM、free5gc 主 repo），停用 WebSearch／WebFetch 擋不住。這是合理的工程行為，記錄成觀察項目，不算違規。
5. **agent 會寫 Claude Code 的 memory**（Opus 1 寫了 `~/.claude/projects/.../memory/`、Opus 2 試著讀它）。每個 trial 都從 snapshot 還原，所以不會帶到下一個 trial；之後如果改成不還原的連續任務，要記得清掉。

### 環境干擾（要修）

- **Docker Hub 限流**：每個 trial 都從零拉約 17 個 image，同一個出口 IP 在幾小時內拉了上百次。Haiku 2 遇到 `429 toomanyrequests`（匿名額度用完），另外有 4 個 trial 看到 kubelet 的 `pull QPS exceeded`。這次 agent 都撐過去了，但會把時間拉長，還可能讓 trial 失敗，而且失敗原因跟模型無關。
  → 建議在 host 架一個 Docker Hub 的 pull-through cache，並在 snapshot 的 `/etc/rancher/k3s/registries.yaml` 設成 mirror。對 agent 是透明的，image 不會預先放進 VM（不會變成提示）。
  → **已完成（2026-11-18）**：host 上的 `oam-registry-cache` ＋ `S1-agent`／`S2-agent` 重存。見 [env/README.md](../../env/README.md#docker-hub-快取)。
- **runner 修正**：`-p` 模式下，背景工作結束時 agent 會被喚醒，一個 trial 可能有好幾個 `result` 事件。`summarize.py` 已改成輪數、時間、token 加總，費用取最後的累計值，最終回報取最長的一段；10 筆紀錄都重新產生過了。

### 錯誤分類（自己修好的問題；沒有任何失敗）

| 類別 | 次數（trial） |
|---|---|
| E2 domain 知識 | Sonnet 1、Sonnet 3（AMF 覆寫漏欄位）；Haiku 1、2、3（v4.3.0 subscriber 格式） |
| E4 基礎架構 | MongoDB PV：Opus 1、Sonnet 1、Sonnet 3、Haiku 1、Haiku 3；UPF NAT：Sonnet 2、Haiku 1、Haiku 2 |
| E5 操作順序 | Sonnet 2（UPF 重建後 SMF 要重啟，自己發現）；Haiku 2（UE 比 gNB 早起來，重啟 UE） |
| E7 不必要的變更 | Haiku 3（改 K3s 設定、重啟 K3s） |
| E6、E8 | 無 |

### 對下一步的意義

- L1 對 Claude 模型太簡單，**要找 Claude 的邊界得往 L2（MEC／ULCL）、L3（多 slice）或 L7（故障修復）走**。
- 對開源模型（Phase 2），L1 剛好是好的起點：Haiku 已經得繞一大圈才完成，小模型很可能就卡在 subscriber 格式或 MongoDB PV 這幾步。這些也是 harness（Phase 3）最先該補的地方。
