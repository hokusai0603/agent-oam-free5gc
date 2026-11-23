#!/usr/bin/env bash
# 基本套件、固定 kernel、關掉會在 trial 之間改變狀態的自動更新。
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

timedatectl set-timezone Asia/Taipei

# 自動更新會讓不同 trial 的環境不一樣，全部關掉。
systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer || true
apt-get remove -y unattended-upgrades || true

apt-get update
apt-get install -y \
  build-essential git curl wget jq net-tools iproute2 iputils-ping \
  tcpdump iperf3 dnsutils conntrack ethtool \
  "linux-headers-$(uname -r)" "linux-modules-extra-$(uname -r)"

# 固定 kernel：gtp5g 是對這個 kernel 編的，換 kernel 就會失效。
apt-mark hold "linux-image-$(uname -r)" "linux-headers-$(uname -r)" "linux-modules-extra-$(uname -r)" \
  linux-image-virtual linux-headers-virtual linux-virtual linux-generic 2>/dev/null || true

# AMF 的 NGAP 走 SCTP。
modprobe sctp
echo sctp > /etc/modules-load.d/sctp.conf

# UPF 需要轉送封包。
cat > /etc/sysctl.d/90-oam.conf <<'EOF'
net.ipv4.ip_forward = 1
EOF
sysctl --system >/dev/null
