#!/usr/bin/env bash
# 跑一個 trial：還原 snapshot → VM 內用 Claude Code（headless）執行任務 → 收集紀錄 → verify → 寫進 results。
#
#   trial/run_trial.sh --task L1 --model claude-opus-5-5 [--effort high] [--timeout-min 60] [--budget-usd 15]
#                      [--phase phase1] [--prompt normal]
#
# 認證（放在 env/secrets/，git 不追蹤；兩個都在時用訂閱）：
#   env/secrets/oauth.env      CLAUDE_CODE_OAUTH_TOKEN=...   Claude 訂閱，在 VM 內 `claude setup-token` 產生
#   env/secrets/anthropic.env  ANTHROPIC_API_KEY=sk-ant-...  API key（按 token 計費）
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
envdir="$root/env"

TASK="" MODEL="" EFFORT=high TIMEOUT_MIN=60 BUDGET=15 PHASE=phase1 PROMPT=normal
while [ $# -gt 0 ]; do
  case "$1" in
    --task) TASK=$2; shift 2 ;;
    --model) MODEL=$2; shift 2 ;;
    --effort) EFFORT=$2; shift 2 ;;
    --timeout-min) TIMEOUT_MIN=$2; shift 2 ;;
    --budget-usd) BUDGET=$2; shift 2 ;;
    --phase) PHASE=$2; shift 2 ;;
    --prompt) PROMPT=$2; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 1 ;;
  esac
done
[ -n "$TASK" ] && [ -n "$MODEL" ] || { sed -n '2,10p' "$0"; exit 1; }

spec="$root/tasks/$TASK/spec.json"
prompt_file="$root/tasks/$TASK/prompt-$PROMPT.md"
snap=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["start_snapshot"])' "$spec")
short=${MODEL#claude-}
trial_id="$TASK-$PROMPT-$short-$EFFORT-$(date +%Y%m%d-%H%M%S)"
out="$root/results/$PHASE/$trial_id"
mkdir -p "$out"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$out/runner.log"; }
vm() { (cd "$envdir" && vagrant ssh oam-node1 -c "$1" 2>/dev/null) | tr -d '\r'; }

# ---- 認證 ----
auth_kind="" SECRET_VAR="" SECRET=""
if [ -n "${OAM_AUTH_FILE:-}" ]; then auth_files=("$OAM_AUTH_FILE")   # 只用在 dry run
else auth_files=("$envdir/secrets/oauth.env" "$envdir/secrets/anthropic.env"); fi
for f in "${auth_files[@]}"; do
  [ -f "$f" ] || continue
  # shellcheck disable=SC1090
  tok=$(. "$f"; echo "${CLAUDE_CODE_OAUTH_TOKEN:-}")
  # shellcheck disable=SC1090
  key=$(. "$f"; echo "${ANTHROPIC_API_KEY:-}")
  if [ -n "$tok" ]; then auth_kind=subscription_oauth SECRET_VAR=CLAUDE_CODE_OAUTH_TOKEN SECRET=$tok; break; fi
  if [ -n "$key" ]; then auth_kind=api_key SECRET_VAR=ANTHROPIC_API_KEY SECRET=$key; break; fi
done
[ -n "$auth_kind" ] || { echo "no credentials: create env/secrets/oauth.env (CLAUDE_CODE_OAUTH_TOKEN=...) or env/secrets/anthropic.env (ANTHROPIC_API_KEY=...)" >&2; exit 1; }
unset tok key

cp "$prompt_file" "$out/prompt.md"
log "trial $trial_id  task=$TASK model=$MODEL effort=$EFFORT timeout=${TIMEOUT_MIN}m budget=\$$BUDGET auth=$auth_kind snapshot=$snap"

# Docker Hub 快取（env/scripts/registry-cache.sh）：掛掉時 trial 照跑（containerd 會退回 Docker Hub），但記下來
if curl -fsS -m 5 http://192.168.57.1:5050/v2/ >/dev/null 2>&1; then log "registry cache: up"
else log "WARNING registry cache: DOWN (images will come from Docker Hub directly; run env/scripts/registry-cache.sh start)"; fi

# ---- 1. 還原環境 ----
t_reset=$(date +%s)
"$envdir/scripts/env.sh" reset "$snap" >> "$out/runner.log" 2>&1
log "env reset to $snap in $(( $(date +%s) - t_reset ))s"

# spec 的 setup（2026-11-23，L2 起）：agent 開始前由 runner 套用的環境，例如 L2 的 MEC 應用。
# 在拍主機狀態基準之前套用，所以不會算成 agent 的副作用。前後各存一份物件的 spec，確認 agent 沒有改動它。
setup_files=$(python3 -I -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("setup", [])))' "$spec")
setup_snapshot() {
  for f in $setup_files; do
    vm "kubectl get -f - -o json" < "$root/$f" | python3 -I -c '
import json, sys
d = json.load(sys.stdin)
for o in d.get("items", [d]):
    print(o["kind"], o["metadata"].get("namespace", ""), o["metadata"]["name"], json.dumps(o.get("spec"), sort_keys=True))'
  done
}
if [ -n "$setup_files" ]; then
  for f in $setup_files; do
    vm "kubectl apply -f -" < "$root/$f" >> "$out/runner.log" 2>&1
    log "setup applied: $f"
  done
  # 等 Deployment 都好了才開始（例如 MEC 應用要真的能回應）
  for f in $setup_files; do
    vm "kubectl get -f - --no-headers -o custom-columns=K:.kind,NS:.metadata.namespace,N:.metadata.name | \
        awk '\$1==\"Deployment\" {print \$2, \$3}' | while read -r ns n; do kubectl -n \$ns rollout status deploy/\$n --timeout=180s; done" \
      < "$root/$f" >> "$out/runner.log" 2>&1 || true
  done
  setup_snapshot > "$out/setup-before.txt"
