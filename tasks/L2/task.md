# L2：ULCL 把 MEC 流量導到另一個 PSA-UPF

> 這份是任務說明。實際送給 agent 的全文是 [prompt-normal.md](prompt-normal.md)（普通版）。參考答案在 [reference/](reference/)，agent 看不到。

## 設計

- **起點和 L1 相同**：`S1-agent`，從零開始。agent 開始前，runner 先套用 [setup/mec-app.yaml](setup/mec-app.yaml)，部署 MEC 應用，固定 IP `10.43.200.10`。
- MEC 應用由環境準備、agent 不用部署，這樣量到的是 5G 使用者面的設定能力，驗證也有固定的目標。
- 規格刻意和 chart 預設不同（導流目的地不是 `1.0.0.1`），agent 必須理解並改寫 SMF 的 ULCL 設定，不能照抄預設值。
- 參數（PLMN、slice、DNN、SUPI、版本）和 L1 相同，L2 和 L1 的差別只在使用者面。

## 驗證（agent 看不到）

| 層級 | 條件 | 狀態 |
|---|---|---|
| V1–V3、V3x | 同 L1 | 現有，可直接用 |
| 需求 | `upf_count` = 3；PLMN／slice／DNN／SUPI／版本與描述一致 | 現有 |
| V4 MEC 路徑 | 綁 uesimtun0 連 `http://10.43.200.10/` 成功；MEC 和上網的流量由不同的 UPF 解封裝（`upfgtp` 計數），而且都經過只轉送的 I-UPF；`RemoteAddr` 當旁證 | **完成，算進 PASS**（2026-11-23） |
| V5 | 多 UPF 版：逐一比對每個 UPF 的 pool／cidr、`ulcl: true`、specificPath 的 dest、members、path、PSA 的回程路由、v4.3.0 的 PSA2 N3 | 完成，只記錄（2026-11-23） |
| V6 | 多 UPF 版：分開看每個 UPF 的 PFCP、N9 TEID、I-UPF 的 ULCL PDR／FAR、N9 上 MEC／上網流量的走向與回程 | 完成，只記錄（2026-11-23） |

細節與三個 chart／SMF 的坑見 [reference/README.md](reference/README.md)。參考答案與 A／B／C 反向版本的實測結果見 [verify/README.md](../../verify/README.md#已測過的情況)，原始輸出在 [reference/evidence/verify-2026-11-23/](reference/evidence/verify-2026-11-23/)。

## 待補

- （完成）`trial/run_trial.sh` 支援 spec 的 `setup`：還原環境後、拍主機狀態基準前套用；前後比對物件 spec（`setup_untouched`）
- 詳細版、模糊版 prompt
