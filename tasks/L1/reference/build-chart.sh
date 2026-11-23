#!/usr/bin/env bash
# 從 env/vendor 裡固定版本的 free5gc-helm 產生 L1 用的 chart（單一 UPF）。
# 輸出：tasks/L1/reference/build/charts/{free5gc,ueransim}
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
source "$root/env/versions.env"
vendor="$root/env/vendor/free5gc-helm"

if [ ! -d "$vendor" ]; then
  git clone -q --depth 1 --branch "$FREE5GC_HELM_VERSION" https://github.com/free5gc/free5gc-helm.git "$vendor"
fi
actual=$(git -C "$vendor" describe --tags --exact-match 2>/dev/null || echo unknown)
[ "$actual" = "$FREE5GC_HELM_VERSION" ] || { echo "vendor is $actual, want $FREE5GC_HELM_VERSION" >&2; exit 1; }

out="$here/build"
rm -rf "$out"; mkdir -p "$out"
cp -r "$vendor/charts" "$out/"

# chart README 的「Enable the single UPF feature」：SMF 的 values 與 configmap 都要換成 single-upf 版本，
# 只改 global.userPlaneArchitecture 不夠（SMF configmap 寫死 ULCL 三個 UPF）。
smf="$out/charts/free5gc/charts/free5gc-smf"
cp "$smf/single-upf-values.yaml" "$smf/values.yaml"
cp "$smf/smf-configmap-single-upf.yaml" "$smf/templates/smf-configmap.yaml"

# chart 的 single-upf 設定把唯一的 UPF 標成 DNAI "mec"（dnnUpfInfoList.dnaiList）。subscriber 有 flow rule 時，
# PCF 下來的 PCC rule 沒有 DNAI，SMF 找不到符合的 UPF（"Can't find UPF with DNN[internet] ... DNAI[]"），
# 改送 PDI 為空、precedence 0 的 PDR，結果 uplink 全被 UPF 丟掉：UE 有 IP 但 ping 不通。
# ULCL 版只有 BranchingUPF 有 dnaiList，Anchor UPF 沒有，所以 ULCL 不會出事。這裡把 UPF 那層的 dnaiList 拿掉。
for f in "$smf/values.yaml" "$smf/templates/smf-configmap.yaml"; do
  python3 -I - "$f" <<'EOF'
import re, sys
p = sys.argv[1]; s = open(p).read()
# 只刪 dnnUpfInfoList 底下（縮排 >= 18）的 dnaiList，保留 SMF 層 dnnInfos 的 dnaiList
new, n = re.subn(r"\n( {18,})dnaiList:\n\1  - mec[^\n]*", "", s)
if n != 1: sys.exit(f"{p}: expected 1 UPF dnaiList, removed {n}")
open(p, "w").write(new)
EOF
done

echo "chart built at $out/charts (free5gc-helm $actual, single UPF)"
