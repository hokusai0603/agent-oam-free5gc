#!/usr/bin/env bash
# 實驗環境管理：snapshot 建立／還原、狀態檢查、進 VM。
#
#   scripts/env.sh status                  VM 狀態與 snapshot 清單
#   scripts/env.sh save <name>             關機狀態下存 snapshot（保證一致）
#   scripts/env.sh reset [name]            還原 snapshot 並開機（預設 S1-k3s-ready）
#   scripts/env.sh check                   檢查 VM 內 K3s / gtp5g / sctp 是否正常
#   scripts/env.sh wait-l1                 等 L1 網路可用（Pod Ready + UE ping 通）
#   scripts/env.sh ssh [cmd...]            進 VM 或在 VM 內執行指令
#
# Snapshot 約定：
#   S1-k3s-ready  乾淨 Ubuntu 22.04 + K3s + Helm + gtp5g，尚未部署任何 5G 元件（L1/L2 bring-up 的起點）
#   S2-l1-ref     L1 參考答案已部署且驗證通過（L6/L7 的起點）；還原後約 3.5 分鐘恢復可用
set -euo pipefail
SELF=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")   # 用相對路徑呼叫時，cd 之後還找得到自己
cd "$(dirname "$0")/.."

NODE=${OAM_NODE:-oam-node1}
DEFAULT_SNAP=S1-k3s-ready

wait_ssh() {
  for _ in $(seq 1 60); do
    vagrant ssh "$NODE" -c true >/dev/null 2>&1 && return 0
    sleep 5
  done
  echo "VM ssh not ready" >&2; return 1
}

case "${1:-}" in
  status)
    vagrant status "$NODE"
    vagrant snapshot list "$NODE" || true
    ;;
  save)
    name=${2:?snapshot name}
    vagrant halt "$NODE"
    vagrant snapshot save "$NODE" "$name"
    vagrant up "$NODE" --no-provision
    ;;
  reset)
    name=${2:-$DEFAULT_SNAP}
    start=$(date +%s)
    vagrant snapshot restore "$NODE" "$name" --no-provision
    wait_ssh
    "$SELF" check
    # S2 是關機狀態存的，還原 = 冷開機，free5GC 要約 3 分鐘才會自己恢復。等它恢復才算 reset 完成。
    case "$name" in S2-*) "$SELF" wait-l1 ;; esac
    echo "reset to $name in $(( $(date +%s) - start ))s"
    ;;
  wait-l1)
    # 等 namespace free5gc 的 Pod 全部 Ready 且 UE 能經 uesimtun0 ping 到 8.8.8.8（最多 8 分鐘）
    for _ in $(seq 1 48); do
      r=$(vagrant ssh "$NODE" -c '
        NS=free5gc
        nr=$(kubectl -n $NS get pods --no-headers 2>/dev/null | awk "{split(\$2,a,\"/\"); if (!(\$3==\"Running\" && a[1]==a[2])) n++} END{print n+0}")
        UE=$(kubectl -n $NS get pod -o name 2>/dev/null | grep ueransim-ue | head -1)
        p=$(kubectl -n $NS exec $UE -- ping -I uesimtun0 -c 2 -W 2 8.8.8.8 2>/dev/null | grep -o "[0-9]*% packet loss")
        echo "notready=$nr ping=${p:-none}"' 2>/dev/null | tr -d '\r')
      case "$r" in *"notready=0 ping=0% packet loss"*) echo "L1: ok"; exit 0 ;; esac
      sleep 10
    done
    echo "L1 did not recover: $r" >&2; exit 1
    ;;
  check)
    vagrant ssh "$NODE" -c '
      set -e
      lsmod | grep -q "^gtp5g" && echo "gtp5g: ok"
      lsmod | grep -q "^sctp"  && echo "sctp: ok"
      for i in $(seq 1 60); do kubectl get nodes 2>/dev/null | grep -q " Ready" && break; sleep 3; done
      kubectl get nodes -o wide
      kubectl -n kube-system wait --for=condition=Available deployment --all --timeout=180s >/dev/null && echo "kube-system: ok"
      echo "non-system namespaces: $(kubectl get ns -o name | grep -vE "/(default|kube-system|kube-public|kube-node-lease)$" | wc -l)"
    '
    ;;
  ssh)
    shift
    if [ $# -eq 0 ]; then vagrant ssh "$NODE"; else vagrant ssh "$NODE" -c "$*"; fi
    ;;
  *)
    sed -n '2,14p' "$SELF"; exit 1
    ;;
esac
