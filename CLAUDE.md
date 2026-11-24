# 專案：Agent-based OAM 自動部署與管理 free5GC

## 背景
- 2026-11-16 和指導老師開會後決定：**暫停 A-DCCF / SEAL 層級的研究**，改做「以 AI Agent ＋ harness 在 OAM（management plane）層自動部署與管理 free5GC 核網」。
- 會議內容在 [逐字稿.md](逐字稿.md)（整理版）和 [摘要.md](摘要.md)。[原始逐字稿_自動辨識.txt](原始逐字稿_自動辨識.txt) 是 Whisper 的原始輸出，只在需要核對時再看。
- 完整實驗設計在 [實驗規劃.md](實驗規劃.md)，**動手前先讀這份**。

## 研究問題
1. 最強的模型（Claude Code ＋ Opus 5.5）能用自然語言描述 bring up、修改、修復 free5GC 到什麼程度？能力邊界在哪？
2. 換成開源模型（Qwen3 大模型 → 8B / 4B、Gemma 4）時，能力流失在哪裡？
3. 用 harness、驗證、多模型交叉驗證、微調，能不能讓**最小的模型**也做到？這是主要的研究貢獻。

## 實驗架構（細節見 實驗規劃.md）
- **Phase 0**：可重置的單機 K3s 環境、手動部署的參考答案、自動驗證腳本（V1 Pod → V2 NF/PDU Session → V3 UE 端到端 → V4 需求符合度 → V5 跨檔設定一致性）
- **任務階梯**：L1 單機基本核網 → L2 加 MEC → L3 多 slice → L4 latency/bandwidth → L5 多機與擺放限制 → L6 變更管理 → L7 故障修復
- **Phase 1**：在 Claude Code 中用 Opus / Sonnet / Haiku 5.5 測能力邊界
- **Phase 2**：同一套 harness 跑開源模型
- **Phase 3**：依錯誤分類逐步加入 harness H1–H7，並做 ablation

## 工作原則（老師的要求）
- **Baby step、quick turnaround**：每完成一小塊就回報老師，不要過度規劃。
- **每一步都做 regression testing**：驗證腳本會累積成 regression suite，每次改 harness 或換模型都要全部重跑。
- 驗證要做到 UE 端到端連線，只看 Pod Running 不算成功。
- 每個 trial 都要記錄：模型 ID、harness 版本、任務、prompt、log、驗證結果、時間、token、人工介入次數、錯誤分類。
- 實驗開始時固定模型 ID 與軟體版本（free5GC、UERANSIM、K3s、Helm chart、gtp5g），並記錄下來。
- Agent 只在測試機或 VM 上執行指令。

## 目前進度
- [x] 會議逐字稿整理、摘要
- [x] 實驗規劃初版（實驗規劃.md）
- [ ] Phase 0：建立可重置環境、L1/L2 手動參考答案、驗證腳本 V1–V3
  - [x] 可重置環境：VM `oam-node1` ＋ snapshot `S1-k3s-ready`（2026-11-17，見 [env/README.md](env/README.md)；smoke test 端到端通過）
  - [x] L1 參考答案（單一 UPF）→ snapshot `S2-l1-ref`（2026-11-18，見 [tasks/L1/reference](tasks/L1/reference/)；從 S1 部署 256 s，UE ping 通）
  - [x] 驗證腳本 V1–V3（[verify/](verify/)，正反測試都通過）、trial runner（[trial/run_trial.sh](trial/run_trial.sh)，dry run 通過）（2026-11-18）
  - [x] VM 內安裝 Claude Code 2.1.325 → snapshot `S1-agent`、`S2-agent`
  - [x] 使用者在 VM 內 `claude setup-token`，token 放到 `env/secrets/oauth.env`（用訂閱，2026-11-18 決定）
