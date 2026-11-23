#!/usr/bin/env python3
"""V6：NF 之間的 dataflow 與封包內容（host 端分析）。

    verify/dataflow.py <dir> tasks/L1/spec.json   → stdout 一行 JSON，stderr 人看的摘要
    <dir> 是 dataflow.sh 拿回來的：capture.pcap、pods.json、svc.json、meta.json（、subs.json）

用 tshark 解碼 NGAP／NAS-5GS（NEA0 不加密，可直接讀）、SBI（HTTP/2 明文）、PFCP、GTP-U，然後做三類判定：

  flow    ：註冊與 PDU session 建立的訊息有沒有照順序出現、由對的 NF 發給對的 NF、回應成功
            （依 TS 23.502 4.2.2.2.2、4.3.2.2.1；以 free5GC v4.3.0 的實作為準，見 verify/README.md）
  content ：訊息內容符合規格、而且前後一致
            PLMN／TAC／S-NSSAI（NGSetup、Allowed NSSAI）、DNN、UE IP；
            PFCP 規則完整（沒有空的 PDI → 參考答案坑 7）；
            NGAP、PFCP、GTP-U 三處的 TEID／IP 互相對得上
  sbi     ：觀察期間所有 SBI 呼叫都有回應，沒有 4xx／5xx
狀態同 V5：ok／error／extra／warn／info／skip；pass = 沒有 error，exact = 再加上沒有 extra。
"""
import collections
import ipaddress
import json
import os
import re
import subprocess
import sys

# ---------- tshark JSON（保留重複的 key）----------


class O(list):
    """tshark -T json 的物件；同一層可能有重複的 key（例如多個 Create PDR），所以存成 (key, value) 的 list"""


def _hook(pairs):
    return O(pairs)


def items(o):
    if isinstance(o, O):
        return o
    return []


def find(o, pred):
    """遞迴找出所有 key 符合 pred 的 (key, value)"""
    if isinstance(o, O):
        for k, v in o:
            if pred(k):
                yield k, v
            yield from find(v, pred)
    elif isinstance(o, list):
        for x in o:
            yield from find(x, pred)


def vals(o, key):
    return [v for _, v in find(o, lambda k: k == key) if not isinstance(v, (O, list))]


def first(o, key, default=None):
    v = vals(o, key)
    return v[0] if v else default


def subtrees(o, prefix):
    return [v for k, v in find(o, lambda k: k.startswith(prefix)) if isinstance(v, (O, list))]


def as_list(x):
    """O 本身也是 list，不能用 isinstance(x, list) 判斷是不是 JSON 陣列"""
    if isinstance(x, O):
        return [x]
    return x if isinstance(x, list) else [x]


def get(o, key):
    for k, v in items(o):
        if k == key:
            return v
    return None


def getall(o, key):
    return [v for k, v in items(o) if k == key]


# ---------- 名稱 ----------

NGAP_PROC = {21: "NGSetup", 15: "InitialUEMessage", 46: "UplinkNASTransport", 4: "DownlinkNASTransport",
             14: "InitialContextSetup", 29: "PDUSessionResourceSetup", 41: "UEContextRelease",
             42: "UEContextReleaseRequest", 26: "PDUSessionResourceModify", 28: "PDUSessionResourceRelease",
             40: "UEContextModification", 24: "Paging", 9: "ErrorIndication", 20: "NGReset"}
NGAP_PDU = {"0": "Request", "1": "Response", "2": "Failure"}
NAS_MM = {0x41: "RegistrationRequest", 0x42: "RegistrationAccept", 0x43: "RegistrationComplete",
          0x44: "RegistrationReject", 0x45: "DeregistrationRequest(UE)", 0x46: "DeregistrationAccept(UE)",
          0x47: "DeregistrationRequest(NW)", 0x48: "DeregistrationAccept(NW)", 0x4c: "ServiceRequest",
          0x4d: "ServiceReject", 0x4e: "ServiceAccept", 0x54: "ConfigurationUpdateCommand",
          0x56: "AuthenticationRequest", 0x57: "AuthenticationResponse", 0x58: "AuthenticationReject",
          0x59: "AuthenticationFailure", 0x5d: "SecurityModeCommand", 0x5e: "SecurityModeComplete",
          0x5f: "SecurityModeReject", 0x64: "5GMMStatus", 0x67: "ULNASTransport", 0x68: "DLNASTransport"}
NAS_SM = {0xc1: "PDUSessionEstablishmentRequest", 0xc2: "PDUSessionEstablishmentAccept",
          0xc3: "PDUSessionEstablishmentReject", 0xd1: "PDUSessionReleaseRequest", 0xd3: "PDUSessionReleaseCommand",
          0xd6: "5GSMStatus"}
PFCP = {1: "HeartbeatRequest", 2: "HeartbeatResponse", 5: "AssociationSetupRequest", 6: "AssociationSetupResponse",
        50: "SessionEstablishmentRequest", 51: "SessionEstablishmentResponse", 52: "SessionModificationRequest",
        53: "SessionModificationResponse", 54: "SessionDeletionRequest", 55: "SessionDeletionResponse",
        56: "SessionReportRequest", 57: "SessionReportResponse"}
NF_RE = re.compile(r"(amf|smf|upf|ausf|udm|udr|pcf|nrf|nssf|chf|nef|bsf|scp|webui|n3iwf|tngf|mongo)", re.I)


def intx(v):
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        try:
            return int(str(v))
        except (TypeError, ValueError):
            return None


def hexbytes_ip(v):
    """ngap.transportLayerAddress = '0a:2a:00:1b' → 10.42.0.27"""
    try:
        b = bytes(int(x, 16) for x in str(v).split(":"))
        return str(ipaddress.ip_address(b[:4])) if len(b) >= 4 else None
    except ValueError:
        return None


def hexbytes_int(v):
    try:
        return int(str(v).replace(":", ""), 16)
    except ValueError:
        return None


def sd_norm(v):
    """各種寫法的 SD → 6 位 16 進位字串：'01:02:03'、'66051'、'0x010203'、'010203'"""
    if v is None:
        return None
    s = str(v)
    if ":" in s:
        return f"{int(s.replace(':', ''), 16):06x}"
    if s.startswith("0x"):
        return f"{int(s, 16):06x}"
    if s.isdigit():
        return f"{int(s):06x}"
    return s.lower()


# ---------- 讀資料 ----------

def ip_map(d, meta):
    """IP → {nf, pod}"""
    m = {}
    pods = []
    for fn in ("pods.json", "pods_after.json"):   # 重啟後 UE／gNB 可能換了 Pod 與 IP，兩份都讀
        try:
            pods += json.load(open(os.path.join(d, fn)))["items"]
        except (FileNotFoundError, ValueError, KeyError):
            pass
    ue_pods = {meta.get("ue", {}).get("pod"), meta.get("ue", {}).get("pod_before")} - {None, ""}
    gnb_pods = {meta.get("gnb", {}).get("pod"), meta.get("gnb", {}).get("pod_before")} - {None, ""}
    ue_pod = meta.get("ue", {}).get("pod")
    for p in pods:
        ip = p.get("status", {}).get("podIP")
        if not ip or p.get("spec", {}).get("hostNetwork"):
            continue
        name = p["metadata"]["name"]
        labels = p["metadata"].get("labels", {}) or {}
        img = " ".join(c.get("image", "") for c in p["spec"].get("containers", []))
        if name in ue_pods:
            nf = "ue"
        elif name in gnb_pods:
            nf = "gnb"
        elif labels.get("nf"):
            nf = labels["nf"].lower()
        else:
            mm = NF_RE.search(name) or NF_RE.search(img)
            nf = mm.group(1).lower() if mm else name
        label = nf
        if "upf" in nf and nf != "upf":   # chart 的 label 是 iupf1／psaupf1…：角色一律當 upf，名稱另外保留（多 UPF 用）
            nf = "upf"
        m[ip] = {"nf": "mongodb" if nf == "mongo" else nf, "pod": name, "name": label}
    for k in (meta.get("ip_phase1"), meta.get("ue", {}).get("ip_after"), meta.get("ue", {}).get("ip_before")):
        if k:
            m.setdefault(k, {"nf": "ue(tun)", "pod": ue_pod})
    return m


