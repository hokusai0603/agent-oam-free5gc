# Phase 1 L2（ULCL＋MEC）分析

- 日期：2026-11-23 16:39 – 2026-11-24 17:13（中間因 529 和訂閱上限中斷，見「無效 trial」）
- 任務：[tasks/L2/prompt-normal.md](../../tasks/L2/prompt-normal.md)。從零開始（起點 `S1-agent`，同 L1），用 ULCL 把往 MEC（`10.43.200.10`）的流量經 I-UPF 分到 MEC 專用的 PSA-UPF，上網走另一個 PSA-UPF
- 其他條件和 [phase1](../phase1/README.md) 相同：free5GC v4.3.0（free5gc-helm `07139fc`）、Claude Code 2.1.325 headless、effort high、60 分鐘／$15 上限、WebSearch／WebFetch 停用（VM 仍可上網，可以 `git clone`）、無人工介入
- 和 L1 不同的地方：
  - MEC 應用是環境的一部分（[setup/mec-app.yaml](../../tasks/L2/setup/mec-app.yaml)），runner 在 agent 開始前套用，結束後比對有沒有被改（`setup_untouched`）
  - **V4 MEC 路徑算進 PASS**：看每個 UPF netns 的 `upfgtp` 計數判斷 MEC 和上網各從哪個 UPF 出去，並確認兩者都經過 I-UPF（[verify/README.md](../../verify/README.md)）
  - V5、V6 改成多 UPF 版本（仍然只記錄，不算進 PASS）
- 參考答案：[tasks/L2/reference](../../tasks/L2/reference/)。chart 預設的 ULCL 不會導流，要修 3 個坑：
  - **A**：SMF 設定沒有 `ulcl: true`
  - **B**：v4.3.0 SMF 的 `SelectPSA2` 要求 PSA2 也有 N3 介面（上游 a510e92 才修）
  - **C**：psaupf2 的 cidr `10.60.128.0/17` 不含 UE 的 IP，MEC 的回應回不到 tunnel

## 結果

| 模型 | PASS | 實際時間（分） | 模型思考時間（分） | turns | 輸出 token | 估算費用 |
|---|---|---|---|---|---|---|
| Opus 5.5 | **3/3** | 30.6 / 27.9 / 14.1 | 7.8 / 7.2 / 6.3 | 65 / 87 / 75 | 32K / 42K / 38K | $2.52 / $3.87 / $2.96 |
| Sonnet 5.5 | **3/3** | 29.6 / 33.0 / 30.0 | 7.7 / 8.8 / 6.1 | 70 / 86 / 66 | 47K / 50K / 33K | $1.62 / $1.97 / $1.40 |
| Haiku 5.5 | **3/3** | 32.0 / 22.9 / 24.9 | 12.3 / 9.2 / 10.6 | 180 / 115 / 111 | 152K / 116K / 124K | $2.27 / $1.02 / $1.27 |

- **9/9 通過**，嚴格版（加 V3x）也是 9/9。每個模型 3/3 的 Wilson 95% 下界約 0.44；三個模型合計 9/9，下界約 0.70。
- 9 個 trial 的 V4 全部通過：MEC 的 anchor 都是 psaupf2、上網的 anchor 都是 psaupf1，而且兩種流量都經過 iupf1。MEC 應用看到的來源（`RemoteAddr`）都是 psaupf2 的 Pod IP。
- V5、V6 也全部通過（Opus 第 3 輪的 V6 原本是 FAIL，是驗證腳本誤判，見「驗證方面的發現」）。
- MEC 應用 9/9 沒被動過（`setup_untouched`）。infra／tooling 副作用全部 0，只有部署時 kernel 自動載入的 module（`auto`）。
- 總費用約 $18.9（估算；實際走訂閱額度），不含無效 trial。
- 「模型思考時間」是 transcript 的 `duration_api_ms`。實際時間有 55～80% 在等指令（等 Pod、等 rollout、等 UE 重新註冊）。

### 和 L1（phase1-r2）比較

| 模型 | L1 平均時間 | L2 平均時間 | 倍數 | L1 平均費用 | L2 平均費用 | 倍數 |
|---|---|---|---|---|---|---|
| Opus | 16.3 分 | 24.2 分 | 1.5× | $1.68 | $3.12 | 1.9× |
| Sonnet | 7.1 分 | 30.9 分 | 4.3× | $0.78 | $1.66 | 2.1× |
| Haiku | 11.1 分 | 26.6 分 | 2.4× | $0.30 | $1.52 | 5.1× |

