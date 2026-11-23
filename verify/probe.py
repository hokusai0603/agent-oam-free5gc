#!/usr/bin/env python3
"""在 VM 內執行的驗證探針（由 verify.sh 經 vagrant ssh 送進 VM）。

不依賴 agent 的命名方式：掃描所有非系統 namespace 的 Pod 以及 VM 本機，
有 uesimtun0 介面的就是 UE；有 nr-cli 的地方就查 UERANSIM 的狀態。

argv[1]：task spec（JSON 字串）。stdout：一行 JSON 結果。
"""
import json
import re
import subprocess
import sys
import time

SYSTEM_NS = {"kube-system", "kube-public", "kube-node-lease", "default"}

# 在每個 Pod（以及 VM 本機）執行的 shell 探針：輸出以 @@段落 分隔
SH_PROBE = r'''
echo "@@tun"; (ip -o -4 addr show uesimtun0 2>/dev/null || true)
echo "@@gtp"; (ip -o link show 2>/dev/null | grep -o "upfgtp[^:]*" || true)
CLI=$(command -v nr-cli 2>/dev/null || true)
if [ -z "$CLI" ]; then
  for c in /ueransim/nr-cli /UERANSIM/build/nr-cli /opt/UERANSIM/build/nr-cli /usr/local/bin/nr-cli \
           /root/UERANSIM/build/nr-cli /home/vagrant/UERANSIM/build/nr-cli; do
    if [ -x "$c" ]; then CLI="$c"; break; fi
  done
fi
echo "@@cli"; echo "$CLI"
if [ -n "$CLI" ]; then
  for n in $("$CLI" --dump 2>/dev/null); do
    echo "@@node $n"
    "$CLI" "$n" -e status 2>&1
    case "$n" in imsi-*) echo "@@ps $n"; "$CLI" "$n" -e ps-list 2>&1 ;; esac
  done
fi
'''


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def kubectl_json(*args):
    rc, out, err = run(["kubectl", *args, "-o", "json"])
    if rc != 0:
        return None
    return json.loads(out)


def parse_probe(text):
    """把 SH_PROBE 的輸出切成段落。"""
    res = {"tun": "", "gtp": "", "cli": "", "nodes": {}, "ps": {}}
    cur, key = None, None
    for line in text.splitlines():
        if line.startswith("@@"):
            parts = line[2:].split(" ", 1)
            cur = parts[0]
            key = parts[1] if len(parts) > 1 else None
            if cur == "node":
                res["nodes"][key] = ""
            elif cur == "ps":
                res["ps"][key] = ""
            continue
        if cur in ("tun", "gtp", "cli"):
            res[cur] += line + "\n"
        elif cur == "node":
            res["nodes"][key] += line + "\n"
        elif cur == "ps":
            res["ps"][key] += line + "\n"
    for k in ("tun", "gtp", "cli"):
        res[k] = res[k].strip()
    return res


def field(text, name):
    m = re.search(r"^\s*%s:\s*(.*)$" % re.escape(name), text, re.M)
    return m.group(1).strip() if m else None


def parse_sessions(ps_text):
    sessions = []
    for block in re.split(r"^PDU Session\d+:\s*$", ps_text, flags=re.M)[1:]:
        sst = field(block, "sst")
        sd = field(block, "sd")
        sessions.append({
            "state": field(block, "state"),
            "apn": field(block, "apn"),
            "sst": int(sst, 16) if sst and sst.startswith("0x") else (int(sst) if sst and sst.isdigit() else sst),
            "sd": sd[2:].lower().zfill(6) if sd and sd.startswith("0x") else sd,
            "address": field(block, "address"),
        })
    return sessions


def ping_cmd(where):
    cmd = ["ping", "-I", "uesimtun0", "-c", "4", "-W", "2", "8.8.8.8"]
    if where["kind"] == "host":
        return ["sudo", *cmd]
    return ["kubectl", "-n", where["ns"], "exec", where["pod"], "-c", where["container"], "--", *cmd]