def sbi_ports(d):
    ports = set()
    try:
        for p in json.load(open(os.path.join(d, "pods.json")))["items"]:
            for c in p["spec"].get("containers", []):
                for cp in c.get("ports", []) or []:
                    if cp.get("protocol", "TCP") == "TCP":
                        ports.add(cp["containerPort"])
    except (FileNotFoundError, ValueError, KeyError):
        pass
    try:
        for s in json.load(open(os.path.join(d, "svc.json")))["items"]:
            for sp in s["spec"].get("ports", []) or []:
                if sp.get("protocol", "TCP") == "TCP":
                    tp = sp.get("targetPort", sp.get("port"))
                    if isinstance(tp, int):
                        ports.add(tp)
                    ports.add(sp.get("port"))
    except (FileNotFoundError, ValueError, KeyError):
        pass
    ports.discard(None)
    ports -= {27017, 5000, 443, 53, 9153, 6443, 10250}
    return sorted(p for p in ports if isinstance(p, int)) or [8000, 8080]


def dedup(pcap, iface="cni0"):
    """cni0 上有時同一個封包會出現兩次（實測：某些 Pod 的流量），tshark 會把重複的那份當成新的 TCP 連線，
    請求和回應就配不起來。先用 editcap -D 去掉相鄰的重複封包。回傳 (要分析的檔案, 去掉幾個)"""
    out = pcap + ".dedup"
    # 兩份的差別只在乙太網路標頭的來源 MAC（一份是 Pod 的、一份是 cni0 的：經主機轉送了一次），
    # 所以比對時略過連結層標頭：Ethernet 14 bytes；any 介面是 Linux cooked v2，20 bytes
    skip = "14" if iface != "any" else "20"
    r = subprocess.run(["editcap", "-D", "20", "-I", skip, pcap, out], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        return pcap, None
    m = re.search(r"(\d+) packets? seen, (\d+) packets? skipped", r.stdout + r.stderr)
    return out, int(m.group(2)) if m else None


def tshark(pcap, ports):
    cmd = ["tshark", "-r", pcap, "-o", "nas-5gs.null_decipher:TRUE"]
    for p in ports:
        cmd += ["-d", f"tcp.port=={p},http2"]
    cmd += ["-Y", "ngap || pfcp || http2.type==1 || gtp", "-T", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError(r.stderr[:500])
    return json.loads(r.stdout or "[]", object_pairs_hook=_hook)


# ---------- 把封包轉成事件 ----------

def events_from(frames, ipm):
    ev = []
    nf = lambda ip: ipm.get(ip, {}).get("nf", ip)
    for f in frames:
        L = get(get(f, "_source"), "layers")
        fr = get(L, "frame")
        no, t = intx(first(fr, "frame.number")), float(first(fr, "frame.time_relative", 0))
        ips = getall(L, "ip")
        if not ips:
            continue
        src, dst = first(ips[0], "ip.src"), first(ips[0], "ip.dst")
        base = {"frame": no, "t": round(t, 3), "src": src, "dst": dst, "src_nf": nf(src), "dst_nf": nf(dst),
                "src_n": ipm.get(src, {}).get("name", nf(src)), "dst_n": ipm.get(dst, {}).get("name", nf(dst))}
        # --- NGAP（一個 SCTP 封包可能帶好幾個 NGAP 訊息）---
        for ng in getall(L, "ngap"):
            for one in as_list(ng):
                proc = intx(first(one, "ngap.procedureCode"))
                if proc is None:  # SCTP 的 ABORT／SHUTDOWN 等沒有 NGAP 內容的 chunk
                    continue
                pdu = str(first(one, "ngap.NGAP_PDU", ""))
                name = f'{NGAP_PROC.get(proc, f"proc{proc}")}{NGAP_PDU.get(pdu, "")}'
                if proc in (15, 46, 4):  # NAS 傳輸：名稱用 NAS 訊息
                    name = NGAP_PROC.get(proc)
                nas = []
                for x in vals(one, "nas_5gs.mm.message_type"):
                    nas.append(NAS_MM.get(intx(x), x))
                for x in vals(one, "nas_5gs.sm.message_type"):
                    nas.append(NAS_SM.get(intx(x), x))
                # ULNASTransport／DLNASTransport 只是外層容器，有內層 SM 訊息時不另外列
                if any(n in NAS_SM.values() for n in nas):
                    nas = [n for n in nas if n not in ("ULNASTransport", "DLNASTransport")]
                ev.append({**base, "proto": "NGAP", "name": name, "nas": nas, "tree": one})
        # --- PFCP ---
        for pf in getall(L, "pfcp"):
            mt = intx(first(pf, "pfcp.msg_type"))
            ev.append({**base, "proto": "PFCP", "name": PFCP.get(mt, f"type{mt}"), "cause": intx(first(pf, "pfcp.cause")),
                       "seq": intx(first(pf, "pfcp.seqno")), "tree": pf})
        # --- GTP-U ---
        for gt in getall(L, "gtp"):
            if intx(first(gt, "gtp.message")) not in (255, None):
                continue
            inner = ips[1] if len(ips) > 1 else None
            ev.append({**base, "proto": "GTP", "name": "T-PDU", "teid": intx(first(gt, "gtp.teid")),
                       "inner_src": first(inner, "ip.src") if inner else None,
                       "inner_dst": first(inner, "ip.dst") if inner else None})
        # --- SBI（HTTP/2 HEADERS）---
        tcp = get(L, "tcp")
        stream = (first(tcp, "tcp.srcport"), first(tcp, "tcp.dstport"))  # 用 port 配對，不用 tcp.stream（重複封包會讓它分裂）
        for h2 in getall(L, "http2"):
            for st in subtrees(h2, "http2.stream"):
                for s in as_list(st):
                    if intx(first(s, "http2.type")) != 1:
                        continue
                    sid = first(s, "http2.streamid")
                    method, path, status = first(s, "http2.headers.method"), first(s, "http2.headers.path"), first(s, "http2.headers.status")
                    if method:
                        ev.append({**base, "proto": "SBI", "name": f"{method} {path}", "method": method, "path": path,
                                   "key": (src, stream[0], dst, stream[1], sid)})
                    elif status:
                        ev.append({**base, "proto": "SBI-resp", "name": status, "status": intx(status),
                                   "key": (dst, stream[1], src, stream[0], sid)})
    ev.sort(key=lambda e: (e["t"], e["frame"]))
    # 配對 SBI 請求與回應
    pend = {}
    for e in ev:
        if e["proto"] == "SBI":
            pend[e["key"]] = e
        elif e["proto"] == "SBI-resp":
            q = pend.pop(e["key"], None)
            if q:
                q["status"] = e["status"]
                q["rtt_ms"] = round((e["t"] - q["t"]) * 1000, 1)
    for q in pend.values():
        q.setdefault("status", None)
    return [e for e in ev if e["proto"] != "SBI-resp"]


# ---------- 判定 ----------

class Report:
    def __init__(self):
        self.checks = []

    def add(self, cid, status, msg, **kw):
        self.checks.append({"id": cid, "status": status, "msg": msg, **kw})


def sbi(src, dst, method, path_re, ok=(200, 201, 202, 204)):
    pr = re.compile(path_re)
    return lambda e: (e["proto"] == "SBI" and e["src_nf"] == src and e["dst_nf"] in (dst, "scp") and e["method"] == method
                      and pr.search(e["path"] or "")), ok


def nas(name, src):
    return lambda e: e["proto"] == "NGAP" and name in e["nas"] and e["src_nf"] == src, None


def ngap(name, src):
    return lambda e: e["proto"] == "NGAP" and e["name"] == name and e["src_nf"] == src, None


def pfcp(name, src):
    return lambda e: e["proto"] == "PFCP" and e["name"] == name and e["src_nf"] == src, None


# 註冊與 PDU session 建立的關鍵步驟（依序）。(id, 說明, matcher)
FLOW = [
    ("reg.request", "UE 經 gNB 送 Registration Request 給 AMF", nas("RegistrationRequest", "gnb")),
    ("reg.ausf", "AMF → AUSF 開始認證", sbi("amf", "ausf", "POST", r"^/nausf-auth/v1/ue-authentications$")),
    ("reg.udm_auth", "AUSF → UDM 產生認證向量", sbi("ausf", "udm", "POST", r"/generate-auth-data$")),
    ("reg.udr_auth", "UDM → UDR 讀 subscriber 的認證資料", sbi("udm", "udr", "GET", r"/authentication-subscription$")),
    ("reg.auth_req", "AMF → UE Authentication Request", nas("AuthenticationRequest", "amf")),
    ("reg.auth_resp", "UE → AMF Authentication Response", nas("AuthenticationResponse", "gnb")),
    ("reg.aka_confirm", "AMF → AUSF 5G-AKA 確認", sbi("amf", "ausf", "PUT", r"/5g-aka-confirmation$")),
    ("reg.smc", "AMF → UE Security Mode Command", nas("SecurityModeCommand", "amf")),
    ("reg.smc_complete", "UE → AMF Security Mode Complete", nas("SecurityModeComplete", "gnb")),
    ("reg.uecm", "AMF → UDM 登記為服務這個 UE 的 AMF", sbi("amf", "udm", "PUT", r"/registrations/amf-3gpp-access$")),
    ("reg.sdm_am", "AMF → UDM 取 access and mobility 訂閱資料", sbi("amf", "udm", "GET", r"/am-data")),
    ("reg.am_policy", "AMF → PCF 建立 AM policy", sbi("amf", "pcf", "POST", r"^/npcf-am-policy-control/v1/policies$")),
    ("reg.accept", "AMF → UE Registration Accept", nas("RegistrationAccept", "amf")),
    ("reg.complete", "UE → AMF Registration Complete", nas("RegistrationComplete", "gnb")),
    ("pdu.request", "UE → AMF PDU Session Establishment Request", nas("PDUSessionEstablishmentRequest", "gnb")),
    ("pdu.smf_create", "AMF → SMF 建立 SM context", sbi("amf", "smf", "POST", r"^/nsmf-pdusession/v1/sm-contexts$")),
    ("pdu.sdm_sm", "SMF → UDM 取 session management 訂閱資料", sbi("smf", "udm", "GET", r"/sm-data")),
    ("pdu.sm_policy", "SMF → PCF 建立 SM policy", sbi("smf", "pcf", "POST", r"^/npcf-smpolicycontrol/v1/sm-policies$")),
    ("pdu.pfcp_est", "SMF → UPF PFCP Session Establishment", pfcp("SessionEstablishmentRequest", "smf")),
    ("pdu.pfcp_est_resp", "UPF → SMF PFCP Session Establishment Response", pfcp("SessionEstablishmentResponse", "upf")),
    ("pdu.n1n2", "SMF → AMF 下發 N1（NAS）／N2（NGAP）訊息", sbi("smf", "amf", "POST", r"/n1-n2-messages$")),
    ("pdu.ran_setup", "AMF → gNB PDU Session Resource Setup（帶 Establishment Accept）", ngap("PDUSessionResourceSetupRequest", "amf")),
    ("pdu.ran_setup_resp", "gNB → AMF PDU Session Resource Setup Response", ngap("PDUSessionResourceSetupResponse", "gnb")),
    ("pdu.smf_modify", "AMF → SMF 更新 SM context（帶 gNB 的 N3 位址）", sbi("amf", "smf", "POST", r"/sm-contexts/[^/]+/modify$")),
    ("pdu.pfcp_mod", "SMF → UPF PFCP Session Modification（設定下行）", pfcp("SessionModificationRequest", "smf")),
    ("pdu.pfcp_mod_resp", "UPF → SMF PFCP Session Modification Response", pfcp("SessionModificationResponse", "upf")),
    ("data.ul", "上行 GTP-U：gNB → UPF", (lambda e: e["proto"] == "GTP" and e["src_nf"] == "gnb" and e["dst_nf"] == "upf", None)),
    ("data.dl", "下行 GTP-U：UPF → gNB", (lambda e: e["proto"] == "GTP" and e["src_nf"] == "upf" and e["dst_nf"] == "gnb", None)),
]


def check_flow(R, ev, start):
    """從 start（去註冊之後第一個 Registration Request）開始依序找每個步驟；回傳 {id: 事件}"""
    found = {}
    if start is None:
        R.add("flow.start", "error", "抓到的封包裡沒有 Registration Request（UE 沒有重新註冊）")
        return found
    t_prev = start
    for cid, desc, (match, ok) in FLOW:
        cand = [e for e in ev if e["t"] >= t_prev and match(e)]
        anywhere = [e for e in ev if e["t"] >= start and match(e)]
        if cand:
            # HTTP/2 連線被 GOAWAY 關掉時，客戶端會在新連線上重送同一個請求（實測：AUSF 的 5g-aka-confirmation）。
            # 有成功的就用成功的那次；都沒成功才用第一次
            e = next((x for x in cand if not ok or x.get("status") in ok), cand[0])
            found[cid] = e
            t_prev = e["t"]
            via = "（經 SCP）" if e.get("dst_nf") == "scp" else ""
            if ok and e.get("status") not in ok:
                st = "沒有回應" if e.get("status") is None else f"回 {e['status']}"
                R.add(cid, "error", f"{desc}：{st}{via}", frame=e["frame"], t=e["t"])
            else:
                extra = f"（{e['status']}，{e.get('rtt_ms')} ms）" if ok else ""
                R.add(cid, "ok", f"{desc}{extra}{via}", frame=e["frame"], t=e["t"])
        elif anywhere:
            e = anywhere[0]
            found[cid] = e
            R.add(cid, "warn", f"{desc}：有出現但順序不對（t={e['t']}，前一步在 t={t_prev}）", frame=e["frame"])
        else:
            R.add(cid, "error", f"{desc}：沒有出現")
    return found


def check_ngsetup(R, ev, want_plmn, want_s, want_tac):
    req = [e for e in ev if e["proto"] == "NGAP" and e["name"] == "NGSetupRequest"]
    resp = [e for e in ev if e["proto"] == "NGAP" and e["name"] in ("NGSetupResponse", "NGSetupFailure")]
    if not req:
        R.add("ngsetup", "skip", "沒有抓到 NG Setup（gNB 沒有重啟，或 gNB 不在 Pod 裡）")
        return
    r = req[-1]
    rs = resp[-1] if resp else None
    if not rs or rs["name"] != "NGSetupResponse":
        R.add("ngsetup", "error", f"NG Setup 失敗：{rs['name'] if rs else '沒有回應'}")
    else:
        R.add("ngsetup", "ok", f"gNB → AMF NG Setup 成功（AMF {rs['src']}）")
    T = r["tree"]
    pl = {f"{a}/{b}" for a, b in zip(vals(T, "e212.mcc"), vals(T, "e212.mnc"))}
    R.add("content.ngsetup_plmn", "ok" if pl == {want_plmn} else "error", f"NG Setup Request 的 PLMN = {sorted(pl)}")
    tacs = {intx(x) for x in vals(T, "ngap.tAC")}
    if want_tac is not None:
        R.add("content.ngsetup_tac", "ok" if want_tac in tacs else "error", f"gNB 宣告的 TAC = {sorted(tacs)}")
    ss = set(zip(map(intx, vals(T, "ngap.sST")), map(sd_norm, vals(T, "ngap.sD"))))
    R.add("content.ngsetup_gnb_slice", "ok" if ss == {want_s} else ("extra" if want_s in ss else "error"),
          f"gNB 宣告的 S-NSSAI = {sorted(f'{a}/{b}' for a, b in ss)}")
    if rs and rs["name"] == "NGSetupResponse":
        ss2 = set(zip(map(intx, vals(rs["tree"], "ngap.sST")), map(sd_norm, vals(rs["tree"], "ngap.sD"))))
        R.add("content.ngsetup_amf_slice", "ok" if ss2 == {want_s} else ("extra" if want_s in ss2 else "error"),
              f"AMF 回應支援的 S-NSSAI = {sorted(f'{a}/{b}' for a, b in ss2)}")


def nas_tree(e, msg):
    """在 NGAP 事件裡找含某個 NAS 訊息的子樹"""
    for k, v in find(e["tree"], lambda k: k == "Plain NAS 5GS Message"):
        mt = [NAS_MM.get(intx(x)) or NAS_SM.get(intx(x)) for x in vals(v, "nas_5gs.mm.message_type") + vals(v, "nas_5gs.sm.message_type")]
        if msg in mt:
            return v
    return e["tree"]


def snssai_in(tree, prefix):
    out = set()
    for st in subtrees(tree, prefix):
        out |= set(zip(map(intx, vals(st, "nas_5gs.mm.sst")), map(sd_norm, vals(st, "nas_5gs.mm.mm_sd") or vals(st, "nas_5gs.mm.sd"))))
    return out


def check_content(R, ev, found, meta, want_plmn, want_s, want_dnn, want_supi, mec=None):
    fs = lambda s: sorted(f"{a}/{b}" for a, b in s)
    ue_ip = meta.get("ip_phase1") or meta.get("ue", {}).get("ip_after")
    # Registration Request：SUCI 的 PLMN
    if "reg.request" in found:
        T = nas_tree(found["reg.request"], "RegistrationRequest")
        mcc, mnc = first(T, "e212.mcc") or first(T, "e212.suci.mcc"), first(T, "e212.mnc") or first(T, "e212.suci.mnc")
        if mcc:
            R.add("content.suci_plmn", "ok" if f"{mcc}/{mnc}" == want_plmn else "error", f"Registration Request 的 SUCI PLMN = {mcc}/{mnc}")
    # Registration Accept：Allowed NSSAI、GUTI 的 PLMN
    if "reg.accept" in found:
        T = nas_tree(found["reg.accept"], "RegistrationAccept")
        allowed = snssai_in(T, "NSSAI - Allowed NSSAI")
        st = "ok" if allowed == {want_s} else ("extra" if want_s in allowed else "error")
        R.add("content.allowed_nssai", st, f"Registration Accept 給 UE 的 Allowed NSSAI = {fs(allowed)}")
        g = {f"{a}/{b}" for a, b in zip(vals(T, "e212.guami.mcc"), vals(T, "e212.guami.mnc"))}
        if g:
            R.add("content.guti_plmn", "ok" if g == {want_plmn} else "error", f"5G-GUTI 的 PLMN = {sorted(g)}")
        rej = subtrees(T, "NSSAI - Rejected NSSAI")
        if rej:
            R.add("content.rejected_nssai", "warn", "Registration Accept 帶了 Rejected NSSAI（UE 請求的某些 slice 被拒絕）")
    # PDU Session Establishment Request／Accept：DNN、S-NSSAI、UE IP
    if "pdu.request" in found:
        e = found["pdu.request"]
        dnn = set(vals(e["tree"], "nas_5gs.cmn.dnn"))
        s = snssai_in(e["tree"], "S-NSSAI")
        R.add("content.pdu_req_dnn", "ok" if dnn == {want_dnn} else "error", f"UE 請求的 DNN = {sorted(dnn)}")
        R.add("content.pdu_req_slice", "ok" if s == {want_s} else "error", f"UE 請求的 S-NSSAI = {fs(s)}")
    if "pdu.ran_setup" in found:
        e = found["pdu.ran_setup"]
        T = nas_tree(e, "PDUSessionEstablishmentAccept")
        addr = set(vals(T, "nas_5gs.sm.pdu_addr_inf_ipv4"))
        R.add("content.pdu_addr", "ok" if ue_ip and addr == {ue_ip} else "error",
              f"Establishment Accept 給的 UE IP = {sorted(addr)}，uesimtun0 = {ue_ip}")
        dnn = set(vals(T, "nas_5gs.cmn.dnn"))
        if dnn:
            R.add("content.pdu_acc_dnn", "ok" if dnn == {want_dnn} else "error", f"Accept 的 DNN = {sorted(dnn)}")
        s = snssai_in(T, "S-NSSAI")
        if s:
            R.add("content.pdu_acc_slice", "ok" if s == {want_s} else "error", f"Accept 的 S-NSSAI = {fs(s)}")
        q = sorted({intx(x) for x in vals(T, "nas_5gs.sm.5qi")})
        amb = (first(T, "nas_5gs.sm.session_ambr_dl"), first(T, "nas_5gs.sm.session_ambr_ul"))
        R.add("content.qos", "info", f"5QI = {q}，Session-AMBR DL/UL = {amb}（單位依 NAS 編碼）")
        dns = vals(T, "gsm_a.gm.sm.pco.dns_ipv4")
        if dns:
            R.add("content.dns", "info", f"PCO 給的 DNS = {dns}")

    # ---- 多 UPF（L2 ULCL）：分開看每個 UPF ----
    upf_nodes = []
    for e in ev:
        if e["proto"] == "PFCP" and e["src_nf"] == "smf" and e["name"] == "SessionEstablishmentRequest" and e["dst"] not in upf_nodes:
            upf_nodes.append(e["dst"])
    if len(upf_nodes) > 1:
        check_multi_upf(R, ev, found, meta, upf_nodes, ue_ip, want_dnn, mec)
        return

    # ---- PFCP 規則 ----
    est = found.get("pdu.pfcp_est")
    ul_fteid = None
    if est:
        pdrs = subtrees(est["tree"], "Create PDR")
        fars = {}
        for f in subtrees(est["tree"], "Create FAR"):
            fid = intx(first(f, "pfcp.far_id"))
            fars[fid] = {"dst": intx(first(f, "pfcp.dst_interface")), "forw": str(first(f, "pfcp.apply_action.forw", "")) in ("1", "True", "true"),
                         "drop": str(first(f, "pfcp.apply_action.drop", "")) in ("1", "True", "true")}
        R.add("pfcp.rules", "info", f"Session Establishment：{len(pdrs)} 條 PDR、{len(fars)} 條 FAR")
        ul = dl = 0
        for p in pdrs:
            pid = intx(first(p, "pfcp.pdr_id"))
            pdi = subtrees(p, "PDI")
            pdi = pdi[0] if pdi else O()
            src = intx(first(pdi, "pfcp.source_interface"))
            teid = first(pdi, "pfcp.f_teid.teid")
            fip = first(pdi, "pfcp.f_teid.ipv4_addr")
            ueip = first(pdi, "pfcp.ue_ip_addr_ipv4")
            sdf = vals(pdi, "pfcp.flow_desc")
            ni = first(pdi, "pfcp.network_instance")
            far = fars.get(intx(first(p, "pfcp.far_id")), {})
            if not (teid or ueip or sdf):
                R.add("pfcp.empty_pdi", "error", f"PDR {pid} 的 PDI 只有 source interface（沒有 F-TEID、UE IP、SDF filter），"
                      "會吃掉所有封包（參考答案坑 7）")
                continue
            if src == 0:  # Access → 上行
                ul += 1
                ul_fteid = (intx(teid), fip)
                if not teid:
                    R.add("pfcp.ul_fteid", "error", f"上行 PDR {pid} 沒有 F-TEID")
                if far and far.get("dst") != 1:
                    R.add("pfcp.ul_far", "error", f"上行 PDR {pid} 的 FAR 不是送往 Core（dst={far.get('dst')}）")
            elif src == 1:  # Core → 下行
                dl += 1
                if far and far.get("dst") not in (0, None):
                    R.add("pfcp.dl_far", "error", f"下行 PDR {pid} 的 FAR 不是送往 Access（dst={far.get('dst')}）")
            if ueip and ue_ip and ueip != ue_ip:
                R.add("pfcp.ue_ip", "error", f"PDR {pid} 的 UE IP {ueip} 和 uesimtun0 {ue_ip} 不同")
            if ni and want_dnn and ni != want_dnn:
                R.add("pfcp.network_instance", "warn", f"PDR {pid} 的 network instance = {ni}（DNN 是 {want_dnn}）")
        if ul and dl:
            R.add("pfcp.ul_dl", "ok", f"有上行 PDR {ul} 條、下行 PDR {dl} 條，UE IP {ue_ip}，UPF N3 F-TEID {ul_fteid}")
        else:
            R.add("pfcp.ul_dl", "error", f"上行 PDR {ul} 條、下行 PDR {dl} 條，必須各至少 1 條")
        if est.get("cause") is None:
            pass
    resp = found.get("pdu.pfcp_est_resp")
    if resp:
        R.add("pfcp.est_cause", "ok" if resp.get("cause") == 1 else "error", f"Session Establishment Response cause = {resp.get('cause')}（1 = accepted）")
    mresp = found.get("pdu.pfcp_mod_resp")
    if mresp:
        R.add("pfcp.mod_cause", "ok" if mresp.get("cause") == 1 else "error", f"Session Modification Response cause = {mresp.get('cause')}")

    # ---- TEID／IP 一致：NGAP ↔ PFCP ↔ GTP-U ----
    ran = found.get("pdu.ran_setup")
    ran_r = found.get("pdu.ran_setup_resp")
    ngap_ul = {(hexbytes_int(t), hexbytes_ip(a)) for t, a in zip(vals(ran["tree"], "ngap.gTP_TEID"), vals(ran["tree"], "ngap.transportLayerAddress"))} if ran else set()
    ngap_dl = {(hexbytes_int(t), hexbytes_ip(a)) for t, a in zip(vals(ran_r["tree"], "ngap.gTP_TEID"), vals(ran_r["tree"], "ngap.transportLayerAddress"))} if ran_r else set()
    if ul_fteid and ngap_ul:
        R.add("teid.ul_ngap_pfcp", "ok" if ul_fteid in ngap_ul else "error",
              f"AMF 告訴 gNB 的上行 TEID/IP {sorted(ngap_ul)} {'包含' if ul_fteid in ngap_ul else '不含'} PFCP 給 UPF 的 F-TEID {ul_fteid}")
    mod = found.get("pdu.pfcp_mod")
    ohc = set()
    if mod:
        for f in subtrees(mod["tree"], "Update FAR") + subtrees(mod["tree"], "Create FAR"):
            for o in subtrees(f, "Outer Header Creation"):
                ohc.add((intx(first(o, "pfcp.outer_hdr_creation.teid")), first(o, "pfcp.outer_hdr_creation.ipv4")))
        ohc.discard((None, None))
        if ngap_dl:
            ok = bool(ohc & ngap_dl)
            R.add("teid.dl_ngap_pfcp", "ok" if ok else "error",
                  f"gNB 回報的下行 TEID/IP {sorted(ngap_dl)}，PFCP 設給 UPF 的 outer header {sorted(ohc)}")
    gtp_ul = [e for e in ev if e["proto"] == "GTP" and e["src_nf"] == "gnb" and e["dst_nf"] == "upf"]
    gtp_dl = [e for e in ev if e["proto"] == "GTP" and e["src_nf"] == "upf" and e["dst_nf"] == "gnb"]
    if gtp_ul:
        te = {e["teid"] for e in gtp_ul}
        srcs = {e["inner_src"] for e in gtp_ul}
        good = ul_fteid and te == {ul_fteid[0]}
        R.add("gtp.ul_teid", "ok" if good else "error", f"上行 GTP-U 的 TEID {sorted(te)}（PFCP F-TEID {ul_fteid[0] if ul_fteid else '?'}）")
        R.add("gtp.ul_src", "ok" if srcs == {ue_ip} else "error", f"上行封包內層的來源 IP = {sorted(map(str, srcs))}（UE {ue_ip}）")
    if gtp_dl:
        te = {e["teid"] for e in gtp_dl}
        dsts = {e["inner_dst"] for e in gtp_dl}
        good = bool(ngap_dl) and te <= {t for t, _ in ngap_dl}
        R.add("gtp.dl_teid", "ok" if good else "error", f"下行 GTP-U 的 TEID {sorted(te)}（gNB 回報 {sorted(t for t, _ in ngap_dl)}）")
        R.add("gtp.dl_dst", "ok" if dsts == {ue_ip} else "error", f"下行封包內層的目的 IP = {sorted(map(str, dsts))}")
    ping = meta.get("ping", "")
    m = re.search(r"(\d+) packets transmitted, (\d+) received", ping)
    if m:
        tx, rx = int(m.group(1)), int(m.group(2))
        R.add("data.ping", "ok" if rx and len(gtp_ul) >= tx and len(gtp_dl) >= rx else "warn",
              f"重新註冊後 ping {rx}/{tx}；抓到上行 GTP-U {len(gtp_ul)} 個、下行 {len(gtp_dl)} 個")


# ---------- 多 UPF（L2 ULCL，2026-11-23）----------

def truthy(v):
    return str(v) in ("1", "True", "true")


def parse_rules(tree):
    """一個 PFCP Establishment／Modification 裡的 PDR、FAR（Create 和 Update 都算）"""
    pdrs, fars = {}, {}
    for kind in ("Create PDR", "Update PDR"):
        for p in subtrees(tree, kind):
            pdi = subtrees(p, "PDI")
            pdi = pdi[0] if pdi else O()
            pdrs[intx(first(p, "pfcp.pdr_id"))] = {
                "id": intx(first(p, "pfcp.pdr_id")), "prec": intx(first(p, "pfcp.precedence")),
                "src": intx(first(pdi, "pfcp.source_interface")), "teid": intx(first(pdi, "pfcp.f_teid.teid")),
                "fip": first(pdi, "pfcp.f_teid.ipv4_addr"), "ueip": first(pdi, "pfcp.ue_ip_addr_ipv4"),
                "sdf": vals(pdi, "pfcp.flow_desc"), "ni": first(pdi, "pfcp.network_instance"),
                "far": intx(first(p, "pfcp.far_id"))}
    for kind in ("Create FAR", "Update FAR"):
        for f in subtrees(tree, kind):
            ohc = [(intx(first(o, "pfcp.outer_hdr_creation.teid")), first(o, "pfcp.outer_hdr_creation.ipv4"))
                   for o in subtrees(f, "Outer Header Creation")]
            fars[intx(first(f, "pfcp.far_id"))] = {"dst": intx(first(f, "pfcp.dst_interface")),
                                                   "forw": truthy(first(f, "pfcp.apply_action.forw", "")),
                                                   "ohc": next((o for o in ohc if o != (None, None)), None)}
    return pdrs, fars


def check_multi_upf(R, ev, found, meta, upf_nodes, ue_ip, want_dnn, mec):
    name = {}
    for e in ev:
        name.setdefault(e["src"], e.get("src_n")); name.setdefault(e["dst"], e.get("dst_n"))
    nm = lambda ip: name.get(ip) or ip
    # 每個 UPF 的規則（依時間套用 Create／Update）、回應
    rules = {ip: ({}, {}) for ip in upf_nodes}
    for ip in upf_nodes:
        reqs = [e for e in ev if e["proto"] == "PFCP" and e["src_nf"] == "smf" and e["dst"] == ip
                and e["name"] in ("SessionEstablishmentRequest", "SessionModificationRequest")]
        resps = {e["seq"]: e for e in ev if e["proto"] == "PFCP" and e["src"] == ip and e["name"].endswith("Response")}
        bad = []
        for q in reqs:
            pd, fa = parse_rules(q["tree"])
            rules[ip][0].update(pd); rules[ip][1].update(fa)
            r = resps.get(q["seq"])
            if not r or r.get("cause") != 1:
                bad.append(f'{q["name"]}(seq {q["seq"]}) → {"沒有回應" if not r else "cause " + str(r.get("cause"))}')
        R.add("pfcp.node_cause", "error" if bad else "ok",
              f"UPF {nm(ip)}：{len(reqs)} 個 PFCP 請求" + (f"，失敗：{bad}" if bad else "，都回 accepted"))
    # N3 的 UPF：NGAP 告訴 gNB 的上行位址
    ran, ran_r = found.get("pdu.ran_setup"), found.get("pdu.ran_setup_resp")
    ngap_ul = {(hexbytes_int(t), hexbytes_ip(a)) for t, a in zip(vals(ran["tree"], "ngap.gTP_TEID"), vals(ran["tree"], "ngap.transportLayerAddress"))} if ran else set()
    ngap_dl = {(hexbytes_int(t), hexbytes_ip(a)) for t, a in zip(vals(ran_r["tree"], "ngap.gTP_TEID"), vals(ran_r["tree"], "ngap.transportLayerAddress"))} if ran_r else set()
    n3 = next((ip for _, ip in ngap_ul if ip in rules), None)
    if not n3:
        R.add("pfcp.n3_upf", "error", f"NGAP 給 gNB 的上行位址 {sorted(ngap_ul)} 不是任何收到 PFCP session 的 UPF {[nm(x) for x in upf_nodes]}")
        return
    R.add("pfcp.nodes", "info", f"{len(upf_nodes)} 個 UPF 收到 PFCP session：{[nm(x) for x in upf_nodes]}；接 N3 的是 {nm(n3)}")

    # 每個 UPF：PDI 不能是空的、上下行都要有、UE IP 一致
    for ip, (pdrs, fars) in rules.items():
        ul = [p for p in pdrs.values() if p["src"] == 0]
        dl = [p for p in pdrs.values() if p["src"] == 1]
        for p in pdrs.values():
            if not (p["teid"] or p["ueip"] or p["sdf"]):
                R.add("pfcp.empty_pdi", "error", f"UPF {nm(ip)} 的 PDR {p['id']} 的 PDI 只有 source interface（參考答案坑 7）")
            if p["ueip"] and ue_ip and p["ueip"] != ue_ip:
                R.add("pfcp.ue_ip", "error", f"UPF {nm(ip)} 的 PDR {p['id']} 的 UE IP {p['ueip']} 和 uesimtun0 {ue_ip} 不同")
        R.add("pfcp.node_ul_dl", "ok" if ul and dl else "error", f"UPF {nm(ip)}：上行 PDR {len(ul)} 條、下行 {len(dl)} 條")
    # 上行：N3 UPF 的預設上行 PDR（沒有 SDF）
    pdrs, fars = rules[n3]
    ul_def = [p for p in pdrs.values() if p["src"] == 0 and not p["sdf"]]
    ul_fteid = (ul_def[0]["teid"], ul_def[0]["fip"]) if ul_def else None
    R.add("teid.ul_ngap_pfcp", "ok" if ul_fteid in ngap_ul else "error",
          f"AMF 告訴 gNB 的上行 TEID/IP {sorted(ngap_ul)}，PFCP 給 {nm(n3)} 的預設上行 F-TEID {ul_fteid}")
    dl_ohc = {fars[p["far"]]["ohc"] for p in pdrs.values() if p["src"] == 1 and p["far"] in fars and fars[p["far"]]["ohc"]}
    if ngap_dl:
        R.add("teid.dl_ngap_pfcp", "ok" if dl_ohc & ngap_dl else "error",
              f"gNB 回報的下行 TEID/IP {sorted(ngap_dl)}，PFCP 設給 {nm(n3)} 的下行 outer header {sorted(dl_ohc)}")
    # N9：每條指向另一個 UPF 的 FAR，對方都要有同一個 F-TEID 的 PDR
    n9, bad = [], []
    for ip, (pd, fa) in rules.items():
        for p in pd.values():
            f = fa.get(p["far"])
            if not f or not f["ohc"] or f["ohc"][1] not in rules:
                continue
            t, peer = f["ohc"]
            ok = any(q["teid"] == t and q["fip"] == peer for q in rules[peer][0].values())
            (n9 if ok else bad).append(f"{nm(ip)} PDR{p['id']} → {nm(peer)} TEID {t:#x}")
    if n9 or bad:
        R.add("teid.n9", "error" if bad else "ok",
              f"N9 轉送規則 {len(n9) + len(bad)} 條" + (f"；對方沒有這個 F-TEID：{bad}" if bad else f"，對方都有對應的 F-TEID：{n9}"))
    # 預設 anchor：N3 UPF 預設上行 PDR 的 FAR 指向的 UPF
    f = fars.get(ul_def[0]["far"]) if ul_def else None
    default = f["ohc"][1] if f and f["ohc"] and f["ohc"][1] in rules else None
    R.add("pfcp.default_anchor", "ok" if default else "error",
          f"預設路徑：{nm(n3)} → {nm(default) if default else '沒有轉給其他 UPF'}")

    # ---- GTP-U：N3 ----
    gtp = [e for e in ev if e["proto"] == "GTP"]
    gtp_ul = [e for e in gtp if e["src_nf"] == "gnb" and e["dst"] == n3]
    gtp_dl = [e for e in gtp if e["src"] == n3 and e["dst_nf"] == "gnb"]
    if gtp_ul:
        te = {e["teid"] for e in gtp_ul}
        R.add("gtp.ul_teid", "ok" if ul_fteid and te == {ul_fteid[0]} else "error", f"上行 GTP-U 的 TEID {sorted(te)}（PFCP F-TEID {ul_fteid}）")
        srcs = {e["inner_src"] for e in gtp_ul}
        R.add("gtp.ul_src", "ok" if srcs == {ue_ip} else "error", f"上行封包內層的來源 IP = {sorted(map(str, srcs))}（UE {ue_ip}）")
    if gtp_dl:
        te = {e["teid"] for e in gtp_dl}
        R.add("gtp.dl_teid", "ok" if ngap_dl and te <= {t for t, _ in ngap_dl} else "error", f"下行 GTP-U 的 TEID {sorted(te)}（gNB 回報 {sorted(t for t, _ in ngap_dl)}）")
    # ---- GTP-U：N9（上網的流量走預設 anchor）----
    def n9_to(pred):
        return sorted({nm(e["dst"]) for e in gtp if e["src"] == n3 and e["dst"] in rules and pred(e)})
    def n9_from(peer, pred):
        return [e for e in gtp if e["src"] == peer and e["dst"] == n3 and pred(e)]
    mec_ip = (mec or {}).get("ip")
    inet = lambda e: e["inner_dst"] not in (mec_ip, None) and e["inner_src"] == ue_ip
    to = n9_to(inet)
    back = n9_from(default, lambda e: e["inner_dst"] == ue_ip and e["inner_src"] != mec_ip) if default else []
    R.add("data.n9_internet", "ok" if default and to == [nm(default)] and back else "error",
          f"上網的流量：{nm(n3)} → {to or '沒有'}（預設 anchor {nm(default) if default else '?'}），回程 {len(back)} 個")

    # ---- ULCL：MEC 的分流規則與實際流量 ----
    if not mec_ip:
        return
    cls = [p for p in pdrs.values() if p["src"] == 0 and any(mec_ip in x for x in p["sdf"])]
    tgt = None
    if cls:
        f = fars.get(cls[0]["far"])
        tgt = f["ohc"][1] if f and f["ohc"] else None
        st = "ok" if tgt in rules and tgt != default else "error"
        R.add("ulcl.pdr", st, f"{nm(n3)} 有 MEC 分流規則 PDR {cls[0]['id']}（優先序 {cls[0]['prec']}，{cls[0]['sdf']}）→ "
              f"{nm(tgt) if tgt else '沒有轉給其他 UPF'}" + ("（就是預設 anchor，沒有分出去）" if tgt == default else ""))
    else:
        R.add("ulcl.pdr", "error", f"{nm(n3)} 沒有比對 MEC {mec_ip} 的上行 PDR：MEC 流量會走預設路徑（ULCL 沒生效，坑 A／B）")
    tried = (meta.get("mec") or {}).get("tried")
    to_mec = n9_to(lambda e: e["inner_dst"] == mec_ip)
    if not tried:
        R.add("data.mec_ul", "skip", "抓封包時沒有送 MEC 流量")
        return
    want = nm(tgt) if tgt else None
    R.add("data.mec_ul", "ok" if to_mec and to_mec == [want] else "error",
          f"往 MEC 的上行：{nm(n3)} → {to_mec or '沒有 N9 封包'}（分流規則指向 {want or '沒有分流規則'}）")
    # 回程看 MEC 封包實際到的 PSA（沒有分流規則時，整個 session 可能都在另一個 PSA 上）
    peers = {e["dst"] for e in gtp if e["src"] == n3 and e["dst"] in rules and e["inner_dst"] == mec_ip}
    back = [e for p in peers for e in n9_from(p, lambda e: e["inner_src"] == mec_ip)]
    R.add("data.mec_dl", "ok" if back else "error",
          f"MEC 的回應：{sorted(nm(p) for p in peers) or '?'} → {nm(n3)} {len(back)} 個" +
          ("" if back or not peers else "（PSA 收到了但沒有回程：可能沒有回 UE 的路由，坑 C）"))


def check_sbi(R, ev, start):
    calls = [e for e in ev if e["proto"] == "SBI" and e["t"] >= start]
    bad = [e for e in calls if e.get("status") is not None and e["status"] >= 400]
    lost = [e for e in calls if e.get("status") is None]
    R.add("sbi.count", "info", f"重新註冊後共 {len(calls)} 個 SBI 呼叫")
    grp = collections.OrderedDict()
    for e in bad:  # UE 重試時同一個錯誤會重複出現，合併計數
        k = (e["src_nf"], e["dst_nf"], e["method"], (e["path"] or "").split("?")[0], e["status"])
        grp.setdefault(k, []).append(e)
    for (a, b, m, p, st), es in grp.items():
        R.add("sbi.error", "error", f"{a} → {b} {m} {p[:110]} 回 {st}" + (f"（{len(es)} 次）" if len(es) > 1 else ""),
              frame=es[0]["frame"])
    if not bad:
        R.add("sbi.error", "ok", "沒有 4xx／5xx 回應")
    retried = 0
    for e in lost:
        later = [x for x in calls if x["t"] >= e["t"] and x is not e and x.get("status") is not None
                 and (x["src_nf"], x["dst_nf"], x["method"], x["path"]) == (e["src_nf"], e["dst_nf"], e["method"], e["path"])]
        if later:
            retried += 1
            continue
        R.add("sbi.no_response", "warn", f"{e['src_nf']} → {e['dst_nf']} {e['method']} {e['path'][:120]} 抓不到回應", frame=e["frame"])
    if retried:
        R.add("sbi.retried", "info", f"{retried} 個請求沒有回應，但之後重送成功（HTTP/2 連線被 GOAWAY 關閉時的正常行為）")
    slow = sorted((e for e in calls if e.get("rtt_ms") is not None), key=lambda e: -e["rtt_ms"])[:3]
    if slow and slow[0]["rtt_ms"] > 1000:
        for e in slow:
            if e["rtt_ms"] > 1000:
                R.add("sbi.slow", "info", f"{e['src_nf']} → {e['dst_nf']} {e['method']} {e['path'][:80]} 花了 {e['rtt_ms']} ms")
    via_scp = [e for e in calls if e["dst_nf"] == "scp"]
    R.add("sbi.mode", "info", "NF 間經 SCP 間接溝通" if via_scp else "NF 間直接溝通（沒有經過 SCP）")
    unknown = sorted({e["dst"] for e in calls if e["dst_nf"] == e["dst"]} | {e["src"] for e in calls if e["src_nf"] == e["src"]})
    if unknown:
        R.add("sbi.unknown_ip", "warn", f"有 SBI 流量的 IP 對不到 Pod：{unknown}")


def edges(ev, start):
    c = collections.Counter()
    for e in ev:
        if e["t"] >= start and e["proto"] in ("SBI", "PFCP", "NGAP", "GTP"):
            c[(e.get("src_n", e["src_nf"]), e.get("dst_n", e["dst_nf"]), e["proto"])] += 1
    return [{"src": a, "dst": b, "proto": p, "n": n} for (a, b, p), n in sorted(c.items())]


def main():
    d, spec_path = sys.argv[1], sys.argv[2]
    spec = json.load(open(spec_path)).get("expect", {})
    want_plmn, want_dnn, want_supi = spec.get("plmn"), spec.get("dnn"), spec.get("supi")
    # spec 的 sd 是 free5GC 的 16 進位字串（"010203"），不能交給 sd_norm（純數字會被當成十進位，NAS 的 mm_sd 才是十進位）
    want_s = (spec.get("sst"), f'{int(str(spec["sd"]), 16):06x}' if spec.get("sd") else None)
    want_tac = spec.get("tac", 1)
    R = Report()
    try:
        meta = json.load(open(os.path.join(d, "meta.json")))
    except (FileNotFoundError, ValueError):
        meta = {"ok": False, "error": "no meta.json"}
    out = {"pass": None, "exact": None, "meta": meta}
    if meta.get("precondition") is False:
        # 前提不成立（V3 沒過）：不是 V6 的失敗，結果是無法判定
        R.add("capture", "skip", f"前提不成立，沒有進行：{meta.get('error')}")
        out.update({"pass": None, "exact": None, "counts": {"skip": 1}, "checks": R.checks})
        print(f"[ skip] capture                  {meta.get('error')}", file=sys.stderr)
        print("V6 n/a  (precondition failed)", file=sys.stderr)
        print(json.dumps(out, ensure_ascii=False, default=str))
        return
    if not meta.get("ok") or not os.path.exists(os.path.join(d, "capture.pcap")):
        R.add("capture", "error", f"沒有抓到封包：{meta.get('error', 'no capture.pcap')}")
    else:
        if meta.get("ue_restarted"):
            R.add("capture.ue_restarted", "info", "UE 去註冊後沒有自己重新註冊，改成重啟 UE 的 Pod")
        if meta.get("tun_up_after_s") is None:
            R.add("capture.reattach", "error", "UE 重啟後沒有重新建立 uesimtun0")
        else:
            R.add("capture.reattach", "ok", f"UE 去註冊、重啟後 {meta['tun_up_after_s']} 秒重新建好 uesimtun0（{meta['ue']['ip_after']}）")
        ipm = ip_map(d, meta)
        pcap, ndup = dedup(os.path.join(d, "capture.pcap"), meta.get("iface", "cni0"))
        if ndup:
            R.add("capture.dedup", "info", f"去掉 {ndup} 個重複的封包（cni0 上同一個封包出現兩次）")
        ev = events_from(tshark(pcap, sbi_ports(d)), ipm)
        dereg = [e for e in ev if e["proto"] == "NGAP" and "DeregistrationRequest(UE)" in e["nas"]]
        t_d = dereg[0]["t"] if dereg else 0
        start = next((e["t"] for e in ev if e["proto"] == "NGAP" and "RegistrationRequest" in e["nas"] and e["t"] >= t_d), None)
        R.add("capture.dereg", "ok" if dereg else "warn",
              "UE 重啟前有送出 Deregistration Request" if dereg else "重啟前沒看到 UE 的 Deregistration Request，AMF 可能還留著舊的 context")

        check_ngsetup(R, ev, want_plmn, want_s, want_tac)
        # 階段 2（重啟 gNB 的 NG Setup）之後的封包不算進註冊流程
        end = next((e["t"] for e in ev if e["proto"] == "NGAP" and e["name"] == "NGSetupRequest" and start is not None and e["t"] > start), float("inf"))
        win = [e for e in ev if e["t"] < end]
        regs = [e for e in win if e["proto"] == "NGAP" and e["name"] == "InitialUEMessage" and "RegistrationRequest" in e["nas"] and e["t"] >= t_d]
        if len(regs) > 1:
            R.add("capture.multi_reg", "warn", f"去註冊後 UE 註冊了 {len(regs)} 次（t={[e['t'] for e in regs]}）；"
                  "只有第一次會被逐步比對，之後的可能是重試")
        found = check_flow(R, win, start)
        # GTP-U 只看重新註冊之後的（之前的封包是舊 session 的 TEID）
        check_content(R, [e for e in win if e["t"] >= (start or 0)], found, meta, want_plmn, want_s, want_dnn, want_supi,
                      spec.get("mec"))
        check_sbi(R, win, start or 0)
        rec = meta.get("ue_recovered_after_gnb_restart")
        if rec is not None:
            # 只記錄：UERANSIM 的 UE 在 gNB 消失後常卡在 service request，參考部署也要對照過才能當成判定依據
            R.add("gnb_restart.ue_recovers", "info",
                  "gNB 重啟後 UE 自己恢復連線" if rec else "gNB 重啟後 UE 沒有在 20 秒內自己恢復，重啟 UE 後才恢復")
        if "reg.request" in found and "reg.accept" in found:
            R.add("time.registration", "info", f"註冊花了 {round((found['reg.accept']['t'] - found['reg.request']['t']) * 1000)} ms")
        if "pdu.request" in found and "pdu.ran_setup" in found:
            R.add("time.pdu", "info", f"PDU session 建立花了 {round((found['pdu.ran_setup']['t'] - found['pdu.request']['t']) * 1000)} ms")
        out["edges"] = edges(win, start or 0)
        out["sequence"] = [{k: e.get(k) for k in ("t", "frame", "src_nf", "dst_nf", "proto", "name", "nas", "status")}
                           for e in ev if e["proto"] != "GTP" or e["t"] >= (start or 0)][:600]
    st = [c["status"] for c in R.checks]
    out.update({"pass": "error" not in st, "exact": "error" not in st and "extra" not in st,
                "counts": {k: st.count(k) for k in ["ok", "error", "extra", "warn", "info", "skip"]}, "checks": R.checks})
    sym = {"ok": "  ok ", "error": "ERROR", "extra": "extra", "warn": " warn", "info": " info", "skip": " skip"}
    for c in R.checks:
        print(f"[{sym[c['status']]}] {c['id']:24} {c['msg']}", file=sys.stderr)
    print(f"V6 {'PASS' if out['pass'] else 'FAIL'}  exact={'yes' if out['exact'] else 'no'}  {out['counts']}", file=sys.stderr)
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
