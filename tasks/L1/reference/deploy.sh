#!/usr/bin/env bash
# L1 參考答案：從 S1-k3s-ready 開始，部署單一 UPF 的 free5GC + UERANSIM，並建立 subscriber。
# 在 host 上執行；透過 vagrant ssh 操作 VM，透過 host-only 網路呼叫 WebUI API。
#
#   tasks/L1/reference/deploy.sh            部署（VM 必須是乾淨的 S1 狀態）
#   OAM_RESET=1 tasks/L1/reference/deploy.sh 先還原 S1 再部署
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
envdir="$root/env"
NODE_IP=192.168.57.11
NS=free5gc
REL=free5gc-helm           # ueransim chart 預設用這個 release 名稱找 AMF
DEPLOY_DIR=/home/vagrant/free5gc-deploy
SUPI=imsi-208930000000001
PLMN=20893

vm() { (cd "$envdir" && vagrant ssh oam-node1 -c "$1" 2>/dev/null); }
log() { echo "[$(date +%H:%M:%S)] $*"; }
t0=$(date +%s)

[ "${OAM_RESET:-0}" = 1 ] && "$envdir/scripts/env.sh" reset

log "1/6 build chart"
"$here/build-chart.sh"

log "2/6 copy chart + values into VM ($DEPLOY_DIR)"
tar -C "$here" -czf - build/charts values-l1.yaml | \
  (cd "$envdir" && vagrant ssh oam-node1 -c "rm -rf $DEPLOY_DIR && mkdir -p $DEPLOY_DIR && tar -xzf - -C $DEPLOY_DIR" 2>/dev/null)

log "3/6 helm install free5gc + ueransim"
vm "set -e
  kubectl get ns $NS >/dev/null 2>&1 || kubectl create ns $NS
  cd $DEPLOY_DIR
  helm -n $NS upgrade --install $REL build/charts/free5gc -f values-l1.yaml >/dev/null
  helm -n $NS upgrade --install ueransim build/charts/ueransim >/dev/null
  helm -n $NS list"

log "4/6 wait for pods"
for i in $(seq 1 80); do
  bad=$(vm "kubectl -n $NS get pods --no-headers" | awk '{split($2,a,"/"); if (!($3=="Running" && a[1]==a[2]) && $3!="Completed") print $1" "$3}')
  [ -z "$bad" ] && break
  sleep 15
done
[ -z "$bad" ] || { echo "pods not ready:"; echo "$bad"; exit 1; }
log "   all pods ready"

log "5/6 create subscriber $SUPI via WebUI API"
python3 -I - "$here/subscriber-imsi-208930000000001.json" "http://$NODE_IP:30500" "$SUPI" "$PLMN" <<'EOF'
import json, sys, time, urllib.request, urllib.error
payload, base, supi, plmn = sys.argv[1:5]
def req(m, u, tok=None, body=None):
    r = urllib.request.Request(base + u, data=json.dumps(body).encode() if body is not None else None, method=m)
    r.add_header("Content-Type", "application/json")
    if tok: r.add_header("Token", tok)
    try:
        with urllib.request.urlopen(r, timeout=10) as x: return x.status, x.read()
    except urllib.error.HTTPError as e: return e.code, e.read()
    except OSError as e: return 0, str(e).encode()
for _ in range(30):
    st, b = req("POST", "/api/login", body={"username": "admin", "password": "free5gc"})
    if st == 200: break
    time.sleep(5)
else:
    sys.exit(f"webui login failed: {st} {b[:200]}")
tok = json.loads(b)["access_token"]
# 注意：GET /api/subscriber/{supi}/{plmn} 對不存在的 subscriber 也回 200（空結構），所以要看清單。
st, b = req("GET", "/api/subscriber", tok)
if st == 200 and any(s.get("ueId") == supi for s in (json.loads(b or b"[]") or [])):
    print("   subscriber already exists"); sys.exit(0)
st, b = req("POST", f"/api/subscriber/{supi}/{plmn}/1", tok, json.load(open(payload)))
if st not in (200, 201): sys.exit(f"create subscriber failed: {st} {b[:200]}")
print("   subscriber created")
EOF

log "6/6 restart UE so it registers with the new subscriber"
# 不用 rollout restart：新舊 UE Pod 會短暫並存、用同一個 SUPI 互相把對方踢掉。先縮到 0、等舊 Pod 消失再開。
vm "set -e
  kubectl -n $NS scale deploy/ueransim-ue --replicas=0 >/dev/null
  kubectl -n $NS wait --for=delete pod -l app=ueransim,component=ue --timeout=120s >/dev/null 2>&1 || true
  while kubectl -n $NS get pod -o name | grep -q ueransim-ue; do sleep 2; done
  kubectl -n $NS scale deploy/ueransim-ue --replicas=1 >/dev/null
  kubectl -n $NS rollout status deploy/ueransim-ue --timeout=180s >/dev/null"
UE=$(vm "kubectl -n $NS get pod -o name | grep ueransim-ue | head -1" | tr -d '\r')
for i in $(seq 1 24); do
  vm "kubectl -n $NS exec $UE -- ip link show uesimtun0" >/dev/null 2>&1 && break
  sleep 5
done
out=$(vm "kubectl -n $NS exec $UE -- sh -c 'ip -br addr show uesimtun0; ping -I uesimtun0 -c 4 -W 2 8.8.8.8'" || true)
echo "$out" | grep -E "uesimtun0|packets|rtt"
echo "$out" | grep -qE " 0% packet loss" || { log "FAIL: UE cannot reach 8.8.8.8 via uesimtun0"; exit 1; }
log "PASS: done in $(( $(date +%s) - t0 ))s"