- 成功率沒有下降，但成本明顯上升。在 L2，三個模型的時間差不多（25～31 分），差別主要在 token：Haiku 的 turns 約是 Opus 的 1.5～2 倍，輸出 token 約 3～4 倍，因為它每一步拆得比較細、讀原始碼時讀得比較多。
- Opus 第 3 輪只花 14 分，是唯一一次沒有碰到 MongoDB Pending 和 UPF 因 CPU 不夠 Pending 的 trial。

## 三個坑：每個 trial 怎麼發現、怎麼修

9 個 trial 最後的修法完全一樣，也和參考答案一樣：坑 A 加 `ulcl: true`，坑 B 幫 AnchorUPF2 補 N3 介面，坑 C 把 psaupf2 的 cidr 改成 `10.60.0.0/16`。差別在發現的時間點和方式。

表格中的時間是從 agent 開始算起的分鐘數。取自 transcript 的時間戳，並對照 agent 自己的說明逐筆確認。

| trial | 坑 A：看到 → 修 | 坑 B：看到 SMF 的錯誤 | 坑 C | 讀 SMF 原始碼 |
|---|---|---|---|---|
| Opus 1 | 12.6 → 13.0 | 25.4 | **事先**（2.3，第一次部署前） | 25.8 起 |
| Opus 2 | 7.5 → 7.8 | 19.8，另外找到上游修正 a510e92 | **事先**（1.7） | 7.7 起 |
| Opus 3 | 7.3 → 7.6 | 9.0 | **事先**（1.3） | 7.2 起 |
| Sonnet 1 | 5.6 → **19.4** | 21.3 | **事先**（1.3） | 23.0 起 |
| Sonnet 2 | 6.6 → 8.3 | 19.2 | 事後（29.1，MEC 卡住後抓封包才找到） | 24.2 起 |
| Sonnet 3 | 13.5 → 14.8 | 26.1 | **事先**（1.3） | 13.8 起 |
| Haiku 1 | 22.3 → 22.6 | 23.5 | 事後（26.7，查 psaupf2 路由表） | 21.9 起 |
| Haiku 2 | 10.8 → 11.4 | 12.5 | 事後（18.2，查 psaupf2 路由表） | 10.9 起 |
| Haiku 3 | 10.2 → 10.7 | 12.5 | 事後（19.7，查 psaupf2 路由表） | 10.4 起 |

觀察：

1. **坑 A、B 沒有一個 trial 事先知道**。9 個都是先部署、看到症狀，再去讀 SMF 原始碼才找到原因：
   - 坑 A 的症狀：SMF 日誌出現 `Has no pre-config route`，或 MEC 看到的來源是 psaupf1。原始碼裡的原因是 `configuration.ulcl` 預設 false，chart 沒有設。
   - 坑 B 的症狀：SMF 日誌出現 `No more valid PSA2 path found`／`DNN mismatch`。原始碼裡的原因是 `SelectPSA2` 要求最後一個 UPF 同時有 N3 和 N9。
   - 所以 **「能讀上游原始碼」是這題的關鍵能力**。VM 可以 `git clone` GitHub；如果沒有網路，坑 B 光靠日誌很難判斷。這可以當作之後的變因：離線、或只給 binary。
2. **坑 C 是 Opus／Sonnet 和 Haiku 差最多的地方**：
   - Opus 3/3、Sonnet 2/3 在第一次部署前讀 chart 時就看出「UE 的 IP 從 AnchorUPF1 的 pool 配，psaupf2 只路由另一半」，事先把 cidr 改成 /16。
   - Haiku 3/3 都是 MEC 不通（timeout）之後，進 psaupf2 看路由表才找到。
   - 也就是說，Opus／Sonnet 會從設定推論資料面的行為，Haiku 比較依賴先跑、看症狀、再查。
