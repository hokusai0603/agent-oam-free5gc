# L1 參考答案執行紀錄

每次都是 `OAM_RESET=1 tasks/L1/reference/deploy.sh`，從 `S1-k3s-ready` 開始（image 都要重新下載）。

| 時間 | 結果 | 耗時 | 備註 |
|---|---|---|---|
| 2026-11-18 14:51 | ✗ | — | deploy.sh bug：WebUI 對不存在的 subscriber 也回 200，誤判「已存在」沒建，UE 註冊失敗 |
| 2026-11-18 14:57 | ✗ | 224 s | UE 拿到 10.60.0.1 但 ping 100% 掉包；腳本因 `| tail` 吃掉錯誤誤報成功。原因：坑 7（UPF 的 dnaiList）＋坑 8（rollout restart 雙 UE） |
| 2026-11-18 15:04 | ✓ | 256 s | 修正後通過：Pod 全部 Ready 169 s、ping 8.8.8.8 4/4、RTT 平均 11.3 ms；1 個 UPF；SMF 無 data path 錯誤；所有 NF image v4.2.2 |

## Snapshot `S2-l1-ref`

- 2026-11-18 15:08 由上面第 3 次執行的狀態存成（關機後存）。
- 還原測試：`reset S2-l1-ref` 40 s 開機 ＋ 約 170 s 等 free5GC 自行恢復，**共約 3.5 分鐘**後 UE ping 通。`env.sh reset S2-*` 會自動等到恢復為止。
