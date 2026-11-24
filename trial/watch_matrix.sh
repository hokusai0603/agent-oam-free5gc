#!/usr/bin/env bash
# 監控 run_matrix.sh：每分鐘更新 results/<phase>/STATUS.md，矩陣結束或停下時寫最終狀態並發桌面通知。
# 用 setsid nohup 啟動，和 Claude Code session 脫鉤（session 斷掉也會繼續）：
#   setsid nohup trial/watch_matrix.sh phase1-l2 > results/phase1-l2/watch.log 2>&1 < /dev/null &
# 新 session 只要讀 results/<phase>/STATUS.md 就知道進度。
set -uo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
PHASE=${1:?usage: $0 phase}
dir="$root/results/$PHASE"
mlog="$dir/matrix.log" status="$dir/STATUS.md"
# 只看最後一次 matrix start 之後的紀錄
since=$(grep -n 'matrix start' "$mlog" | tail -1 | cut -d: -f1)

write_status() {   # $1 = 狀態字串
  python3 -I - "$dir" "$1" "$since" <<'PY' > "$status.tmp" && mv "$status.tmp" "$status"
import json, os, sys, datetime
d, state, since = sys.argv[1], sys.argv[2], int(sys.argv[3])
rows = []
try:
    for l in open(os.path.join(d, "trials.jsonl")):
        if l.strip(): rows.append(json.loads(l))
except FileNotFoundError:
    pass
log = open(os.path.join(d, "matrix.log")).read().splitlines()
cur = log[since - 1:]
now = datetime.datetime.now().strftime("%F %T")
print(f"# 矩陣狀態（{os.path.basename(d)}）\n\n- 狀態：**{state}**\n- 更新時間：{now}\n")
valid = [r for r in rows if r.get("valid")]
print(f"- 有效 trial：{len(valid)}，通過 {sum(1 for r in valid if r['verify'].get('pass'))}；"
      f"無效（529／限流等）：{len(rows) - len(valid)}\n")
print("| trial | 模型 | 有效 | PASS | V4 | V5 | V6 | 時間（分） | 費用（估） |")
print("|---|---|---|---|---|---|---|---|---|")
for r in rows:
    v, a = r.get("verify", {}), r.get("agent", {})
    v5 = (v.get("v5") or {}).get("pass"); v6 = (v.get("v6") or {}).get("pass")
    ok = "是" if r.get("valid") else "否（" + ",".join(r.get("invalid_reasons", [])) + "）"
    print(f"| {r['trial_id'][-15:]} | {r['model'].replace('claude-', '')} | {ok} | {v.get('pass')} | {v.get('v4')} "
          f"| {v5} | {v6} | {int(a.get('wall_s') or 0) / 60:.1f} | ${a.get('cost_usd') or 0:.2f} |")
print("\n## matrix.log（這次啟動之後）\n\n```")
print("\n".join(l[:260] for l in cur[-15:]))
print("```")
PY
}

notify() {
  notify-send "L2 矩陣 $PHASE" "$1" 2>/dev/null || true
  echo "[$(date '+%F %T')] $1"
}

echo "[$(date '+%F %T')] watch start phase=$PHASE since_line=$since"
while :; do
  tail_part=$(sed -n "${since},\$p" "$mlog")
  if printf '%s' "$tail_part" | grep -q 'matrix done'; then
    write_status "完成"; notify "矩陣跑完了，見 $status"; exit 0
  fi
  if printf '%s' "$tail_part" | grep -q 'matrix stopped'; then
    why=$(printf '%s' "$tail_part" | grep 'matrix stopped' | tail -1)
    write_status "停止：$why"; notify "矩陣停下來了：$why"; exit 1
  fi
  if ! pgrep -f "run_matrix.sh .*--phase $PHASE" >/dev/null; then
    write_status "異常：run_matrix.sh 已經不在跑，但 log 沒有 done/stopped（可能被中斷或主機重開）"
    notify "run_matrix.sh 不見了（異常結束）"; exit 2
  fi
  write_status "執行中（最後一行：$(tail -1 "$mlog" | cut -c1-120)）"
  sleep 60
done
