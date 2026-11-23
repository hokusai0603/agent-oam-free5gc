#!/usr/bin/env bash
# V6 的 VM 端：在 VM 內執行（由 verify/dataflow.sh 經 vagrant ssh 送進來，agent 看不到）。
# 1. 找出 UERANSIM 的 UE／gNB Pod（有 nr-cli、--dump 列出 imsi-… 或 gNB 名稱的就是）
# 2. 在 cni0 上抓封包
# 3. UE 正常去註冊（switch-off）→ 重啟 UE、gNB 的 container → 等 UE 重新註冊並建好 PDU session → 經 uesimtun0 ping
# 4. 匯出 Pod／Service 的 IP 對照表、MongoDB 的 subscriber 資料
# 輸出放在 /tmp/oam-df/，最後印一行 JSON（meta）。
#
# 注意：會重啟 agent 部署的 UE 與 gNB container（不刪 Pod，Pod 名稱和 IP 不變）。
# 所以要在 V1–V3x 之後才跑。沒有先去註冊就直接重啟 UE，AMF 會留著舊的 context，UE 會卡在重送 PDU session（實測）。
set -uo pipefail
OUT=/tmp/oam-df
WAIT_TUN=${WAIT_TUN:-60}
PING_N=${PING_N:-5}
sudo rm -rf "$OUT"; mkdir -p "$OUT"
note() { echo "$*" >> "$OUT/notes.txt"; }

sysns='^(kube-system|kube-public|kube-node-lease)$'
kubectl get pods -A -o json > "$OUT/pods.json"
kubectl get svc -A -o json > "$OUT/svc.json"

# ---- 1. 找 UE／gNB ----
ue_ns="" ue_pod="" gnb_ns="" gnb_pod="" supi="" cli=""
while read -r ns pod; do
  [[ "$ns" =~ $sysns ]] && continue
  for c in /ueransim/nr-cli nr-cli /UERANSIM/build/nr-cli /opt/UERANSIM/build/nr-cli; do
    d=$(timeout 10 kubectl -n "$ns" exec "$pod" -- "$c" --dump 2>/dev/null) || continue
    [ -n "$d" ] || continue
    if echo "$d" | grep -q '^imsi-' && [ -z "$ue_pod" ]; then
      ue_ns=$ns ue_pod=$pod cli=$c supi=$(echo "$d" | grep '^imsi-' | head -1)
    elif ! echo "$d" | grep -q '^imsi-' && [ -z "$gnb_pod" ]; then
      gnb_ns=$ns gnb_pod=$pod
    fi
    break
  done
done < <(kubectl get pods -A --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name}{"\n"}{end}')

cid() { kubectl -n "$1" get pod "$2" -o jsonpath='{.status.containerStatuses[0].containerID}' | sed 's#.*://##'; }
# 重啟一個 Pod：有 controller（ReplicaSet／StatefulSet…）管的就刪掉讓它重建（不會累積 CrashLoop 的退避時間），
# 沒人管的才用 crictl 重啟 container。印出重啟後的 Pod 名稱。
restart_pod() {
  local ns=$1 pod=$2 owner sel new
  owner=$(kubectl -n "$ns" get pod "$pod" -o jsonpath='{.metadata.ownerReferences[0].kind}')
  if [ -n "$owner" ]; then
    sel=$(kubectl -n "$ns" get pod "$pod" -o json | python3 -c 'import json,sys;l=json.load(sys.stdin)["metadata"].get("labels",{});print(",".join(f"{k}={v}" for k,v in l.items()))')
    kubectl -n "$ns" delete pod "$pod" --wait=false --grace-period=1 >/dev/null 2>&1
    for _ in $(seq 1 60); do
      new=$(kubectl -n "$ns" get pods -l "$sel" --no-headers 2>/dev/null | awk -v o="$pod" '$1!=o && $3=="Running" {print $1; exit}')
      [ -n "$new" ] && { echo "$new"; return; }
      sleep 1
    done
    note "replacement for $ns/$pod not Running after 60s"
    echo "$pod"
  else
    sudo crictl stop -t 1 "$(cid "$ns" "$pod")" >/dev/null 2>&1 || note "crictl stop $pod failed"
    echo "$pod"
  fi
}
ue_ip() { kubectl -n "$ue_ns" exec "$ue_pod" -- ip -4 -o addr show uesimtun0 2>/dev/null | awk '{print $4}' | cut -d/ -f1; }