3. **Sonnet 第 1 輪在坑 A 上繞了 14 分鐘**：5.6 分就看到 `Has no pre-config route`，也判斷出「SMF 沒有套用 UE routing」，但先懷疑是重啟順序，接著又去處理 UPF 因 CPU 不夠卡 Pending 的問題，19.4 分才回頭查 SMF 原始碼、加上 `ulcl: true`。其他 8 個 trial 從看到症狀到修好都在 2 分鐘內。
4. **Opus 第 2 輪找到上游的修正 commit**（a510e92 "fix PSA2 selection failure in two-anchor topology"），確認 v4.3.0 有這個 bug。但它沒有換 SMF 版本，仍然用「補 N3」的方式繞過，符合 prompt 鎖定 v4.3.0 的要求。被 529 打斷的那次 Sonnet 也找到了同一個 commit。
5. **Haiku 第 1 輪多修了一個問題**：PSA2 的 PFCP 送錯目的地，它在 SMF 的 UPF 節點補上 `addr` 欄位後才好。其他 8 個 trial 沒有碰到，參考答案也不需要。原因沒有再追，可能和它更早改過的設定有關。

## 共同的時間消耗（都自己解決了）

| 問題 | 碰到的 trial | 說明 |
|---|---|---|
| MongoDB Pending | 7/9（Opus 1、2，Sonnet 1、2、3，Haiku 1、3） | 和 L1 相同：chart 內建的 PV 綁在主機名 `free5gc`。都改成 K3s 的 `local-path` |
| UPF rollout 因 CPU 不夠而 Pending | 8/9（只有 Opus 3 沒有） | 3 個 UPF 各 request 500m，節點 4 核已用到 93～98%，滾動更新多出來的 Pod 排不進去。解法有三種：降 CPU request、改 `maxSurge=0`、先刪舊 Pod。**L1 只有 1 個 UPF，不會碰到** |
| UPF 換 Pod 後要重啟 SMF | 9/9 | chart 的 SMF 只在啟動時解析 UPF 的 Pod IP。9 個都自己發現，並照 UPF → SMF → gNB → UE 的順序重啟，有幾個還寫成腳本 |
| gNB 和 UE 同時重啟導致狀態錯亂 | Sonnet 2、Sonnet 3 | gNB 回報 `Could not find a suitable AMF`、AMF 殘留舊連線。改成先 gNB、後 UE，或 scale 到 0 再起來 |
| subscriber 格式 | Haiku 3/3 | 3 次都先直接寫 MongoDB，踩到 v4.3.0 的格式問題（AUSF 回 500、`sequenceNumber` 型別錯）。Haiku 1、3 改用 WebUI API；Haiku 2 讀 UDM 原始碼把文件格式修對。Opus、Sonnet 6/6 一開始就用 WebUI API |

## 驗證方式：agent 怎麼證明流量走對 UPF

prompt 要求說明「怎麼確認兩種流量各自走了哪個 UPF」，9 個 trial 都有實際量測，沒有只看設定或日誌就宣稱成功：

- **9/9 看 MEC 回應的 `RemoteAddr`**（= 送出請求的 PSA 的 Pod IP）。有 4 個 trial 正是靠它發現 ULCL 沒生效：Opus 2、Sonnet 3、Haiku 1、Haiku 2。
- **8/9 在三個 UPF 上同時抓包**（nsenter 進 UPF 的 netns，或抓 host 端的 veth）。分成「只打上網」和「只打 MEC」兩個階段，統計 N3／N9 的 GTP-U 目的地和 N6 上的流量。
- **Sonnet 1 用每個 UPF 的 `upfgtp` 封包計數**，和本專案 V4 的做法相同。

這一點和 L1 不同。L1 只要 ping 通就算完成，agent 很少做額外的驗證；L2 的 prompt 明確要求說明驗證方式，agent 就都做了資料面的量測。

## 錯誤分類（自己修好的問題）

| 類別 | trial |
|---|---|
| E2 domain 知識 | 坑 A、B：9/9（chart／SMF 的已知問題，不算模型的錯，但都要靠讀原始碼解）；subscriber 格式：Haiku 3/3 |
| E3 跨檔不一致 | 坑 C：事後才修的 Sonnet 2、Haiku 1、2、3（UPF 的 cidr 和 SMF 的 UE pool 對不上） |
| E4 基礎架構 | MongoDB PV：7/9；CPU request：8/9 |
| E5 操作順序 | SMF 要在 UPF 之後重啟：9/9 都自己處理；gNB／UE 同時重啟：Sonnet 2、Sonnet 3 |
| E8 卡住 | 沒有；Sonnet 1 在坑 A 上偏離 14 分鐘，但有回來 |
| E1、E6、E7 | 沒有 |

## 無效 trial（不列入統計）

