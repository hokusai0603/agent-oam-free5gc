#!/usr/bin/env bash
# 從 env/vendor 裡固定版本的 free5gc-helm（07139fc，free5GC v4.3.0）產生 L2 用的 chart（ULCL：I-UPF + 2 個 PSA-UPF）。
# 輸出：tasks/L2/reference/build/charts/{free5gc,ueransim}
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
vendor="$root/env/vendor/free5gc-helm-v4.3.0"
WANT=07139fcc6f7579928a0722e274bdc876a4a4e240
MEC_IP=${MEC_IP:-10.43.200.10}
# 反向測試用：SKIP_FIX=A 不加 ulcl: true；SKIP_FIX=B 不給 AnchorUPF2 加 N3（C 在 deploy.sh 處理）
SKIP_FIX=${SKIP_FIX:-}

if [ ! -d "$vendor" ]; then
  git clone -q https://github.com/free5gc/free5gc-helm.git "$vendor"
  git -C "$vendor" checkout -q "$WANT"
fi
actual=$(git -C "$vendor" rev-parse HEAD)
[ "$actual" = "$WANT" ] || { echo "vendor is $actual, want $WANT" >&2; exit 1; }

out="$here/build"
rm -rf "$out"; mkdir -p "$out"
cp -r "$vendor/charts" "$out/"

# chart 的 ULCL 設定寫死在 SMF 的 template 裡（不在 values），導流規則是「目的地 1.0.0.1/32 走 AnchorUPF2」，
# 另外還有一個用不到的 UE2（imsi-208930000000004）。改成：目的地 = MEC 應用的 IP，只留 UE1。
tpl="$out/charts/free5gc/charts/free5gc-smf/templates/_smf-ulcl.tpl"
python3 -I - "$tpl" "$MEC_IP" "$SKIP_FIX" <<'EOF'
import re, sys
p, mec, skip = sys.argv[1:4]; s = open(p).read()
s, n1 = re.subn(r"- dest: 1\.0\.0\.1/32", f"- dest: {mec}/32", s)
s, n2 = re.subn(r"\nUE2:\n(?:  .*\n?)*", "\n", s)
# 坑：template 沒有 `ulcl: true`。SMF 的 ulcl 預設 false，會忽略 uerouting.yaml 的 specificPath，
# 只建 gNB→BranchingUPF→AnchorUPF1 的預設路徑：全部流量（包含往 MEC 的）都從 AnchorUPF1 出去。
s, n3 = re.subn(r"\nsmfName: SMF\n", "\nsmfName: SMF\nulcl: true\n", s) if skip != "A" else (s, 1)
# 坑：free5GC v4.3.0 SMF 的 BPManager.SelectPSA2 檢查「路徑最後一個 UPF（PSA2）有沒有 N3 介面」，
# 正確應該檢查第一個（I-UPF）；上游 2026-10-25 才修（free5gc/smf a510e92）。PSA2 只有 N9 時 SMF 記
# "Skipping path via UPF [...] (DNN mismatch)"，specificPath 不生效。替代做法：AnchorUPF2 多宣告一個 N3 介面（同一個 IP）。
m = re.search(r"\n    AnchorUPF2:\n(?:      .*\n)*?      interfaces:\n", s)
ep = "{{ if .Values.global.smf.multus.enabled }}{{ .Values.global.upf.multus.psaupf2.n9if.ipAddress }}{{ else }}PSAUPF2_SERVICE_NAME{{ end }}"
n4 = 0
if m and skip == "B":
    n4 = 1
elif m:
    s = s[:m.end()] + f"        - interfaceType: N3\n          endpoints:\n            - {ep}\n          networkInstances:\n            - internet\n" + s[m.end():]
    n4 = 1
if (n1, n2, n3, n4) != (1, 1, 1, 1): sys.exit(f"{p}: patch failed (dest={n1}, UE2={n2}, ulcl={n3}, psa2_n3={n4})")
open(p, "w").write(s)
EOF

echo "chart built at $out/charts (free5gc-helm ${actual:0:7}, ULCL, MEC $MEC_IP${SKIP_FIX:+, SKIP_FIX=$SKIP_FIX})"
