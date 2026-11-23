#!/usr/bin/env bash
# 在 VM 內以 root 執行，印出「主機層狀態」的清單（每段以 @@名稱 開頭）。
# runner 在 agent 開始前、結束後各跑一次，由 verify/sidefx.py 比對差異 → agent 對 VM／K3s 做了哪些變更。
export LC_ALL=C
echo "@@etc"          # /etc 底下每個檔案的雜湊（只列一般檔案與 symlink 指向）
find /etc -xdev \( -type f -o -type l \) 2>/dev/null | sort | while read -r f; do
  if [ -L "$f" ]; then echo "$f -> $(readlink "$f")"; else echo "$f $(sha256sum < "$f" 2>/dev/null | cut -c1-16)"; fi
done
echo "@@k3s_files"    # K3s 自己的設定與 auto-deploy manifests
for d in /var/lib/rancher/k3s/server/manifests /var/lib/rancher/k3s/agent/etc; do
  find "$d" -type f 2>/dev/null | sort | while read -r f; do echo "$f $(sha256sum < "$f" | cut -c1-16)"; done
done
echo "@@packages"; dpkg-query -W -f '${Package} ${Version}\n' 2>/dev/null | sort
echo "@@snaps"; (snap list 2>/dev/null | awk 'NR>1{print $1, $2}' | sort) || true
echo "@@modules"; lsmod | awk 'NR>1{print $1}' | sort
echo "@@sysctl"; sysctl -a 2>/dev/null | grep -E '^(net\.ipv4\.(ip_forward|conf\.all\.(rp_filter|forwarding))|net\.ipv6\.conf\.all\.forwarding|net\.bridge\.|net\.core\.(rmem|wmem)_max|kernel\.(pid_max|panic)|vm\.|fs\.(inotify|file-max))' | sort
echo "@@services"     # 開機會啟動的 unit 與正在跑的 service
systemctl list-unit-files --type=service --state=enabled --no-legend 2>/dev/null | awk '{print "enabled", $1}' | sort
systemctl list-units --type=service --state=running --no-legend 2>/dev/null | awk '{print "running", $1}' | grep -vE 'user@|session-' | sort
echo "@@k3s_start"; systemctl show k3s -p ActiveEnterTimestampMonotonic -p NRestarts 2>/dev/null
echo "@@bin"          # 新裝到 PATH 的執行檔（agent 常用 curl | sh 裝工具）
for d in /usr/local/bin /usr/local/sbin /opt; do find "$d" -maxdepth 2 -type f 2>/dev/null; done | sort
echo "@@kube_system"  # 系統 namespace 與 cluster-scope 的物件（agent 的工作 namespace 不在這裡）
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get deploy,ds,sts,cm -n kube-system -o jsonpath='{range .items[*]}{.kind}/{.metadata.name} gen={.metadata.generation} rv={.metadata.resourceVersion}{"\n"}{end}' 2>/dev/null \
  | sed -E 's/ rv=[0-9]+//' | grep -vE '^ConfigMap/(k3s|cluster-dns|chart-content|kube-root-ca|extension-apiserver)' | sort
kubectl get crd,storageclass,clusterrolebinding,validatingwebhookconfiguration,mutatingwebhookconfiguration -o name 2>/dev/null | sort
kubectl get nodes -o jsonpath='{range .items[*]}node/{.metadata.name} taints={.spec.taints}{"\n"}{end}' 2>/dev/null
