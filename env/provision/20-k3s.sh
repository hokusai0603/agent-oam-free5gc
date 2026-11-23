#!/usr/bin/env bash
# 單節點 K3s server。kubeconfig 開給 vagrant 使用者，agent 不需要 sudo 就能用 kubectl。
set -euxo pipefail

iface=$(ip -o -4 addr show | awk -v ip="${NODE_IP}" '$4 ~ "^"ip"/" {print $2}')
test -n "$iface"

# docker.io 走 host 上的 pull-through cache（scripts/registry-cache.sh）。cache 掛掉時 containerd 會退回直接連 Docker Hub。
if [ -n "${REGISTRY_MIRROR:-}" ]; then
  install -d /etc/rancher/k3s
  cat > /etc/rancher/k3s/registries.yaml <<EOF
# Docker Hub pull-through cache on the host (env/scripts/registry-cache.sh). containerd falls back to Docker Hub if it is down.
mirrors:
  docker.io:
    endpoint:
      - "${REGISTRY_MIRROR}"
EOF
fi

curl -sfL https://get.k3s.io | \
  INSTALL_K3S_VERSION="${K3S_VERSION}" \
  INSTALL_K3S_EXEC="server --node-ip ${NODE_IP} --flannel-iface ${iface} --write-kubeconfig-mode 644 --disable traefik" \
  sh -

install -d -o vagrant -g vagrant /home/vagrant/.kube
cp /etc/rancher/k3s/k3s.yaml /home/vagrant/.kube/config
chown vagrant:vagrant /home/vagrant/.kube/config
chmod 600 /home/vagrant/.kube/config
grep -q KUBECONFIG /home/vagrant/.bashrc || echo 'export KUBECONFIG=$HOME/.kube/config' >> /home/vagrant/.bashrc

# 等 node 與系統 Pod 就緒。
for i in $(seq 1 60); do
  kubectl get nodes 2>/dev/null | grep -q ' Ready' && break
  sleep 5
done
kubectl wait --for=condition=Ready node --all --timeout=300s
kubectl -n kube-system wait --for=condition=Available deployment --all --timeout=300s
