#!/usr/bin/env bash
# 把實際裝上的版本寫到 /etc/oam-env-versions，host 端會抓回來存檔。
set -uo pipefail
out=/etc/oam-env-versions
{
  echo "recorded_at=$(date -Iseconds)"
  echo "hostname=$(hostname)"
  echo "os=$(. /etc/os-release; echo "$PRETTY_NAME")"
  echo "kernel=$(uname -r)"
  echo "gtp5g=${GTP5G_VERSION} loaded=$(lsmod | grep -c '^gtp5g')"
  echo "sctp_loaded=$(lsmod | grep -c '^sctp')"
  command -v k3s  >/dev/null && echo "k3s=$(k3s --version | head -1)"
  command -v helm >/dev/null && echo "helm=$(helm version --short)"
  echo "cpus=$(nproc) mem_mb=$(free -m | awk '/Mem:/{print $2}')"
} > "$out"
cat "$out"