- [x] Phase 1：Opus / Sonnet / Haiku 5.5 跑 L1 各 3 次（條件見 [results/phase1/README.md](results/phase1/README.md)）
  - [x] Opus pilot 1 次：PASS，13.6 分、估計 $1.82（2026-11-18，[ANALYSIS.md](results/phase1/ANALYSIS.md)）
  - [x] 決定：prompt 固定 free5GC v4.3.0；Fable 5.2 需另購額度，先不跑
  - [x] 正式矩陣 9 個 trial：**9/9 通過**；平均 Opus 7.4 分／$1.91、Sonnet 10.2 分／$0.62、Haiku 15.8 分／$0.48（估計）。分析與錯誤分類在 [ANALYSIS.md](results/phase1/ANALYSIS.md)（2026-11-18）
  - [x] Docker Hub pull-through cache：host `192.168.57.1:5050`，`S1-agent`／`S2-agent` 已重存（2026-11-18，見 [env/README.md](env/README.md#docker-hub-快取)）
  - [x] 驗證加「VM／K3s 層變更清單」：`verify/hoststate.sh`＋`sidefx.py`，runner 開始前／結束後各拍一次，只記錄不判分（2026-11-19，見 [verify/README.md](verify/README.md)）
  - [x] V3x（老師建議）：經 uesimtun0 抓網頁（HTTP／HTTPS／1 MB）、DNS、第一跳必須是 UPF；外網問題用對照組排除；記為 `pass_strict`，原本的 `pass` 不變（2026-11-19，見 [verify/README.md](verify/README.md)）
  - [x] 重跑（phase1-r2，含 V3x／副作用）：**9/9 通過、嚴格版 9/9**；infra／tooling 副作用全 0（部署時自動載入的 netfilter module 另列 `auto`）；Opus 2／3 因盲等 MongoDB Pending 拉長到 18～23 分（2026-11-19，[ANALYSIS.md](results/phase1-r2/ANALYSIS.md)）
  - [x] V5 跨 NF 設定一致性（`verify/consistency.py`，靜態看 configmaps＋subscriber）：18 個 trial 補算，全部 pass、只有 2 個 exact（其餘留著 chart 預設的 slice／PLMN；AMF 多宣告 112233 的 5 個有 4 個是 Haiku）；反向測試 22/22（2026-11-20）
  - [x] V6 NF 間 dataflow 與封包內容（`verify/dataflow.sh`）：UE 去註冊→自動重新註冊，cni0 抓封包，tshark 解 NGAP／NAS／SBI／PFCP／GTP-U，檢查 28 個流程步驟、內容與 TEID 一致；正常 3/3 PASS，故障 3 種都抓到；已接進 `run_trial.sh`，V5／V6 先只記錄不併進 PASS（2026-11-20，見 [verify/README.md](verify/README.md)）
  - 結論：L1 碰不到 Claude 的邊界 → Claude 要往 L2／L3／L7 測；L1 留給 Phase 2 開源模型
- [ ] L2（ULCL＋MEC，**從零開始**，起點同 L1；2026-11-20 決定 L2／L3 都從零開始，「在已部署的 L1 上加 MEC」歸 L6）
  - [x] 參考答案（free5gc-helm `07139fc`／v4.3.0，[tasks/L2/reference](tasks/L2/reference/)）：從 S1 部署 270 s PASS；MEC 看到的來源 = PSA-UPF2。chart 預設的 ULCL 其實不會導流，要修 3 個坑：template 沒 `ulcl: true`、v4.3.0 SMF 的 PSA2 選路 bug（上游 a510e92 才修）、psaupf2 的 cidr 不含 UE pool（2026-11-20）。可信度補強（2026-11-23）：從零共 3/3 PASS、逐跳 GTP-U 封包證據、`SKIP_FIX=A|B|C` 反向版本 3/3 照預測失敗、三個坑都和上游官方範例對得上（見 [RUNS.md](tasks/L2/reference/RUNS.md)）
  - [x] prompt 普通版、spec（[tasks/L2](tasks/L2/)）；MEC 應用是環境的一部分（[setup/mec-app.yaml](tasks/L2/setup/mec-app.yaml)，`traefik/whoami`，固定 IP 10.43.200.10）
  - [x] 驗證擴充（2026-11-23，見 [verify/README.md](verify/README.md)）：V4 MEC 路徑（看每個 UPF 的 `upfgtp` 計數判斷 anchor，不靠 NAT；**算進 L2 的 PASS**）；V5／V6 改成多 UPF（誤判 1／5 → 0）。實測：參考答案全 PASS；A／B／C 反向版本 V4 都 FAIL，V5、V6 也都點出對應的坑；L1 迴歸（V5 18 個 trial、V6 5 份封包、反向測試 22/22）結果不變；L2 V5 反向測試 12/12
  - [x] runner 支援 spec 的 `setup`（還原後、拍基準前套用；前後比對 `setup_untouched`），dry run 通過
  - [x] 跑 Opus／Sonnet／Haiku 各 3 次（[results/phase1-l2](results/phase1-l2/)，2026-11-23～24）：**有效 9/9 通過**（含 V4 MEC 路徑），V5／V6 也全過；平均 Opus 24.2 分／$3.12、Sonnet 30.9 分／$1.66、Haiku 26.6 分／$1.52（估計）。另有 3 個無效 trial（Sonnet 2 次 529 Overloaded、Haiku 1 次撞到訂閱 5 小時上限，已重跑）。過程中修了：runner 對 529 自動重試、常駐監控 `trial/watch_matrix.sh`、V6 把萬用 SDF 當預設 PDR（Opus r3 誤判）、runner 固定系統 Python（主機新裝的 uv Python 沒有 PyYAML）、`ss` 載入的 *_diag module 歸 auto
  - [x] L2 [ANALYSIS.md](results/phase1-l2/ANALYSIS.md)（2026-11-24）：坑 A、B 9/9 都是看到症狀後讀 SMF 原始碼解掉；坑 C Opus 3/3、Sonnet 2/3 事先改好，Haiku 3/3 出問題後才查；時間 55～80% 在等（UPF rollout 因 CPU 不夠 Pending 8/9、MongoDB PV 7/9）。結論：L2 普通版也碰不到 Claude 的邊界，下一步方向（離線／L3／L7／模糊版）待和老師討論
- [ ] 確認 Ollama 能不能讓 Claude Code 跑開源模型、tool calling 穩不穩；不行就自己寫 agent loop
- [ ] 文獻：找有沒有開源模型微調的 Kubernetes 設定專用模型，當作對照組
- [ ] 和 Alex 討論 planner / sub-agent 架構
- [ ] 等老師轉寄相關資料

> 每完成一項就更新這個清單，換電腦或開新 session 時才接得上。

## 相關人員
- 習文：continuous learning（之後可結合 MTLF 提早觸發訓練）
- 慶哲：hierarchical FL 監控
- Alex：sub-agent 架構