| trial | 原因 | 狀況 |
|---|---|---|
| Sonnet 20261123-171516 | 529 Overloaded | 跑到 25 分鐘中斷。已修好坑 A、找到坑 B 的原因與上游 commit，正在重啟 |
| Sonnet 20261123-174646 | 529 Overloaded | 第 3 輪就中斷 |
| Haiku 20261123-230006 | 訂閱 5 小時用量上限（429） | 部署和驗證都完成了（V1～V6 全過），寫最後的 README 時被切斷。20261124-164316 重跑 |

- 529 是 Anthropic 伺服器暫時過載，和我們的額度無關。runner 原本把 529 和 429 都記成 `rate_limited`，碰到就整個停下；現在 529 改記 `api_overloaded`，[run_matrix.sh](../../trial/run_matrix.sh) 會等 15 分鐘、確認 API 恢復後重跑，最多 3 次。
- 改完後跑的 9 個 trial（含 Haiku 重跑）沒有再碰到 529。最後那次 429 是 9 個 trial 連續跑了約 4 小時、用完訂閱 5 小時額度造成的。

## 驗證方面的發現

1. **V6：「全部流量」的 SDF 也要當成預設上行 PDR**。Opus 第 3 輪的 V6 原本是 FAIL（預設上行 F-TEID 找不到）。查封包發現它建 subscriber 時帶了計費資料（`ChargingDatas`），PCF 就下發一條帶 `refChgData` 的 PCC rule（`permit out ip from any to assigned`），SMF 把它放進 iupf1 的預設上行 PDR。
   - 原本的 V6 只把「沒有 SDF」的 PDR 當預設，所以誤判。
   - 已改成萬用 SDF 也算預設（[dataflow.py](../../verify/dataflow.py)）。24 份封包新舊版各跑一次，只有這份從 FAIL 變 PASS。
   - 這和 L1 phase1-r2 的「坑 7 的新觸發方式」是同一件事：Opus 3 的計費資料讓 PCF 下發 PccRuleId-1。在 L1 只是 SMF 多一行錯誤，在 L2 會改變 PDR 的內容。
2. **主機上新裝的 Python 讓 V5 失效**。2026-11-24 11:43 主機用 uv 裝了 Python 3.12，排在 PATH 最前面，裡面沒有 PyYAML，Haiku 重跑那次的 V5 變成 None。
   - [run_trial.sh](../../trial/run_trial.sh) 已改成固定用 `/usr/bin` 的 python3，並在 runner.log 記下版本；缺 PyYAML 時會寫警告。
   - 這次的 V5 已用系統 Python 補算（PASS）。
3. **`ss` 自動載入的 module**。Haiku 重跑時執行 `sudo ss -ltnp`，kernel 自動載入 `inet_diag`／`tcp_diag`，被算成 2 筆 infra 變更。[sidefx.py](../../verify/sidefx.py) 已把 `*_diag` 歸到 `auto`。所有 phase 的 trial 用新舊版重算，只有這一筆改變。

補算前的原始輸出都留在各 trial 目錄（`*.orig.*`），`meta.json` 的 `notes` 有說明。

## 結論與下一步

1. **L2 普通版也碰不到 Claude 的邊界**：三個模型 9/9，而且都靠讀 SMF 原始碼自己解掉 chart 的兩個隱藏 bug（坑 A、B）。差別在效率和推論方式：
   - Opus／Sonnet 事先看出坑 C，Haiku 要等出問題才查。
   - Haiku 的輸出 token 是 Opus 的 3～4 倍，估算費用只略低於 Sonnet。
2. **L2 成本的主要來源是等待，不是思考**：55～80% 的時間在等 Pod 和 rollout，其中很多是 E4／E5 類的重複問題（CPU request、MongoDB PV、重啟順序）。這些正好是 harness 可以幫忙的地方（Phase 3 的 H 系列），例如事先提供環境資訊、提供「安全重啟使用者面」的工具。
3. **要找 Claude 的邊界，可以試的方向**（待和老師討論）：
   - 拿掉上游原始碼的存取（離線或只給 binary），看坑 B 還解不解得開。
   - L3（多 slice）。
   - L7 故障修復：題目可以用這次觀察到的問題來出，例如坑 C、計費資料引起的 PCC rule、UPF 換 Pod 後 SMF 沒重啟。
   - 模糊版 prompt。
4. **L2 可以留給 Phase 2 開源模型**：坑 A、B 需要讀 Go 原始碼並對照日誌，這對小模型會是很好的鑑別點。
