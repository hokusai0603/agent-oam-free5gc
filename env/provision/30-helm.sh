#!/usr/bin/env bash
set -euxo pipefail
tmp=$(mktemp -d)
curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" -o "$tmp/helm.tgz"
tar -xzf "$tmp/helm.tgz" -C "$tmp"
install -m 755 "$tmp/linux-amd64/helm" /usr/local/bin/helm
rm -rf "$tmp"
helm version --short
