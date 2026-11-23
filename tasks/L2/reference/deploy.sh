#!/usr/bin/env bash
# L2 參考答案：從 S1-k3s-ready 開始，部署 ULCL（I-UPF + PSA-UPF1 上網 + PSA-UPF2 往 MEC）的 free5GC v4.3.0 + UERANSIM，
# 建立 subscriber，最後檢查 (1) UE 能上網 (2) UE 連 MEC 時，MEC 看到的來源是 PSA-UPF2。
# 在 host 上執行；透過 vagrant ssh 操作 VM。
#
#   tasks/L2/reference/deploy.sh             部署（VM 必須是乾淨的 S1 狀態）
#   OAM_RESET=1 tasks/L2/reference/deploy.sh 先還原 S1 再部署
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
envdir="$root/env"
NS=free5gc
REL=free5gc-helm           # ueransim chart 預設用這個 release 名稱找 AMF
DEPLOY_DIR=/home/vagrant/free5gc-deploy
SUPI=imsi-208930000000001
PLMN=20893
MEC_IP=10.43.200.10

vm() { (cd "$envdir" && vagrant ssh oam-node1 -c "$1" 2>/dev/null); }
vm_stdin() { (cd "$envdir" && vagrant ssh oam-node1 -c "$1" 2>/dev/null); }
log() { echo "[$(date +%H:%M:%S)] $*"; }
t0=$(date +%s)

[ "${OAM_RESET:-0}" = 1 ] && "$envdir/scripts/env.sh" reset

log "0/7 environment: MEC app ($MEC_IP)"
vm_stdin "kubectl apply -f - >/dev/null && kubectl -n mec rollout status deploy/mec-app --timeout=180s >/dev/null" < "$root/tasks/L2/setup/mec-app.yaml"

log "1/7 build chart"
"$here/build-chart.sh"

log "2/7 copy chart + values into VM ($DEPLOY_DIR)"
# SKIP_FIX=C（反向測試）：拿掉 psaupf2 的 cidr 覆寫，回到 chart 預設的 10.60.128.0/17
if [ "${SKIP_FIX:-}" = C ]; then sed '/^free5gc-upf:/,$d' "$here/values-l2.yaml" > "$here/build/values-l2.yaml"
else cp "$here/values-l2.yaml" "$here/build/values-l2.yaml"; fi
tar -C "$here" -czf - build/charts build/values-l2.yaml subscriber-imsi-208930000000001.json | \
  vm_stdin "rm -rf $DEPLOY_DIR && mkdir -p $DEPLOY_DIR && tar -xzf - -C $DEPLOY_DIR"

log "3/7 helm install free5gc + ueransim"
vm "set -e
  kubectl get ns $NS >/dev/null 2>&1 || kubectl create ns $NS
  cd $DEPLOY_DIR
  helm -n $NS upgrade --install $REL build/charts/free5gc -f build/values-l2.yaml >/dev/null
  helm -n $NS upgrade --install ueransim build/charts/ueransim >/dev/null
  helm -n $NS list"

log "4/7 wait for pods"
for i in $(seq 1 80); do
  bad=$(vm "kubectl -n $NS get pods --no-headers" | awk '{split($2,a,"/"); if (!($3=="Running" && a[1]==a[2]) && $3!="Completed") print $1" "$3}')
  [ -z "$bad" ] && break
  sleep 15
done
[ -z "$bad" ] || { echo "pods not ready:"; echo "$bad"; exit 1; }
log "   all pods ready"

log "5/7 create subscriber $SUPI via WebUI API (ClusterIP, from inside the VM)"
vm_stdin "python3 - $DEPLOY_DIR/subscriber-imsi-208930000000001.json $SUPI $PLMN" <<'EOF'
import json, subprocess, sys, time, urllib.request, urllib.error
payload, supi, plmn = sys.argv[1:4]
ip = subprocess.check_output(["kubectl", "-n", "free5gc", "get", "svc", "webui-service", "-o", "jsonpath={.spec.clusterIP}"], text=True)
base = f"http://{ip}:5000"
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
# GET /api/subscriber/{supi}/{plmn} 對不存在的 subscriber 也回 200（空結構），所以要看清單。
st, b = req("GET", "/api/subscriber", tok)
if st == 200 and any(s.get("ueId") == supi for s in (json.loads(b or b"[]") or [])):
    print("   subscriber already exists"); sys.exit(0)
st, b = req("POST", f"/api/subscriber/{supi}/{plmn}/1", tok, json.load(open(payload)))
if st not in (200, 201): sys.exit(f"create subscriber failed: {st} {b[:200]}")
print("   subscriber created")
EOF

