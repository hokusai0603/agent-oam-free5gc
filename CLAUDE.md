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
- [ ] Phase 1：Opus / Sonnet / Haiku 跑 L1、L2（第一個里程碑，約 3 天，完成後回報老師）
- [ ] 確認 Ollama 能不能讓 Claude Code 跑開源模型、tool calling 穩不穩；不行就自己寫 agent loop
- [ ] 文獻：找有沒有開源模型微調的 Kubernetes 設定專用模型，當作對照組
- [ ] 和 Alex 討論 planner / sub-agent 架構
- [ ] 等老師轉寄相關資料

> 每完成一項就更新這個清單，換電腦或開新 session 時才接得上。

## 相關人員
- 習文：continuous learning（之後可結合 MTLF 提早觸發訓練）
- 慶哲：hierarchical FL 監控
- Alex：sub-agent 架構
