#!/usr/bin/env bash
# 依序跑完整矩陣：模型交錯（opus→sonnet→haiku→opus…），避免同一模型集中在同一時段。
#   trial/run_matrix.sh [--rounds 3] [--phase phase1] [--task L1] [--skip N] -- model1 model2 ...
#   --skip N：跳過照順序的前 N 個 trial（中斷後接著跑；被判 invalid 的那個不要算進去）
# 某個 trial 被限流（429）或認證失敗就停下，不繼續消耗。
# 529 Overloaded（伺服器暫時過載）：等 OVERLOAD_WAIT_MIN 分（預設 15）、trial/api_check.sh 確認恢復後重跑同一格，
# 最多 OVERLOAD_RETRIES 次（預設 3）。被 529 打斷的 trial 留著紀錄（valid=false），不算進統計。
set -uo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
ROUNDS=3 PHASE=phase1 TASK=L1 SKIP=0
OVERLOAD_RETRIES=${OVERLOAD_RETRIES:-3} OVERLOAD_WAIT_MIN=${OVERLOAD_WAIT_MIN:-15}
while [ $# -gt 0 ]; do
  case "$1" in
    --rounds) ROUNDS=$2; shift 2 ;;
    --phase) PHASE=$2; shift 2 ;;
    --task) TASK=$2; shift 2 ;;
    --skip) SKIP=$2; shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
models=("$@")
[ ${#models[@]} -gt 0 ] || { echo "usage: $0 [--rounds N] -- model..." >&2; exit 1; }
mkdir -p "$root/results/$PHASE"
mlog="$root/results/$PHASE/matrix.log"
echo "[$(date '+%F %T')] matrix start task=$TASK rounds=$ROUNDS skip=$SKIP models=${models[*]}" >> "$mlog"
n=0
for r in $(seq 1 "$ROUNDS"); do
  for m in "${models[@]}"; do
    n=$((n+1)); [ $n -le "$SKIP" ] && continue
    tries=0
    while :; do
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
      # 529 Overloaded（伺服器暫時過載）：等一陣子、確認 API 恢復後重跑同一格，最多 OVERLOAD_RETRIES 次
      if printf '%s' "$line" | grep -q 'api_overloaded'; then
        tries=$((tries+1))
        if [ $tries -gt "$OVERLOAD_RETRIES" ]; then
          echo "matrix stopped: api_overloaded $tries times in a row" >> "$mlog"; exit 3
        fi
        while :; do
          echo "[$(date '+%F %T')] api_overloaded: retry $tries/$OVERLOAD_RETRIES after ${OVERLOAD_WAIT_MIN} min" >> "$mlog"
          sleep $((OVERLOAD_WAIT_MIN*60))
          chk=$("$root/trial/api_check.sh" "$m" 2>&1 | tail -1)
          echo "[$(date '+%F %T')] $chk" >> "$mlog"
          case "$chk" in API_OK*) break ;; esac
          tries=$((tries+1))
          if [ $tries -gt "$OVERLOAD_RETRIES" ]; then
            echo "matrix stopped: API still failing after $OVERLOAD_RETRIES waits" >> "$mlog"; exit 3
          fi
        done
        continue
      fi
      break
    done
  done
done
echo "[$(date '+%F %T')] matrix done" >> "$mlog"