if [ -z "$ue_pod" ]; then
  python3 -c 'import json;print(json.dumps({"ok":False,"error":"no UERANSIM UE pod with nr-cli found"}))'
  exit 0
fi
before_ip=$(ue_ip)
if [ -z "$before_ip" ]; then
  # 前提：V3 已經通過、UE 有 PDU session。沒有的話重啟只會攪亂狀態（實測：殘留的註冊和新 UE 互相干擾），判定為無法進行
  python3 -c 'import json,sys;print(json.dumps({"ok":False,"precondition":False,"error":"UE has no uesimtun0 before capture (V3 not passing?)","ue":{"ns":sys.argv[1],"pod":sys.argv[2]}}))' "$ue_ns" "$ue_pod"
  exit 0
fi
hostnet=$(kubectl -n "$ue_ns" get pod "$ue_pod" -o jsonpath='{.spec.hostNetwork}')
iface=cni0
ip link show cni0 >/dev/null 2>&1 || iface=any
[ "$hostnet" = "true" ] && { iface=any; note "UE pod uses hostNetwork; capturing on any (packets may appear twice)"; }

# ---- 2. 開始抓封包 ----
sudo tcpdump -i "$iface" -s 0 -U -w "$OUT/capture.pcap" 'not port 6443 and not port 10250 and not port 22' >"$OUT/tcpdump.log" 2>&1 &
tpid=$!
sleep 2
t0=$(date +%s.%N)

# ---- 3. 去註冊 → 重啟 → 等重新註冊 ----
# 順序（都是實測出來的）：
#  階段 1（註冊與 PDU session）：先經 tunnel ping 一下讓 UE 回到 CM-CONNECTED（idle 時 switch-off 不會送出 Deregistration），
#    再去註冊。chart 的 UERANSIM UE 約 1 秒後會自己重新註冊；15 秒內沒有，才重啟 UE 的 Pod。
#  階段 2（NG Setup）：最後才重啟 gNB。不能和 UE 一起換：新的 UE 可能先連上「正在被替換」的舊 gNB，
#    接著 radio link failure，AMF 留下殘缺的 context，PDU session 一直重送。
old_ue=$ue_pod old_gnb=$gnb_pod ue_restarted=false
kubectl -n "$ue_ns" exec "$ue_pod" -- ping -I uesimtun0 -c 1 -W 2 8.8.8.8 >/dev/null 2>&1
t_dereg=$(date +%s)
kubectl -n "$ue_ns" exec "$ue_pod" -- "$cli" "$supi" -e "deregister switch-off" >"$OUT/dereg.txt" 2>&1 || note "deregister failed: $(cat "$OUT/dereg.txt")"
reup=""
for _ in $(seq 1 15); do
  sleep 1
  n=$(kubectl -n "$ue_ns" logs "$ue_pod" --since=$(( $(date +%s) - t_dereg + 1 ))s 2>/dev/null | grep -c 'PDU Session establishment is successful')
  if [ "${n:-0}" -gt 0 ] && [ -n "$(ue_ip)" ]; then reup=yes; break; fi
done
if [ -z "$reup" ]; then
  note "UE did not re-register by itself within 15s after deregister; restarting the UE pod"
  ue_pod=$(restart_pod "$ue_ns" "$ue_pod"); ue_restarted=true
fi
tun_s=""
for i in $(seq 1 "$WAIT_TUN"); do
  if [ -n "$(ue_ip)" ]; then tun_s=$i; break; fi
  sleep 1
done
ping_out=""
if [ -n "$tun_s" ]; then
  sleep 1
  ping_out=$(kubectl -n "$ue_ns" exec "$ue_pod" -- ping -I uesimtun0 -c "$PING_N" -W 2 8.8.8.8 2>&1 | tail -2 | tr '\n' ' ')
fi
# L2：經 tunnel 連 MEC（UE 的 image 沒有 curl，進 UE 的網路 namespace 借 VM 的 curl），讓 N9 上有往 MEC 的流量
mec_codes=""
if [ -n "${MEC_IP:-}" ] && [ -n "$tun_s" ]; then
  upid=$(sudo crictl inspect -o go-template --template '{{.info.pid}}' "$(cid "$ue_ns" "$ue_pod")" 2>/dev/null)
  for _ in 1 2 3; do
    c=$(sudo nsenter -t "$upid" -n curl -s -o /dev/null -m 5 --interface uesimtun0 -w '%{http_code}' "http://$MEC_IP:${MEC_PORT:-80}/" 2>/dev/null)
    mec_codes="$mec_codes ${c:-000}"
  done