log "6/7 restart UE so it registers with the new subscriber"
# 不用 rollout restart：新舊 UE Pod 會短暫並存、用同一個 SUPI 互相把對方踢掉。先縮到 0、等舊 Pod 消失再開。
vm "set -e
  kubectl -n $NS scale deploy/ueransim-ue --replicas=0 >/dev/null
  while kubectl -n $NS get pod -o name | grep -q ueransim-ue; do sleep 2; done
  kubectl -n $NS scale deploy/ueransim-ue --replicas=1 >/dev/null
  kubectl -n $NS rollout status deploy/ueransim-ue --timeout=180s >/dev/null"
UE=$(vm "kubectl -n $NS get pod -o name | grep ueransim-ue | head -1" | tr -d '\r')
for i in $(seq 1 24); do
  vm "kubectl -n $NS exec $UE -- ip link show uesimtun0" >/dev/null 2>&1 && break
  sleep 5
done

log "7/7 end-to-end checks"
out=$(vm "kubectl -n $NS exec $UE -- sh -c 'ip -br addr show uesimtun0; ping -I uesimtun0 -c 4 -W 2 8.8.8.8'" || true)
echo "$out" | grep -E "uesimtun0|packets|rtt"
echo "$out" | grep -qE " 0% packet loss" || { log "FAIL: UE cannot reach 8.8.8.8 via uesimtun0"; exit 1; }

# UE image 沒有 curl：進 UE 的 network namespace，用 VM 的 curl 綁 uesimtun0 連 MEC。
# 另外讀兩個 PSA-UPF netns 裡 MASQUERADE 的計數：ping 8.8.8.8 應該只讓 psaupf1 增加。
res=$(vm_stdin "bash -s" <<EOS
set -e
netns_pid() { cid=\$(kubectl -n $NS get "\$1" -o jsonpath='{.status.containerStatuses[0].containerID}' | sed 's|.*://||'); sudo crictl inspect -o go-template --template '{{.info.pid}}' "\$cid"; }
nat() { sudo nsenter -t "\$1" -n iptables -t nat -L POSTROUTING -v -n -x | awk '/MASQUERADE/ {s+=\$1} END {print s+0}'; }
p1=\$(kubectl -n $NS get pod -o name | grep psaupf1); p2=\$(kubectl -n $NS get pod -o name | grep psaupf2)
ip1=\$(kubectl -n $NS get \$p1 -o jsonpath='{.status.podIP}'); ip2=\$(kubectl -n $NS get \$p2 -o jsonpath='{.status.podIP}')
n1=\$(netns_pid \$p1); n2=\$(netns_pid \$p2); u=\$(netns_pid $UE)
ra=\$(sudo nsenter -t \$u -n curl -sS -m 10 --interface uesimtun0 http://$MEC_IP/ | awk '/^RemoteAddr:/ {split(\$2,a,":"); print a[1]}')
a1=\$(nat \$n1); a2=\$(nat \$n2)
sudo nsenter -t \$u -n ping -I uesimtun0 -c 2 -W 2 8.8.8.8 >/dev/null || true
b1=\$(nat \$n1); b2=\$(nat \$n2)
echo "psa1=\$ip1 psa2=\$ip2 mec_saw=\${ra:-none} internet_nat_psa1=+\$((b1-a1)) internet_nat_psa2=+\$((b2-a2))"
EOS
)
echo "   $res"
# 診斷：SMF 有沒有建 PSA2 路徑（坑 A：完全沒有 PSA2 字樣；坑 B：DNN mismatch）
diag=$(vm "kubectl -n $NS logs deploy/free5gc-helm-free5gc-smf-smf 2>/dev/null" | grep -c -E 'DNN mismatch' || true)
psa2=$(vm "kubectl -n $NS logs deploy/free5gc-helm-free5gc-smf-smf 2>/dev/null" | grep -c -E 'PSA2' || true)
echo "   smf_log: dnn_mismatch=$diag psa2_lines=$psa2"
kv() { echo "$res" | tr ' ' '\n' | sed -n "s/^$1=//p"; }
[ "$(kv mec_saw)" != none ] || { log "FAIL: UE cannot reach MEC $MEC_IP via uesimtun0"; exit 1; }
[ "$(kv mec_saw)" = "$(kv psa2)" ] || { log "FAIL: MEC saw source $(kv mec_saw), expected PSA-UPF2 $(kv psa2)"; exit 1; }
[ "$(kv internet_nat_psa1)" != +0 ] && [ "$(kv internet_nat_psa2)" = +0 ] || { log "FAIL: internet traffic did not leave via PSA-UPF1 only"; exit 1; }
log "PASS: done in $(( $(date +%s) - t0 ))s"
