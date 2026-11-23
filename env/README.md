# 實驗環境（Phase 0）

Agent 受測用的單機 K3s VM。每個 trial 都從同一個 snapshot 開始，跑完就還原。

## 為什麼不用 host

- host 是多人共用的機器：k3s 的 kubeconfig 只有 root 能讀、sudo 要密碼、沒有載入 gtp5g。
- agent 會改 iptables、路由和 CNI，改錯會影響別人；L7 還會故意弄壞環境。
- host 沒辦法重置，殘留狀態（image 快取、模組、規則）會讓 trial 之間不能比較。

## 規格

| 項目 | 值 |
|---|---|
| VM | `agent-oam-oam-node1`，ubuntu/jammy64（22.04），4 vCPU、10 GB RAM |
| 網路 | NAT（上網）＋ host-only `192.168.57.11`；**不 bridge 到 eno1** |
| 共享資料夾 | 關閉（agent 看不到 host 上的參考答案、chart、驗證腳本） |
| 自動更新 | 關閉 unattended-upgrades、kernel 套件 hold |
| 版本 | 見 [versions.env](versions.env)；實際安裝結果見 [recorded-versions.txt](recorded-versions.txt) |

VM 內容：K3s（含 flannel、CoreDNS、local-path provisioner、metrics-server；**拿掉 traefik**）、Helm 3、gtp5g、sctp 模組、`ip_forward=1`。kubeconfig 在 `vagrant` 使用者的 `~/.kube/config`，不需要 sudo 就能用 kubectl；需要 root 的操作 `vagrant` 有免密碼 sudo。

**沒有**預先放進 VM 的東西：free5GC、UERANSIM、Helm chart、Multus、任何 5G 相關 image。這些是 agent 要自己處理的。

## Snapshot

| 名稱 | 內容 | 用途 |
|---|---|---|
| `S1-k3s-ready` | 上述乾淨環境 | L1–L5 bring-up 的起點 |
| `S1-agent` | S1 ＋ Claude Code 2.1.325 ＋ Docker Hub 快取設定（2026-11-18 重存） | Phase 1 trial 的起點（L1–L5） |
| `S2-agent` | S2 ＋ Claude Code 2.1.325 ＋ Docker Hub 快取設定（2026-11-18 重存） | L6／L7 trial 的起點 |
| `S2-l1-ref` | L1 參考答案（[tasks/L1/reference](../tasks/L1/reference/)）已部署並通過驗證 | L6 變更、L7 故障修復的起點；冷開機，還原後約 3.5 分鐘恢復可用（`reset` 會自動等） |

## 使用

```bash
cd ~/agent-oam-free5gc/env
scripts/env.sh status          # 狀態與 snapshot 清單
scripts/env.sh reset           # 還原到 S1-k3s-ready 並檢查
scripts/env.sh check           # 檢查 gtp5g / sctp / K3s
scripts/env.sh reset S2-l1-ref # 還原到 L1 已部署的狀態，等到 UE ping 通
scripts/env.sh ssh             # 進 VM
scripts/env.sh save <name>     # 關機後存 snapshot
```

從零重建：`vagrant destroy -f && vagrant up`，再 `scripts/env.sh save S1-k3s-ready`。

## Docker Hub 快取

每個 trial 都從沒有 image 的 snapshot 開始，所以每次都要拉約 19 個 image。2026-11-18 的 Phase 1 有 trial 撞到 Docker Hub 的匿名限流（429），因此在 host 架了 pull-through cache：

- `scripts/registry-cache.sh start|stop|status|catalog`：host 上的 `registry:2.8.3` container（`oam-registry-cache`），**只綁在 host-only 網卡 `192.168.57.1:5050`**，實驗室其他機器連不到。資料在 `env/registry-cache/`（git 不追蹤）。
- VM 端是 `/etc/rancher/k3s/registries.yaml`，把 `docker.io` 指向這個快取。`S1-agent`、`S2-agent` 已經包含這個設定；從零重建時由 `provision/20-k3s.sh` 寫入（`OAM_REGISTRY_MIRROR=` 設成空字串就不用）。`S1-k3s-ready`、`S2-l1-ref` 底下有子 snapshot，沒有重存，**沒有**這個設定。
- 對 agent 是透明的：VM 裡仍然沒有任何 5G image，image 名稱照樣寫 `docker.io/...`。快取掛掉時 containerd 會自動退回直接連 Docker Hub（已測試）。
- 實測（2026-11-18）：14 個 NF image 從快取拉 18 秒。快取已預先放了 Phase 1 用到的 19 個 image（free5GC v4.3.0 的 14 個 NF、UERANSIM v4.0.1、MongoDB、busybox、curl、dbpython），共約 720 MB。
- 注意：host 重開機時，如果 Docker 比 VirtualBox 的 `vboxnet1` 先起來，container 會因為綁不到 IP 而失敗。跑 trial 前先 `scripts/registry-cache.sh status` 確認。

## Smoke test

2026-11-17 已確認此環境能跑 free5gc-helm v4.2.2 並做到 UE 端到端 ping 通；過程中踩到的坑見 [smoke-test-2026-11-17.md](smoke-test-2026-11-17.md)。存 S1 → 還原約 40 秒。

## 已知限制／待辦

- 目前只有單節點；L5 多機時用 `OAM_NODES=2 vagrant up` 再補 K3s agent join。
- ~~每次還原後 image 都要重新從 Docker Hub 拉~~ → 已加 Docker Hub 快取（見上方）。
- Multus 沒裝。free5gc-helm v4.2.2 預設不需要 Multus；之後任務需要時再決定是由環境提供還是交給 agent。
- `vendor/` 是 host 端參考用的 free5gc-helm 原始碼（git 不追蹤），**不會**進到 VM。
