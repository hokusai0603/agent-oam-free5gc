#!/usr/bin/env bash
# V6 的反向測試：在 VM 上一個能用的 L1 部署上，故意弄壞一處，確認 dataflow 檢查抓得到。
#   verify/tests/dataflow_faults.sh <out_dir> [fault...]
# fault：
#   flowrule   subscriber 加一條 flow rule（WebUI API）→ 觸發參考答案坑 7（單一 UPF 帶 dnaiList [mec]）
#   dl_drop    UPF 丟掉送往 gNB 的 GTP-U（下行不通）
#   no_smdata  拿掉 subscriber 的 session management 訂閱資料（MongoDB）→ PDU session 建不起來
# 每個 fault 做完都會還原。
set -uo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
root=$(cd "$here/.." && pwd)
out=$(realpath "${1:?usage: dataflow_faults.sh <out_dir> [fault...]}")
shift
faults=("$@")
[ ${#faults[@]} -gt 0 ] || faults=(flowrule dl_drop no_smdata)
spec="$root/tasks/L1/spec.json"
vm() { (cd "$root/env" && vagrant ssh oam-node1 -c "$1" 2>/dev/null | tr -d '\r'); }

# WebUI API：登入、讀寫 subscriber（在 VM 裡經 ClusterIP 連）
WEBUI='
W=$(kubectl get svc -A -o jsonpath="{range .items[*]}{.spec.clusterIP}:{.spec.ports[0].port} {.metadata.name}{\"\n\"}{end}" | awk "/webui-service/ {print \$1; exit}")
TOK=$(curl -s -X POST http://$W/api/login -H "Content-Type: application/json" -d "{\"username\":\"admin\",\"password\":\"free5gc\"}" | python3 -c "import json,sys;print(json.load(sys.stdin)[\"access_token\"])")
'
SUB=imsi-208930000000001/20893

for f in "${faults[@]}"; do
  echo "=== fault: $f"
  case $f in
    flowrule)
      vm "$WEBUI"'
curl -s http://$W/api/subscriber/'$SUB' -H "Token: $TOK" > /tmp/sub-orig.json
python3 - <<PY
import json
s = json.load(open("/tmp/sub-orig.json"))
s["FlowRules"] = [{"filter": "1.1.1.1/32", "precedence": 128, "snssai": "01010203", "dnn": "internet", "qosRef": 1}]
s["QosFlows"] = [{"snssai": "01010203", "dnn": "internet", "qosRef": 1, "5qi": 8, "mbrUL": "208 Mbps", "mbrDL": "208 Mbps", "gbrUL": "108 Mbps", "gbrDL": "108 Mbps"}]
json.dump(s, open("/tmp/sub-new.json", "w"))
PY
curl -s -o /dev/null -w "PUT %{http_code}\n" -X PUT http://$W/api/subscriber/'$SUB' -H "Token: $TOK" -H "Content-Type: application/json" -d @/tmp/sub-new.json'
      ;;
    dl_drop)
      vm 'p=$(kubectl get pods -A -o wide --no-headers | awk "/upf/ {print \$1\" \"\$2; exit}"); set -- $p
cid=$(kubectl -n $1 get pod $2 -o jsonpath="{.status.containerStatuses[0].containerID}" | sed "s#.*://##")
pid=$(sudo crictl inspect --template "{{.info.pid}}" -o go-template $cid)
echo $pid > /tmp/upf-pid; sudo nsenter -t $pid -n iptables -I OUTPUT -p udp --dport 2152 -j DROP && echo "DROP inserted in UPF netns"'
      ;;
    no_smdata)
      vm 'm=$(kubectl get pods -A --no-headers | awk "/mongodb/ {print \$1\" \"\$2; exit}"); set -- $m
kubectl -n $1 exec $2 -- mongosh free5gc --quiet --eval "
  const c = db.getCollection(\"subscriptionData.provisionedData.smData\");
  const docs = c.find({ueId: \"imsi-208930000000001\"}).toArray();
  db.getCollection(\"oamtest_smData_backup\").insertMany(docs.length ? docs : [{empty: true}]);
  print(\"removed \" + c.deleteMany({ueId: \"imsi-208930000000001\"}).deletedCount);"'
      ;;
  esac
  mkdir -p "$out/$f"
  "$here/dataflow.sh" "$spec" "$out/$f" 2>"$out/$f/summary.txt" >/dev/null
  # dataflow.sh 的摘要在 dataflow.py 的 stderr；重新產生一次方便看
  python3 -I "$here/dataflow.py" "$out/$f/dataflow" "$spec" 2>"$out/$f/summary.txt" >/dev/null
  grep -E 'ERROR|warn|extra|V6' "$out/$f/summary.txt"
  echo "--- restore $f"
  case $f in
    flowrule)
      vm "$WEBUI"'
curl -s -o /dev/null -w "PUT %{http_code}\n" -X PUT http://$W/api/subscriber/'$SUB' -H "Token: $TOK" -H "Content-Type: application/json" -d @/tmp/sub-orig.json'
      ;;
    dl_drop)
      vm 'sudo nsenter -t $(cat /tmp/upf-pid) -n iptables -D OUTPUT -p udp --dport 2152 -j DROP && echo "DROP removed"'
      ;;
    no_smdata)
      vm 'm=$(kubectl get pods -A --no-headers | awk "/mongodb/ {print \$1\" \"\$2; exit}"); set -- $m
kubectl -n $1 exec $2 -- mongosh free5gc --quiet --eval "
  const b = db.getCollection(\"oamtest_smData_backup\");
  const docs = b.find({empty: {\$exists: false}}, {_id: 0}).toArray();
  if (docs.length) db.getCollection(\"subscriptionData.provisionedData.smData\").insertMany(docs);
  b.drop(); print(\"restored \" + docs.length);"'
      ;;
  esac
  # 還原後讓 UE 重新建立 session（故障期間 UE 可能已經放棄重試）
  vm 'u=$(kubectl get pods -A --no-headers | awk "/ueransim-ue|-ue-/ {print \$1\" \"\$2; exit}"); set -- $u
kubectl -n $1 delete pod $2 --wait=true >/dev/null
for i in $(seq 1 60); do n=$(kubectl -n $1 get pods --no-headers | awk "/ueransim-ue|-ue-/ && \$3==\"Running\" {print \$1; exit}")
  [ -n "$n" ] && kubectl -n $1 exec $n -- ip -4 -br a show uesimtun0 2>/dev/null | grep -q . && { echo "UE recovered after ${i}s"; break; }; sleep 1; done'
done
