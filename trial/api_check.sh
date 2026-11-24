#!/usr/bin/env bash
# 在 VM 內用很小的 prompt 呼叫一次模型，確認 API 有沒有恢復（529 Overloaded 之後用）。
#   trial/api_check.sh claude-sonnet-5-5
# 輸出一行：API_OK model=... 或 API_FAIL model=... status=... 。exit 0 = OK、1 = 失敗。
# 認證只經 stdin 傳進 VM，不會出現在指令列或輸出。
set -uo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
envdir="$root/env"
MODEL=${1:?usage: $0 model}
f="$envdir/secrets/oauth.env"
# shellcheck disable=SC1090
tok=$(. "$f"; echo "${CLAUDE_CODE_OAUTH_TOKEN:-}")
[ -n "$tok" ] || { echo "API_FAIL model=$MODEL status=no_credentials"; exit 1; }
res=$(printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$tok" | (cd "$envdir" && vagrant ssh oam-node1 -c \
  "read -r l; export \"\$l\"; export PATH=\$HOME/.local/bin:\$PATH DISABLE_AUTOUPDATER=1; cd /tmp; \
   timeout 180 claude -p 'Reply with exactly: OK' --model $MODEL --output-format json --no-session-persistence --max-budget-usd 1 2>/dev/null | tail -1" 2>/dev/null) | tr -d '\r')
unset tok
printf '%s' "$res" | python3 -c '
import json, sys
model = sys.argv[1]
try:
    j = json.loads(sys.stdin.read())
except Exception:
    print(f"API_FAIL model={model} status=no_json"); sys.exit(1)
st = j.get("api_error_status") or j.get("subtype")
msg = str(j.get("result", ""))
cost = j.get("total_cost_usd", 0) or 0
if j.get("is_error") or j.get("api_error_status"):
    print(f"API_FAIL model={model} status={st} msg={msg[:80]!r}"); sys.exit(1)
print(f"API_OK model={model} reply={msg[:20]!r} cost=${cost:.4f}")
' "$MODEL"
