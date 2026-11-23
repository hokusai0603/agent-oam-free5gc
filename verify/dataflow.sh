#!/usr/bin/env bash
# V6：NF 之間的 dataflow 與封包內容。在 host 上執行。
#   verify/dataflow.sh tasks/L1/spec.json <out_dir>
# 1. 把 dataflow_capture.sh 送進 VM 執行（UE 去註冊→重啟→重新註冊，同時在 cni0 抓封包）
# 2. 把 pcap、Pod／Service 對照表、subscriber 資料拿回 host 的 <out_dir>/dataflow/
# 3. 在 host 上用 tshark 解碼、比對（dataflow.py）→ <out_dir>/dataflow.json
# 會重啟 agent 部署的 UE／gNB container，所以要排在 V1–V3x 之後。
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
spec=$(realpath "${1:?usage: dataflow.sh <spec.json> <out_dir>}")
out=${2:?}
mkdir -p "$out/dataflow"
out=$(realpath "$out")
d="$out/dataflow"
cd "$root/env"
# L2：spec 有 MEC 時，抓封包期間也送 MEC 流量
mec=$(python3 -I -c 'import json,sys; m=json.load(open(sys.argv[1])).get("expect",{}).get("mec") or {}; print(m.get("ip",""), m.get("port",80))' "$spec")
read -r mec_ip mec_port <<< "$mec"
meta=$(vagrant ssh oam-node1 -c "cat > /tmp/oam-dfcap.sh && MEC_IP='$mec_ip' MEC_PORT='$mec_port' bash /tmp/oam-dfcap.sh; rm -f /tmp/oam-dfcap.sh" \
        < "$here/dataflow_capture.sh" 2>/dev/null | tr -d '\r' | grep '^{' | tail -1)
[ -n "$meta" ] || meta='{"ok": false, "error": "capture script produced no output"}'
echo "$meta" > "$d/meta.json"
for f in capture.pcap pods.json pods_after.json svc.json subs.json notes.txt dereg.txt; do
  vagrant ssh oam-node1 -c "test -f /tmp/oam-df/$f && cat /tmp/oam-df/$f" > "$d/$f" 2>/dev/null || rm -f "$d/$f"
done
vagrant ssh oam-node1 -c 'rm -rf /tmp/oam-df' >/dev/null 2>&1
python3 -I "$here/dataflow.py" "$d" "$spec" > "$out/dataflow.json"
