# Phase 1：Claude 模型在 L1 的能力邊界

## 實驗條件（固定）

| 項目 | 值 |
|---|---|
| 任務 | L1，普通版描述（[tasks/L1/prompt-normal.md](../../tasks/L1/prompt-normal.md)） |
| free5GC 版本 | **v4.3.0**，prompt 指定 free5gc-helm commit `07139fc`（2026-11-18 pilot 後決定；pilot 沒指定版本，agent 自己拿了 main）。驗證加一項需求：所有 NF image tag 必須是 v4.3.0 |
| 起始環境 | snapshot `S1-agent`：Ubuntu 22.04、K3s v1.37.2、Helm v3.22.1、gtp5g v0.9.16、Claude Code 2.1.325；**沒有**任何 5G 元件、chart、image |
| Image 來源 | 2026-11-18 的 9 個正式 trial：VM 直接從 Docker Hub 拉（Haiku 第 2 次被 429 限流）。**之後的 trial**：經 host 上的 Docker Hub 快取（[env/README.md](../../env/README.md#docker-hub-快取)），對 agent 透明 |
| Harness | Claude Code 2.1.325，headless（`-p`），`--dangerously-skip-permissions`，自動更新關閉 |
| 停用的工具 | WebSearch、WebFetch（VM 仍可上網，用來裝套件、拉 chart 與 image） |
| Effort | `high`（四個模型一致） |
| 上限 | 每個 trial 60 分鐘、$15（`--max-budget-usd`，以 Claude Code 的估計費用計） |
| 認證 | Claude 訂閱（`claude setup-token` 產生的長效 token，放 `env/secrets/oauth.env`）。費用欄位是依 API 價格的**估計值**；token 用量是實際值 |
| 不設定 | fallback model、CLAUDE.md、settings、MCP |
| 人工介入 | 無（headless，prompt 寫明不會有人回答問題） |
| 評分 | [verify/verify.sh](../../verify/verify.sh) 加 [tasks/L1/spec.json](../../tasks/L1/spec.json)：V1 Pod、V2 註冊與 PDU session（nr-cli）、V3 經 uesimtun0 ping 8.8.8.8、需求（1 UPF／1 gNB／1 UE、SUPI、PLMN、slice、DNN）。agent 結束後等 60 秒再驗 |
| 有效性 | 中途換成其他模型、被訂閱用量上限限流、認證失敗 → 該 trial 無效（`invalid_reasons`），重跑，不算 agent 失敗 |

## 模型

| 模型 | ID |
|---|---|
| ~~Claude Fable 5.2~~ | `claude-fable-5-2`：訂閱回 `Fable 5.2 requires usage credits`，需另購用量額度。**2026-11-18 決定先不買，Phase 1 不含 Fable** |
| Claude Opus 5.5 | `claude-opus-5-5` |
| Claude Sonnet 5.5 | `claude-sonnet-5-5` |
| Claude Haiku 5.5 | `claude-haiku-5-5` |

## 進行方式

1. **Pilot**：Opus 5.5 跑 1 次，量實際費用與時間，確認流程。（不計入結果）
2. 使用者看過 pilot 後核准（2026-11-18）：Opus、Sonnet、Haiku × 3 次 = 9 個 trial，模型交錯執行（[trial/run_matrix.sh](../../trial/run_matrix.sh)，紀錄在 `matrix.log`）。

## 每個 trial 留下的東西（`results/phase1/<trial_id>/`）

`prompt.md`、`agent-cmd.sh`（實際執行的指令）、`transcript.jsonl`（完整對話與 tool call，API key 已遮蔽）、`agent-stderr.log`、`work.tgz`（agent 工作目錄）、`cluster-state.txt`、`configmaps.yaml`、`verify.json`、`verify-summary.txt`、`runner.log`、`meta.json`。

彙整：`trials.jsonl`（每個 trial 一行）。錯誤分類（E1–E8）在人工看過 transcript 後填進 [ANALYSIS.md](ANALYSIS.md)。

## 執行指令

```bash
cd ~/agent-oam-free5gc
trial/run_trial.sh --task L1 --model claude-opus-5-5 --effort high --timeout-min 60 --budget-usd 15
# 完整矩陣
trial/run_matrix.sh --rounds 3 -- claude-opus-5-5 claude-sonnet-5-5 claude-haiku-5-5
```

## 怎麼判讀結果

### 檔案

| 檔案 | 用途 |
|---|---|
| [ANALYSIS.md](ANALYSIS.md) | **先看這份**：結果表、每個 trial 遇到的問題、錯誤分類、觀察 |
| `trials.jsonl` | 每個 trial 一行，適合用程式彙整 |
| `<trial_id>/meta.json` | 同一行的內容，單獨一個檔 |
| `<trial_id>/verify-summary.txt` | 驗證結果，人看的版本 |
| `<trial_id>/transcript.jsonl` | 完整過程：agent 的每段文字、每個指令和輸出。要知道「怎麼做到的」看這個 |
| `<trial_id>/work.tgz` | agent 留下的檔案（values、腳本、clone 的原始碼） |
| `matrix.log` | 矩陣的執行順序與每個 trial 的一行結果 |

### `meta.json` 的欄位

| 欄位 | 意義 | 注意 |
|---|---|---|
| `verify.pass` | V1、V2、V3、需求全過（定義見 [verify/README.md](../../verify/README.md)） | 主要指標 |
| `verify.v3x`、`verify.pass_strict` | 經 tunnel 抓網頁、DNS、第一跳是 UPF；嚴格版 PASS | 2026-11-19 才加入，Phase 1 的 trial 都是 `None`（部署已經不在，無法補驗） |
| `valid` | 這次 trial 算不算數 | `false` 是外部原因（限流、認證、換模型），要重跑，**不算 agent 失敗** |
| `agent.wall_s` | agent 從開始到結束的秒數 | 不含還原 VM、驗證；受網路與 image 下載速度影響 |
| `agent.num_turns` | agent 呼叫模型的輪數 | 所有段落加總（背景工作結束時 agent 會被喚醒，多跑一段） |
| `agent.cost_usd` | 費用 | 用訂閱跑，這是 Claude Code 照 API 價格算的**估計值**，不是實際扣款 |
| `agent.usage` | token 用量 | 實際值，所有段落加總；`cache_read` 大代表讀了很多 context |
| `agent.tool_calls` | 每種工具用了幾次 | 幾乎全是 Bash |
| `side_effects` | agent 對 VM／K3s 的變更 | 2026-11-18 的 trial 沒有開始前的基準，是 `None`；之後的 trial 才有 |
| `error_class` | 錯誤分類 | 人工看 transcript 後寫在 ANALYSIS.md，這個欄位目前沒填 |

### 結論能說到哪裡

**可以說**：在這組條件下（單機 K3s、版本固定 v4.3.0、普通版描述、effort high、Claude Code、可上網也能 `git clone`、沒有人介入），Opus、Sonnet、Haiku 5.5 都能把 L1 做到 UE 端到端連通；**L1 不是 Claude 5.5 的能力邊界**。

**不能說**：

- **「成功率 100%」**：每個模型只跑 3 次。3/3 的 95% 信賴區間下限約 44%，9 次合起來（9/9）下限約 70%。只能說「沒有觀察到失敗」。
- **「哪個模型比較好」的統計結論**：時間和費用每個模型只有 3 個值，而且受網路與 Docker Hub 限流影響（Haiku 2）。目前只能當成描述，不能當成顯著差異。
- **「部署是正確而且乾淨的」**：PASS 只代表端到端連通、UE 看到的參數正確。多出來的 slice、沒清掉的資源、對主機的變更（例如 Haiku 3 改了 K3s 設定）都不影響 PASS。
- **「模型本身懂 free5GC」**：agent 會 `git clone` 原始碼來查，所以測到的是「模型＋查原始碼」的能力。
- **過程分析是初步的**：ANALYSIS.md 裡「遇到什麼、怎麼修」是一個人看 transcript 的判讀，沒有盲測，也沒有第二人複核。
- **pilot 不併入**：pilot 的 prompt 沒有指定版本，條件不同。
