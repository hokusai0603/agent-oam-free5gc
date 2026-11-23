#!/usr/bin/env bash
# 依序跑完整矩陣：模型交錯（opus→sonnet→haiku→opus…），避免同一模型集中在同一時段。
#   trial/run_matrix.sh [--rounds 3] [--phase phase1] [--task L1] -- model1 model2 ...
# 某個 trial 被限流或認證失敗（invalid）就停下，不繼續消耗。
set -uo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
ROUNDS=3 PHASE=phase1 TASK=L1
while [ $# -gt 0 ]; do
  case "$1" in
    --rounds) ROUNDS=$2; shift 2 ;;
    --phase) PHASE=$2; shift 2 ;;
    --task) TASK=$2; shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
models=("$@")
[ ${#models[@]} -gt 0 ] || { echo "usage: $0 [--rounds N] -- model..." >&2; exit 1; }
mkdir -p "$root/results/$PHASE"
mlog="$root/results/$PHASE/matrix.log"
echo "[$(date '+%F %T')] matrix start task=$TASK rounds=$ROUNDS models=${models[*]}" >> "$mlog"
for r in $(seq 1 "$ROUNDS"); do
  for m in "${models[@]}"; do
    echo "[$(date '+%F %T')] round $r model $m" >> "$mlog"
    out=$("$root/trial/run_trial.sh" --task "$TASK" --model "$m" --effort high --timeout-min 60 --budget-usd 15 --phase "$PHASE" 2>&1)
    rc=$?
    line=$(printf '%s\n' "$out" | grep '^RESULT' | tail -1)
    echo "[$(date '+%F %T')] rc=$rc ${line:-NO RESULT LINE}" >> "$mlog"
    if [ $rc -ne 0 ] || [ -z "$line" ]; then
      printf '%s\n' "$out" | tail -20 >> "$mlog"; echo "matrix stopped: runner error" >> "$mlog"; exit 1
    fi
    if printf '%s' "$line" | grep -qE 'rate_limited|auth_error'; then
      echo "matrix stopped: invalid trial (rate limit / auth)" >> "$mlog"; exit 2
    fi
  done
done
echo "[$(date '+%F %T')] matrix done" >> "$mlog"
