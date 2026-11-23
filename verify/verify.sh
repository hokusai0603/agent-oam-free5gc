#!/usr/bin/env bash
# 驗證 VM 目前的網路狀態（V1 Pod、V2 註冊／PDU session、V3 uesimtun0 ping、需求符合度）。
# 在 host 上執行；探針經 vagrant ssh 送進 VM，agent 看不到這支腳本。
#
#   verify/verify.sh tasks/L1/spec.json            輸出一行 JSON（stdout），摘要（stderr）
#   verify/verify.sh tasks/L1/spec.json -o out.json
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
spec_file=${1:?usage: verify.sh <spec.json> [-o out.json]}
out=""
[ "${2:-}" = "-o" ] && out=${3:?}

spec=$(python3 -I -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))' "$spec_file")
remote_spec=$(printf '%q' "$spec")
json=$(cd "$root/env" && vagrant ssh oam-node1 -c "cat > /tmp/oam-probe.py && python3 /tmp/oam-probe.py $remote_spec; rm -f /tmp/oam-probe.py" \
        < "$here/probe.py" 2>/dev/null | tr -d '\r' | grep '^{' | tail -1)
[ -n "$json" ] || { echo '{"pass": false, "error": "probe produced no output"}'; exit 2; }

if [ -n "$out" ]; then echo "$json" > "$out"; fi
echo "$json"
python3 -I - "$json" >&2 <<'EOF'
import json, sys
r = json.loads(sys.argv[1])
mark = lambda b: "PASS" if b else ("n/a" if b is None else "FAIL")
print(f'V1 pods     {mark(r["v1"]["pass"])}  pods={r["v1"]["pods"]} not_ready={len(r["v1"]["not_ready"])} restarts={r["v1"]["total_restarts"]}')
v2 = r["v2"]
print(f'V2 control  {mark(v2["pass"])}  gnb={v2["gnb_found"]} ngap_up={v2["gnb_ngap_up"]} ue={v2["ue_found"]} registered={v2["ue_registered"]} pdu_active={v2["ue_with_active_pdu"]}')
pings = ", ".join(f'{p["where"]}:{p["rx"]}/{p["tx"]}' for p in r["v3"]["pings"]) or "no uesimtun0"
print(f'V3 e2e      {mark(r["v3"]["pass"])}  {pings}')
for u in r.get("v3x", {}).get("ues", []):
    parts = []
    for k, c in u.get("checks", {}).items():
        extra = ""
        if k == "first_hop_is_upf":
            extra = f'({c["hops"][0]})'
        elif "tries" in c:
            last = c["tries"][-1]
            extra = f'({last["code"]},{last["size"]}B)'
        parts.append(f'{k}={mark(c["pass"])}{extra}')
    print(f'V3x {u["where"]}: ' + (" ".join(parts) or u.get("error", "")))
if "v3x" in r:
    print(f'V3x         {mark(r["v3x"]["pass"])}  (web/DNS/first hop via uesimtun0)')
for u in r.get("v4", {}).get("ues", []):
    c = u.get("checks", {})
    print(f'V4 {u["where"]}: ' + (" ".join(f'{k}={mark(v["pass"])}' for k, v in c.items()) or u.get("error", ""))
          + f'  mec_anchor={u.get("mec_anchor")} internet_anchor={u.get("internet_anchor")} remote_addr={u.get("mec_remote_addr")}')
if "v4" in r:
    print(f'V4 MEC      {mark(r["v4"]["pass"])}  (MEC and internet leave via different UPFs, both via a branching UPF)')
for k, c in r["req"]["checks"].items():
    print(f'  req {k:10s} {mark(c["pass"])}  expect={c["expect"]} actual={c["actual"]}')
print(f'OVERALL     {mark(r["pass"])}')
if "pass_strict" in r:
    print(f'STRICT      {mark(r["pass_strict"])}  (OVERALL + V3x)')
EOF