fi
ip_phase1=$(ue_ip)
sleep 2
# ---- 階段 2：重啟 gNB → NG Setup；確認 UE 能回來 ----
t_gnb=""
gnb_recovered=""
if [ -n "$gnb_pod" ]; then
  t_gnb=$(date +%s.%N)
  gnb_pod=$(restart_pod "$gnb_ns" "$gnb_pod")
  for _ in $(seq 1 30); do
    kubectl -n "$gnb_ns" logs "$gnb_pod" 2>/dev/null | grep -q 'NG Setup procedure is successful' && break
    sleep 1
  done
  gnb_recovered=false
  for _ in $(seq 1 20); do
    if kubectl -n "$ue_ns" exec "$ue_pod" -- ping -I uesimtun0 -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then gnb_recovered=true; break; fi
    sleep 1
  done
  if [ "$gnb_recovered" = false ]; then
    note "UE did not recover by itself after gNB restart; restarting the UE pod"
    ue_pod=$(restart_pod "$ue_ns" "$ue_pod")
    for _ in $(seq 1 40); do [ -n "$(ue_ip)" ] && break; sleep 1; done
  fi
fi
sleep 2
# $tpid 是 sudo 的 PID，訊號不一定會轉給 tcpdump；直接找這次的 tcpdump 停掉
sudo pkill -INT -f "^tcpdump -i $iface -s 0 -U -w $OUT/capture.pcap" 2>/dev/null; sleep 1
sudo kill -INT "$tpid" 2>/dev/null
sudo chmod 644 "$OUT/capture.pcap" 2>/dev/null
after_ip=$(ue_ip)
kubectl get pods -A -o json > "$OUT/pods_after.json"
gnb_name=""
[ -n "$gnb_pod" ] && gnb_name=$(timeout 10 kubectl -n "$gnb_ns" exec "$gnb_pod" -- "$cli" --dump 2>/dev/null | head -1)

# ---- 4. subscriber 資料（V5 用）----
mpod=$(kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name} {.spec.containers[0].image}{"\n"}{end}' | awk '$3 ~ /mongo/ {print $1" "$2; exit}')
if [ -n "$mpod" ]; then
  set -- $mpod
  timeout 30 kubectl -n "$1" exec "$2" -- mongosh free5gc --quiet --eval '
    const c = n => db.getCollection(n).find({}, {_id: 0}).toArray();
    print(EJSON.stringify({
      auth: c("subscriptionData.authenticationData.authenticationSubscription"),
      am: c("subscriptionData.provisionedData.amData"),
      sm: c("subscriptionData.provisionedData.smData")}, {relaxed: true}));' > "$OUT/subs.json" 2>"$OUT/subs.err" \
    || note "mongo export failed: $(head -c 300 "$OUT/subs.err")"
else
  note "no mongodb pod found"
fi

python3 - "$OUT" <<PY
import json, sys
print(json.dumps({"ok": True, "iface": "$iface", "ue": {"ns": "$ue_ns", "pod": "$ue_pod", "supi": "$supi",
  "ip_before": "$before_ip", "ip_after": "$after_ip", "pod_before": "$old_ue"},
  "gnb": {"ns": "$gnb_ns", "pod": "$gnb_pod", "name": "$gnb_name", "pod_before": "$old_gnb"},
  "tun_up_after_s": int("$tun_s") if "$tun_s" else None, "precondition": True, "ue_restarted": "$ue_restarted" == "true",
  "ip_phase1": "$ip_phase1", "mec": {"ip": "${MEC_IP:-}", "tried": bool("${MEC_IP:-}") and bool("$tun_s"), "codes": "$mec_codes".split()}, "t_gnb_restart": float("$t_gnb") if "$t_gnb" else None,
  "ue_recovered_after_gnb_restart": {"true": True, "false": False}.get("$gnb_recovered"), "ping": "$ping_out".strip(), "t0": float("$t0"),
  "notes": open(sys.argv[1] + "/notes.txt").read().splitlines() if __import__("os").path.exists(sys.argv[1] + "/notes.txt") else []}))
PY
