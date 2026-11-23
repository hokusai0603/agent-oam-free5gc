#!/usr/bin/env bash
# 編譯並安裝 gtp5g（free5GC UPF 需要的 kernel 模組），開機自動載入。
set -euxo pipefail

if lsmod | grep -q '^gtp5g'; then
  echo "gtp5g already loaded"; exit 0
fi

src=/usr/local/src/gtp5g
rm -rf "$src"
git clone --depth 1 --branch "${GTP5G_VERSION}" https://github.com/free5gc/gtp5g.git "$src"
make -C "$src" -j"$(nproc)"
make -C "$src" install
modprobe gtp5g
echo gtp5g > /etc/modules-load.d/gtp5g.conf
lsmod | grep '^gtp5g'
