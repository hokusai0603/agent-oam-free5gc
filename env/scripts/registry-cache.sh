#!/usr/bin/env bash
# Docker Hub pull-through cache（給 VM 內的 K3s 用）
#   env/scripts/registry-cache.sh start|stop|status|catalog
# 只綁在 host-only 網卡 192.168.57.1（VM 連得到，實驗室其他機器連不到）。
# VM 端在 /etc/rancher/k3s/registries.yaml 把 docker.io 指向這裡；快取掛掉時 containerd 會退回直接連 Docker Hub。
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
NAME=oam-registry-cache
IMAGE=registry:2.8.3
BIND=192.168.57.1:5050
DATA=$root/env/registry-cache        # git 不追蹤
d() { sg docker -c "$*"; }
case "${1:-status}" in
  start)
    mkdir -p "$DATA"
    if d "docker ps -a --format '{{.Names}}'" | grep -qx $NAME; then d "docker start $NAME" >/dev/null
    else
      d "docker run -d --name $NAME --restart unless-stopped -p $BIND:5000 \
        -v $DATA:/var/lib/registry \
        -e REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io \
        -e REGISTRY_STORAGE_DELETE_ENABLED=true \
        $IMAGE" >/dev/null
    fi
    sleep 2; curl -fsS "http://$BIND/v2/" >/dev/null && echo "cache up at http://$BIND" ;;
  stop) d "docker stop $NAME" ;;
  status) d "docker ps -a --filter name=$NAME --format '{{.Names}} {{.Status}} {{.Ports}}'"; du -sh "$DATA" 2>/dev/null || true ;;
  catalog) curl -fsS "http://$BIND/v2/_catalog?n=200"; echo ;;
  *) echo "usage: $0 start|stop|status|catalog" >&2; exit 1 ;;
esac