# ---------- V3x：抓網頁、DNS、第一跳（老師 2026-11-19 的建議）----------
# 所有測試都綁在 uesimtun0／UE IP 上；沒綁的話流量會從 Pod 的 eth0 直接出去，變成假的成功。
# UE 的 image 沒有 curl／dig，所以用 nsenter 進 UE 的網路 namespace，借 VM 的工具來測（不改 agent 的 Pod）。
DNS_SERVER = "8.8.8.8"
WEB_TESTS = [   # (名稱, 每一組候選 URL, 成功條件)；任一個候選成功就算過
    ("http", ["http://connectivitycheck.gstatic.com/generate_204", "http://cp.cloudflare.com/generate_204"],
     lambda code, size: code in (200, 204)),
    ("https", ["https://www.google.com/", "https://www.cloudflare.com/"],
     lambda code, size: 200 <= code < 400 and size > 1000),
    # 下載 1 MB：大封包要能分段穿過 GTP-U（MTU 設錯時小封包 ping 得通、這個會卡住）
    ("download_1mb", ["https://speed.cloudflare.com/__down?bytes=1000000",
                      "http://ipv4.download.thinkbroadband.com/1MB.zip"],
     lambda code, size: code == 200 and size >= 1000000),
]


def ue_netns_prefix(loc, items):
    """回傳在 UE 網路 namespace 裡執行指令的前綴；找不到就回傳 None。"""
    if loc["kind"] == "host":
        return ["sudo"]
    for p in items:
        if p["metadata"]["namespace"] == loc["ns"] and p["metadata"]["name"] == loc["pod"]:
            for c in p["status"].get("containerStatuses", []) or []:
                if c["name"] == loc["container"] and c.get("containerID"):
                    cid = c["containerID"].split("://", 1)[-1]
                    rc, out, _ = run(["sudo", "k3s", "crictl", "inspect", "--output", "go-template",
                                      "--template", "{{.info.pid}}", cid], timeout=20)
                    if rc == 0 and out.strip().isdigit():
                        return ["sudo", "nsenter", "-t", out.strip(), "-n"]
    return None


