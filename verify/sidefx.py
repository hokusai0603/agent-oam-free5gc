#!/usr/bin/env python3
"""比對 agent 開始前／結束後的主機層狀態（verify/hoststate.sh 的輸出），列出 agent 對 VM／K3s 的變更。

    verify/sidefx.py before.txt after.txt     → stdout 一行 JSON

只記錄，不影響 PASS／FAIL。分類：
  infra   ：K3s 設定、系統 service、kernel module、sysctl、kube-system／cluster 物件、node taint → 要人工看是否必要
  tooling ：裝套件、裝執行檔（tcpdump、jq…）→ 通常無害，但也是變更
  auto    ：建 iptables／ipset 規則時 kernel 自動載入的 netfilter module（xt_*、ip_set*…）。
            部署 free5GC 本身就會出現（SCTP Service → xt_sctp、UPF → xt_TCPMSS、kube-router → xt_set），
            另外列出、不算 infra（2026-11-19 phase1-r2 發現）。
            唯讀查詢自動載入的 *_diag（`ss` 會載入 inet_diag／tcp_diag／udp_diag…）也算 auto（2026-11-24 L2 Haiku 重跑發現）
"""
import json
import re
import sys

# 正常運作就會變的東西，不算 agent 的變更
NOISE = [
    r"^/etc/(ld\.so\.cache|mtab|resolv\.conf|machine-id|\.pwd\.lock|subuid-|subgid-|gshadow-|shadow-|passwd-|group-)\b",
    r"^/etc/systemd/system/.*\.wants/(snap-|snapd)",
    r"^/var/lib/rancher/k3s/agent/etc/(containerd/config\.toml|cni/|flannel/|k3s-agent-load-balancer\.json|k3s-api-server-agent-load-balancer\.json)",
]
noise_re = re.compile("|".join(NOISE))
# 依需要自動起停的系統服務（不是 agent 啟動的）
# kernel 在建 iptables／ipset／nft 規則時自動載入的 module
auto_mod_re = re.compile(r"^(xt_|ipt_|ip6t_|ip_set|nf_|nft_|ip_vs|ebt_)|^(inet|tcp|udp|raw|unix|netlink|af_packet|sock)_diag$")
svc_noise_re = re.compile(r"^running (systemd-(timedated|hostnamed|localed|timesyncd)|fwupd|packagekit|apt-daily|"
                          r"apt-daily-upgrade|motd-news|man-db|unattended-upgrades|polkit|udisks2|e2scrub)")


def parse(path):
    sec, cur = {}, None
    for line in open(path, errors="replace"):
        line = line.rstrip("\n")
        if line.startswith("@@"):
            cur = line[2:].strip()
            sec.setdefault(cur, [])
        elif cur and line.strip():
            sec[cur].append(line)
    return sec


def keyed(lines, sep=" "):
    """'path hash' → {path: hash}；沒有值的行 → {line: ''}"""
    d = {}
    for l in lines:
        k, _, v = l.partition(sep)
        d[k] = v
    return d


def diff_keyed(a, b):
    a, b = keyed(a), keyed(b)
    added = sorted(k for k in b if k not in a and not noise_re.search(k))
    removed = sorted(k for k in a if k not in b and not noise_re.search(k))
    changed = sorted(k for k in a if k in b and a[k] != b[k] and not noise_re.search(k))
    return {"added": added, "removed": removed, "changed": changed}


def diff_set(a, b):
    a = {x for x in a if not svc_noise_re.search(x)}
    b = {x for x in b if not svc_noise_re.search(x)}
    return {"added": sorted(b - a), "removed": sorted(a - b)}


def main():
    A, B = parse(sys.argv[1]), parse(sys.argv[2])
    g = lambda s, k: s.get(k, [])
    r = {
        "etc": diff_keyed(g(A, "etc"), g(B, "etc")),
        "k3s_files": diff_keyed(g(A, "k3s_files"), g(B, "k3s_files")),
        "packages": diff_keyed(g(A, "packages"), g(B, "packages")),
        "snaps": diff_keyed(g(A, "snaps"), g(B, "snaps")),
        "modules": diff_set(g(A, "modules"), g(B, "modules")),
        "sysctl": diff_keyed(g(A, "sysctl"), g(B, "sysctl"), ),
        "services": diff_set(g(A, "services"), g(B, "services")),
        "bin": diff_set(g(A, "bin"), g(B, "bin")),
        "kube_system": diff_keyed(g(A, "kube_system"), g(B, "kube_system")),
    }
    # sysctl 的格式是 "key = value"
    r["sysctl"] = diff_keyed([l.replace(" = ", " ", 1) for l in g(A, "sysctl")],
                             [l.replace(" = ", " ", 1) for l in g(B, "sysctl")])
    ka, kb = keyed(g(A, "k3s_start"), "="), keyed(g(B, "k3s_start"), "=")
    r["k3s_restarted"] = bool(ka and kb and ka.get("ActiveEnterTimestampMonotonic") != kb.get("ActiveEnterTimestampMonotonic"))

    def n(d):
        return sum(len(v) for v in d.values())
    infra = []
    for k in ("etc", "k3s_files", "sysctl", "kube_system"):
        for kind, items in r[k].items():
            infra += [f"{k}:{kind}:{i}" for i in items]
    auto = [f"modules:{kind}:{i}" for kind, items in r["modules"].items() for i in items if auto_mod_re.search(i)]
    infra += [f"modules:{kind}:{i}" for kind, items in r["modules"].items() for i in items if not auto_mod_re.search(i)]
    infra += [f"services:{kind}:{i}" for kind, items in r["services"].items() for i in items]
    if r["k3s_restarted"]:
        infra.append("k3s:restarted")
    tooling = [f"packages:{kind}:{i}" for kind, items in r["packages"].items() for i in items]
    tooling += [f"snaps:{kind}:{i}" for kind, items in r["snaps"].items() for i in items]
    tooling += [f"bin:{kind}:{i}" for kind, items in r["bin"].items() for i in items]
    r["summary"] = {"infra_changes": len(infra), "tooling_changes": len(tooling),
                    "auto_changes": len(auto),
                    "infra": infra[:200], "tooling": tooling[:200], "auto": auto[:200]}
    print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