fi

# 主機層狀態的基準（agent 開始前）。結果存在 host，agent 看不到。
hoststate() { (cd "$envdir" && vagrant ssh oam-node1 -c 'sudo bash -s' < "$root/verify/hoststate.sh" 2>/dev/null) | tr -d '\r'; }
hoststate > "$out/hoststate-before.txt"
log "host state baseline: $(wc -l < "$out/hoststate-before.txt") lines"

# ---- 2. 把 prompt、認證送進 VM ----
(cd "$envdir" && vagrant ssh oam-node1 -c 'mkdir -p ~/trial ~/work && cat > ~/trial/prompt.md' < "$prompt_file" 2>/dev/null)
# 只放一種認證進去：兩個變數同時存在時 Claude Code 會優先用 API key
printf 'unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN\nexport %s=%q\n' "$SECRET_VAR" "$SECRET" | \
  (cd "$envdir" && vagrant ssh oam-node1 -c 'umask 077; cat > ~/trial/auth.env' 2>/dev/null)

# ---- 3. 在 VM 內背景執行 agent（ssh 斷線也不會中斷）----
cat > "$out/agent-cmd.sh" <<EOF
#!/usr/bin/env bash
# 由 run_trial.sh 產生，在 VM 內以 vagrant 使用者執行
cd ~/work
export PATH=\$HOME/.local/bin:\$PATH DISABLE_AUTOUPDATER=1
. ~/trial/auth.env
rm -f ~/trial/auth.env
date +%s > ~/trial/start
timeout ${TIMEOUT_MIN}m claude -p "\$(cat ~/trial/prompt.md)" \\
  --model $MODEL --effort $EFFORT \\
  --output-format stream-json --verbose \\
  --dangerously-skip-permissions \\
  --disallowedTools WebSearch WebFetch \\
  --max-budget-usd $BUDGET \\
  --no-session-persistence \\
  > ~/trial/transcript.jsonl 2> ~/trial/stderr.log
echo \$? > ~/trial/exit_code
date +%s > ~/trial/end
EOF
# 注意：寫檔要在前景做；整串放在 & 後面會讓 stdin 變成 /dev/null，檔案會是空的。
(cd "$envdir" && vagrant ssh oam-node1 -c 'cat > ~/trial/agent-cmd.sh && chmod +x ~/trial/agent-cmd.sh' < "$out/agent-cmd.sh" 2>/dev/null)
[ "$(vm 'wc -c < ~/trial/agent-cmd.sh')" -gt 100 ] || { log "agent-cmd.sh not written"; exit 1; }
vm 'setsid nohup ~/trial/agent-cmd.sh > /dev/null 2>&1 < /dev/null & sleep 2; pgrep -f agent-cmd.sh >/dev/null && echo launched' | grep -q launched \
  || { log "agent failed to launch"; exit 1; }
log "agent started"

# ---- 4. 等 agent 結束 ----
deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 + 300 ))
while :; do
  ec=$(vm 'cat ~/trial/exit_code 2>/dev/null || true' || true)
  [ -n "$ec" ] && break
  if [ "$(date +%s)" -gt "$deadline" ]; then ec=runner_timeout; break; fi
  n=$(vm 'wc -l < ~/trial/transcript.jsonl 2>/dev/null || echo 0' || true)
  echo "[$(date +%H:%M:%S)] running... transcript lines=$n" >> "$out/runner.log"
  sleep 30
done
log "agent exited: exit_code=$ec"