def resolve(prefix, name, bind_ip):
    """經指定的網路（bind_ip=None 表示 VM 本身）向 8.8.8.8 查 A 記錄。"""
    cmd = [*prefix, "dig", "@" + DNS_SERVER, name, "A", "+short", "+time=3", "+tries=2"]
    if bind_ip:
        cmd += ["-b", bind_ip]
    rc, out, _ = run(cmd, timeout=20)
    ips = [l for l in out.split() if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", l)]
    return ips[0] if ips else None


def fetch(prefix, url, ip, iface):
    host = re.match(r"https?://([^/:]+)", url).group(1)
    port = "443" if url.startswith("https") else "80"
    cmd = [*prefix, "curl", "-sS", "-o", "/dev/null", "-m", "30",
           "-w", "%{http_code} %{size_download} %{local_ip} %{time_total}", url]
    if ip:
        cmd += ["--resolve", f"{host}:{port}:{ip}"]
    if iface:
        cmd += ["--interface", iface]
    rc, out, err = run(cmd, timeout=40)
    parts = out.split()
    if len(parts) == 4 and parts[0].isdigit():
        return {"url": url, "code": int(parts[0]), "size": int(float(parts[1])), "local_ip": parts[2],
                "time_s": float(parts[3]), "error": err.strip()[:200] or None}
    return {"url": url, "code": 0, "size": 0, "local_ip": None, "time_s": None, "error": (err or out).strip()[:200]}


def run_v3x(loc, items, upf_ips):
    """對一個 UE 做 V3x；同樣的測試也從 VM 本身做一次當對照（對照失敗 → 該項無法判定）。"""
    res = {"where": loc.get("pod", "vm-host"), "checks": {}}
    prefix = ue_netns_prefix(loc, items)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", loc.get("tun", ""))
    ue_ip = m.group(1) if m else None
    if not prefix or not ue_ip:
        res["error"] = "cannot enter UE network namespace" if not prefix else "no UE IP on uesimtun0"
        return res
    res["ue_ip"] = ue_ip
    checks = res["checks"]

    # 第一跳：TTL=1 的封包要由 UPF 回 time exceeded → 證明流量真的從 UPF 出去
    hops = []
    for ttl in (1, 2, 3):
        hop = None
        for _ in range(2):
            rc, out, _ = run([*prefix, "ping", "-I", "uesimtun0", "-c", "1", "-W", "2", "-t", str(ttl), DNS_SERVER], timeout=10)
            mm = re.search(r"From (\d+\.\d+\.\d+\.\d+).*(exceeded)", out, re.I)
            if mm:
                hop = mm.group(1)
                break
        hops.append(hop)
    # 第一跳沒回應 → 無法判定（有些 UPF 不回 ICMP time exceeded）；回應的不是 UPF → 失敗（流量繞過 UPF）
    checks["first_hop_is_upf"] = {"hops": hops, "upf_ips": sorted(upf_ips),
                                  "pass": (hops[0] in upf_ips) if hops[0] else None}
    if not hops[0]:
        checks["first_hop_is_upf"]["note"] = "no ICMP time-exceeded at TTL=1: cannot tell"

    # DNS（經 tunnel，向 8.8.8.8 查）
    names = {re.match(r"https?://([^/:]+)", u).group(1) for _, urls, _ in WEB_TESTS for u in urls}
    ue_dns = {n: resolve(prefix, n, ue_ip) for n in sorted(names)}
    ctl_dns = {n: resolve(["sudo"], n, None) for n in sorted(names)}
    dns_ok = any(ue_dns.values())
    checks["dns"] = {"server": DNS_SERVER, "answers": ue_dns,
                     "pass": True if dns_ok else (None if not any(ctl_dns.values()) else False)}

    # 網頁：經 tunnel 抓；local_ip 必須是 UE IP（確認真的綁在 tunnel 上）
    for name, urls, ok in WEB_TESTS:
        tries, passed, ctl_ok = [], False, False
        for u in urls:
            host = re.match(r"https?://([^/:]+)", u).group(1)
            r = fetch(prefix, u, ue_dns.get(host) or ctl_dns.get(host), "uesimtun0")
            r["bound_to_ue"] = r["local_ip"] == ue_ip
            tries.append(r)
            if ok(r["code"], r["size"]) and r["bound_to_ue"]:
                passed = True
                break
        if not passed:   # 對照：從 VM 本身抓同樣的網址
            for u in urls:
                c = fetch(["sudo"], u, None, None)
                if ok(c["code"], c["size"]):
                    ctl_ok = True
                    break
        checks[name] = {"tries": tries, "pass": True if passed else (False if ctl_ok else None)}
        if not passed and not ctl_ok:
            checks[name]["note"] = "control from VM also failed: external problem, not counted"
    vals = [c["pass"] for c in checks.values()]
    res["pass"] = None if all(v is None for v in vals) else all(v is not False for v in vals)
    return res


# ---------- V4：MEC 路徑（L2，2026-11-23）----------
# 判斷「哪個 UPF 把流量解封裝送出去」：gtp5g 解封裝／封裝的封包會經過 UPF 自己的 upfgtp 介面，
# 只轉送 GTP-U（I-UPF）的 UPF，upfgtp 不會增加、只有 GTP-U 封包。所以分別送 MEC 流量和上網流量，
# 比較每個 UPF 的 upfgtp 位元組和 GTP-U 封包數，不依賴 NAT（agent 不做 MASQUERADE 也能判斷）。
def pod_netns_prefix(pod):
    for c in pod["status"].get("containerStatuses", []) or []:
        if c.get("containerID"):
            cid = c["containerID"].split("://", 1)[-1]
            rc, out, _ = run(["sudo", "k3s", "crictl", "inspect", "--output", "go-template",
                              "--template", "{{.info.pid}}", cid], timeout=20)
            if rc == 0 and out.strip().isdigit():
                return ["sudo", "nsenter", "-t", out.strip(), "-n"]
    return None


def if_bytes(prefix):
    """回傳 upfgtp* 介面的 rx+tx 位元組（gtp5g 解封裝／封裝的封包才會經過）。"""
    rc, out, _ = run([*prefix, "cat", "/proc/net/dev"], timeout=10)
    tot = 0
    for line in out.splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if name.strip().startswith("upfgtp") and len(f) >= 9:
            tot += int(f[0]) + int(f[8])
    return tot


def upf_deltas(upfs, action):
    """執行 action 期間，每個 UPF 的 upfgtp 位元組變化（gtp）和 GTP-U 封包數（n_gtpu，tcpdump 只數 UDP 2152）。
    eth0 的位元組不能用：UPF 有時會連發大量 PFCP heartbeat（實測 V3x 下載時 10 秒 2500 個），會蓋過資料流量。"""
    caps = {n: subprocess.Popen([*px, "tcpdump", "-i", "any", "-nn", "-q", "-l", "udp port 2152"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for n, px in upfs.items()}
    time.sleep(1.5)
    before = {n: if_bytes(px) for n, px in upfs.items()}
    action()
    time.sleep(1)
    after = {n: if_bytes(px) for n, px in upfs.items()}
    counts = {}
    for n, c in caps.items():
        run(["sudo", "pkill", "-INT", "-P", str(c.pid)], timeout=5)
        try:
            out, _ = c.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            c.kill()
            out, _ = c.communicate()
        counts[n] = sum(1 for l in out.splitlines() if "UDP" in l or "udp" in l)
    return {n: {"gtp": after[n] - before[n], "n_gtpu": counts[n]} for n in upfs}


def anchor_of(deltas):
    """upfgtp 變化最大、且佔所有 upfgtp 變化 80% 以上的 UPF；沒有明顯的就回傳 None。"""
    tot = sum(d["gtp"] for d in deltas.values())
    if tot <= 0:
        return None
    n, d = max(deltas.items(), key=lambda kv: kv[1]["gtp"])
    return n if d["gtp"] >= 0.8 * tot else None


def run_v4(loc, items, upf_pods, mec):
    res = {"where": loc.get("pod", "vm-host"), "checks": {}}
    prefix = ue_netns_prefix(loc, items)
    if not prefix:
        res["error"] = "cannot enter UE network namespace"
        return res
    url = f'http://{mec["ip"]}:{mec.get("port", 80)}/'
    upfs = {}
    for p in upf_pods:
        px = pod_netns_prefix(p)
        if px:
            upfs[p["metadata"]["name"]] = px
    ip_of = {p["metadata"]["name"]: p["status"].get("podIP") for p in upf_pods}
    checks = res["checks"]

    # (1) 連得到 MEC；whoami 類的應用會回 RemoteAddr（MEC 看到的來源），只記錄、當旁證
    bodies = []
    def mec_traffic():
        for _ in range(5):
            rc, out, err = run([*prefix, "curl", "-sS", "-m", "5", "--interface", "uesimtun0", "-w", "\n@@code %{http_code}", url], timeout=10)
            bodies.append(out)
    d_mec = upf_deltas(upfs, mec_traffic)
    codes = [int(m.group(1)) for b in bodies for m in [re.search(r"@@code (\d+)", b)] if m]
    ok = sum(1 for c in codes if 200 <= c < 400)
    ra = sorted({m.group(1) for b in bodies for m in [re.search(r"^RemoteAddr:\s*\[?([\d.]+)", b, re.M)] if m})
    checks["mec_reachable"] = {"url": url, "ok": ok, "tries": len(bodies), "codes": codes, "pass": ok >= 3}

    # (2) 上網的流量
    def inet_traffic():
        run([*prefix, "ping", "-I", "uesimtun0", "-c", "10", "-i", "0.2", "-W", "2", "8.8.8.8"], timeout=20)
    d_inet = upf_deltas(upfs, inet_traffic)

    mec_upf, inet_upf = anchor_of(d_mec), anchor_of(d_inet)
    res["deltas"] = {"mec": d_mec, "internet": d_inet}
    res["mec_anchor"], res["internet_anchor"] = mec_upf, inet_upf
    res["mec_remote_addr"] = ra
    # (3) MEC 和上網從不同的 UPF 出去
    checks["separate_anchors"] = {"mec": mec_upf, "internet": inet_upf,
                                  "pass": None if not (mec_upf and inet_upf) else mec_upf != inet_upf}
    # (4) 兩種流量都先經過同一個只轉送的 UPF（I-UPF）：upfgtp 幾乎沒變、有 GTP-U 封包
    def forwarders(d):
        g = max((x["gtp"] for x in d.values()), default=0)
        anchor_pk = max((x["n_gtpu"] for x in d.values() if x["gtp"] == g), default=0)
        return {n for n, x in d.items() if x["gtp"] <= 0.05 * max(g, 1) and x["n_gtpu"] >= 0.5 * max(anchor_pk, 1)}
    br = sorted(forwarders(d_mec) & forwarders(d_inet) - {mec_upf, inet_upf})
    checks["via_branching_upf"] = {"upfs": br, "pass": None if not (mec_upf and inet_upf) else bool(br)}
    # 旁證：MEC 看到的來源是 UPF 的 IP 時，必須是 MEC 的 anchor（anchor 有做 NAT）
    ra_upf = [n for n, ip in ip_of.items() if ip in ra]
    checks["remote_addr_consistent"] = {"remote_addr": ra, "upf": ra_upf,
                                        "pass": None if not ra_upf or not mec_upf else ra_upf == [mec_upf]}
    vals = [c["pass"] for c in checks.values()]
    res["pass"] = False if False in vals else (None if None in [checks["mec_reachable"]["pass"], checks["separate_anchors"]["pass"]] else True)
    return res


def main():
    spec = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    result = {"v1": {}, "v2": {}, "v3": {}, "req": {}, "ue_locations": [], "notes": []}

    # ---------- V1：Pod 狀態 ----------
    pods = kubectl_json("get", "pods", "-A")
    items = [p for p in (pods or {}).get("items", []) if p["metadata"]["namespace"] not in SYSTEM_NS]
    bad, restarts, summary = [], 0, []
    for p in items:
        name = f'{p["metadata"]["namespace"]}/{p["metadata"]["name"]}'
        phase = p["status"].get("phase")
        cs = p["status"].get("containerStatuses", []) or []
        restarts += sum(c.get("restartCount", 0) for c in cs)
        waiting = [c["state"]["waiting"].get("reason") for c in cs if "waiting" in c.get("state", {})]
        ready = all(c.get("ready") for c in cs) if cs else False
        summary.append({"pod": name, "phase": phase, "ready": ready, "waiting": waiting,
                        "images": sorted({c.get("image", "") for c in cs})})
        if phase == "Succeeded":
            continue
        if p["metadata"].get("deletionTimestamp"):
            continue  # 正在終止的 Pod 不算
        if phase != "Running" or not ready:
            bad.append({"pod": name, "phase": phase, "waiting": waiting})
    result["v1"] = {"pass": bool(items) and not bad, "pods": len(items), "not_ready": bad,
                    "total_restarts": restarts}

    # ---------- 掃描 Pod 與 VM 本機 ----------
    probes = []
    for p in items:
        if p["status"].get("phase") != "Running" or p["metadata"].get("deletionTimestamp"):
            continue
        for c in p["spec"].get("containers", []):
            rc, out, err = run(["kubectl", "-n", p["metadata"]["namespace"], "exec", p["metadata"]["name"],
                                "-c", c["name"], "--", "sh", "-c", SH_PROBE], timeout=45)
            if rc == 0 or out:
                probes.append(({"kind": "pod", "ns": p["metadata"]["namespace"], "pod": p["metadata"]["name"],
                                "container": c["name"], "image": c.get("image", "")}, parse_probe(out)))
    rc, out, err = run(["sudo", "sh", "-c", SH_PROBE], timeout=45)
    probes.append(({"kind": "host"}, parse_probe(out)))

    ue_nodes, gnb_nodes, upf_like = [], [], set()
    for where, pr in probes:
        label = where.get("pod", "vm-host")
        if pr["gtp"]:
            upf_like.add(label)
        if pr["tun"]:
            result["ue_locations"].append({**where, "tun": pr["tun"]})
        for node, st in pr["nodes"].items():
            if node.startswith("imsi-"):
                ue_nodes.append({"where": where, "supi": node,
                                 "rm_state": field(st, "rm-state"), "mm_state": field(st, "mm-state"),
                                 "plmn": field(st, "current-plmn"),
                                 "sessions": parse_sessions(pr["ps"].get(node, ""))})
            else:
                gnb_nodes.append({"where": where, "name": node, "ngap_up": field(st, "is-ngap-up")})
    for p in items:
        imgs = " ".join(c.get("image", "") for c in p["spec"].get("containers", []))
        if re.search(r"(^|/|-)upf[:@-]|/upf:|go-upf", imgs, re.I):
            upf_like.add(p["metadata"]["name"])

    # ---------- V2：NG Setup、註冊、PDU session ----------
    registered = [u for u in ue_nodes if u["rm_state"] == "RM-REGISTERED"]
    active = [u for u in registered if any(s["state"] == "PS-ACTIVE" for s in u["sessions"])]
    ngap_up = any(g["ngap_up"] == "true" for g in gnb_nodes)
    result["v2"] = {
        "pass": bool(active) and (ngap_up or not gnb_nodes),
        "gnb_found": len(gnb_nodes), "gnb_ngap_up": ngap_up,
        "ue_found": len(ue_nodes), "ue_registered": len(registered), "ue_with_active_pdu": len(active),
        "ues": [{k: v for k, v in u.items() if k != "where"} | {"where": u["where"].get("pod", "vm-host")}
                for u in ue_nodes],
    }
    if not ue_nodes:
        result["notes"].append("no UERANSIM nr-cli UE found (V2 relies on nr-cli)")

    # ---------- V3：經 uesimtun0 ping ----------
    pings = []
    for loc in result["ue_locations"]:
        rc, out, err = run(ping_cmd(loc), timeout=30)
        m = re.search(r"(\d+) packets transmitted, (\d+) received", out)
        rtt = re.search(r"= [\d.]+/([\d.]+)/", out)
        pings.append({"where": loc.get("pod", "vm-host"), "tx": int(m.group(1)) if m else 0,
                      "rx": int(m.group(2)) if m else 0, "rtt_avg_ms": float(rtt.group(1)) if rtt else None})
    result["v3"] = {"pass": any(p["rx"] >= 3 for p in pings), "pings": pings,
                    "ue_tun_found": len(result["ue_locations"])}

    # V3x：只對 ping 通的 UE 做（ping 都不通就沒必要）
    upf_ips = set()
    for p in items:
        if p["metadata"]["name"] in upf_like and p["status"].get("podIP"):
            upf_ips.add(p["status"]["podIP"])
            for ip in p["status"].get("podIPs", []) or []:
                upf_ips.add(ip.get("ip"))
    if "vm-host" in upf_like:   # UPF 直接跑在 VM 上
        rc, out, _ = run(["hostname", "-I"])
        upf_ips.update(out.split())
    v3x_list = []
    for loc, pg in zip(result["ue_locations"], pings):
        if pg["rx"] >= 1:
            v3x_list.append(run_v3x(loc, items, upf_ips))
    if v3x_list:
        oks = [x.get("pass") for x in v3x_list]
        v3x_pass = True if any(o is True for o in oks) else (None if all(o is None for o in oks) else False)
    else:
        v3x_pass = False
    result["v3x"] = {"pass": v3x_pass, "ues": v3x_list}

    # V4：MEC 路徑（spec 有 expect.mec 才做）
    if spec.get("expect", {}).get("mec"):
        upf_pods = [p for p in items if p["metadata"]["name"] in upf_like]
        v4_list = [run_v4(loc, items, upf_pods, spec["expect"]["mec"])
                   for loc, pg in zip(result["ue_locations"], pings) if pg["rx"] >= 1]
        oks = [x.get("pass") for x in v4_list]
        result["v4"] = {"pass": True if True in oks else (False if not oks or False in oks else None), "ues": v4_list}

    # ---------- 需求符合度 ----------
    exp = spec.get("expect", {})
    checks = {}
    if "upf_count" in exp:
        checks["upf_count"] = {"expect": exp["upf_count"], "actual": len(upf_like), "where": sorted(upf_like),
                               "pass": len(upf_like) == exp["upf_count"]}
    target = [u for u in active if not exp.get("supi") or u["supi"] == exp["supi"]]
    if "supi" in exp:
        checks["supi"] = {"expect": exp["supi"], "actual": [u["supi"] for u in ue_nodes], "pass": bool(target)}
    t = target[0] if target else None
    sess = next((s for s in (t["sessions"] if t else []) if s["state"] == "PS-ACTIVE"), None)
    if "plmn" in exp:
        checks["plmn"] = {"expect": exp["plmn"], "actual": t and t["plmn"], "pass": bool(t and t["plmn"] == exp["plmn"])}
    if "dnn" in exp:
        checks["dnn"] = {"expect": exp["dnn"], "actual": sess and sess["apn"], "pass": bool(sess and sess["apn"] == exp["dnn"])}
    if "sst" in exp:
        checks["slice"] = {"expect": {"sst": exp["sst"], "sd": exp.get("sd")},
                           "actual": sess and {"sst": sess["sst"], "sd": sess["sd"]},
                           "pass": bool(sess and sess["sst"] == exp["sst"] and (exp.get("sd") is None or sess["sd"] == exp["sd"]))}
    if "gnb_count" in exp:
        checks["gnb_count"] = {"expect": exp["gnb_count"], "actual": len(gnb_nodes), "pass": len(gnb_nodes) == exp["gnb_count"]}
    if "ue_count" in exp:
        checks["ue_count"] = {"expect": exp["ue_count"], "actual": len(ue_nodes), "pass": len(ue_nodes) == exp["ue_count"]}
    if "free5gc_version" in exp:
        # 核網 NF 的 image tag 都要是指定版本（UERANSIM 的 image 另有版號，不檢查）
        nf_re = re.compile(r"(?:^|/)free5gc/(amf|ausf|bsf|chf|nef|nrf|nssf|pcf|scp|smf|udm|udr|upf|webui|webconsole)(?::([^@\s]+))?")
        tags = {}
        for s in summary:
            for img in s["images"]:
                m = nf_re.search(img)
                if m:
                    tags.setdefault(m.group(1), set()).add(m.group(2) or "latest")
        actual = {k: sorted(v) for k, v in sorted(tags.items())}
        checks["free5gc_version"] = {"expect": exp["free5gc_version"], "actual": actual,
                                     "pass": bool(tags) and all(v == {exp["free5gc_version"]} for v in tags.values())}
    result["req"] = {"pass": all(c["pass"] for c in checks.values()) if checks else None, "checks": checks}

    result["pass"] = all(result[k]["pass"] for k in ("v1", "v2", "v3")) and result["req"]["pass"] is not False
    if "v4" in result:   # L2：MEC 路徑是完成條件，算進 PASS（無法判定 → 不通過）
        result["pass"] = result["pass"] and result["v4"]["pass"] is True
    # 嚴格版：再加上 V3x（抓網頁、DNS、第一跳是 UPF）。V3x 無法判定（外網問題）時是 None
    result["pass_strict"] = (None if result["v3x"]["pass"] is None
                             else result["pass"] and result["v3x"]["pass"])
    result["pods"] = summary
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