# ---- 5. 收集紀錄（認證 token／key 一律遮蔽）----
redact() { python3 -I -c 'import sys; k=sys.argv[1]; d=sys.stdin.read(); sys.stdout.write(d.replace(k, "<REDACTED_SECRET>"))' "$SECRET"; }
{ vm 'cat ~/trial/transcript.jsonl' || true; } | redact > "$out/transcript.jsonl"
{ vm 'cat ~/trial/stderr.log' || true; } | redact > "$out/agent-stderr.log"
vm 'echo "start=$(cat ~/trial/start 2>/dev/null) end=$(cat ~/trial/end 2>/dev/null)"' > "$out/agent-times.txt"
# agent 產生的檔案與叢集狀態（之後人工分析用）
(cd "$envdir" && vagrant ssh oam-node1 -c 'tar -C ~ -czf - work 2>/dev/null' 2>/dev/null) > "$out/work.tgz" || true
{ vm 'helm list -A; echo; kubectl get all -A -o wide; echo; kubectl get configmap -A --no-headers | grep -vE "^(kube-system|kube-public|kube-node-lease|default) "' || true; } | redact > "$out/cluster-state.txt"
{ vm 'for ns in $(kubectl get ns -o name | sed "s#namespace/##" | grep -vE "^(kube-system|kube-public|kube-node-lease|default)$"); do kubectl -n $ns get configmap -o yaml; done' || true; } | redact > "$out/configmaps.yaml"
vm 'sudo tail -n 300 /var/log/syslog 2>/dev/null | grep -iE "gtp5g|upf" | tail -50' > "$out/vm-syslog-gtp.txt" || true

[ -n "$setup_files" ] && setup_snapshot > "$out/setup-after.txt"
# agent 對 VM／K3s 做了哪些變更（只記錄，不影響 PASS／FAIL）
hoststate > "$out/hoststate-after.txt"
python3 -I "$root/verify/sidefx.py" "$out/hoststate-before.txt" "$out/hoststate-after.txt" > "$out/sidefx.json" || true

# ---- 6. 驗證（給系統 60 秒穩定）----
sleep 60
"$root/verify/verify.sh" "$spec" -o "$out/verify.json" > /dev/null 2> "$out/verify-summary.txt" || true
cat "$out/verify-summary.txt" | tee -a "$out/runner.log"

# V6：NF 間的 dataflow 與封包內容（2026-11-20）。會讓 UE 去註冊、重啟 gNB，所以排在 V1–V3x 之後。
# V3 沒過時 dataflow_capture.sh 會自己跳過（前提不成立 → 無法判定）。OAM_NO_DATAFLOW=1 可以關掉。
if [ "${OAM_NO_DATAFLOW:-0}" != 1 ]; then
  "$root/verify/dataflow.sh" "$spec" "$out" 2> "$out/dataflow-summary.txt" || true
  tail -1 "$out/dataflow-summary.txt" | tee -a "$out/runner.log"
fi
# V5：跨 NF 設定一致性（靜態，看 configmaps.yaml；有 V6 匯出的 subscriber 資料時一併比對）
subs_arg=()
[ -s "$out/dataflow/subs.json" ] && subs_arg=(--subs "$out/dataflow/subs.json")
python3 -I "$root/verify/consistency.py" "$out/configmaps.yaml" "$spec" --services "$out/cluster-state.txt" "${subs_arg[@]}" \
  > "$out/consistency.json" 2> "$out/consistency-summary.txt" || true
tail -1 "$out/consistency-summary.txt" | tee -a "$out/runner.log"

# ---- 7. 寫紀錄 ----
python3 -I "$root/trial/summarize.py" "$out" "$TASK" "$PROMPT" "$MODEL" "$EFFORT" "$TIMEOUT_MIN" "$BUDGET" "$auth_kind" "$snap" "$ec" \
  | tee "$out/meta.json" >> "$root/results/$PHASE/trials.jsonl"
log "recorded in results/$PHASE/trials.jsonl"
python3 -I - "$out/meta.json" <<'PY' | tee -a "$out/runner.log"
import json, sys
m = json.load(open(sys.argv[1]))
a, v = m["agent"], m["verify"]
est = " (est.)" if a.get("cost_is_estimate") else ""
print(f"RESULT {m['trial_id']}: pass={v['pass']} strict={v.get('pass_strict')} "
      f"(V1={v['v1']} V2={v['v2']} V3={v['v3']} V3x={v.get('v3x')} V4={v.get('v4')} req={v['req']} setup_untouched={v.get('setup_untouched')} "
      f"V5={(v.get('v5') or {}).get('pass')} V6={(v.get('v6') or {}).get('pass')}) "
      f"cost=${a['cost_usd']}{est} turns={a['num_turns']} wall={a['wall_s']}s exit={a['exit_code']} "
      f"valid={m['valid']} {','.join(m['invalid_reasons'])} "
      f"side_effects(infra={m['side_effects'].get('infra_changes')} tooling={m['side_effects'].get('tooling_changes')} auto={m['side_effects'].get('auto_changes')})")
PY
