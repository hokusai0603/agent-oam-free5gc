#!/usr/bin/env python3
"""V5：跨 NF 設定一致性（靜態檢查，只看設定檔，不需要網路在跑）。

    verify/consistency.py configmaps.yaml tasks/L1/spec.json [--subs subs.json] [--services cluster-state.txt]
        → stdout 一行 JSON，stderr 人看的摘要

configmaps.yaml 是 `kubectl get cm -A -o yaml` 的輸出（每個 trial 目錄都有）。
NF 設定靠 data 的檔名找（amfcfg.yaml、smfcfg.yaml…），UERANSIM 靠內容找（有 supi 的是 UE、有 amfConfigs 的是 gNB），
不依賴 agent 取的 release／ConfigMap 名稱。

每一項檢查的結果：
  ok     ：符合
  error  ：不符合規格，或 NF 之間對不上（會讓註冊、PDU session 或資料面失敗，或違反任務描述）
  extra  ：規格要的都有，但還多設了規格外的東西（例如多的 slice）。pass 不看它，exact 才看
  warn   ：目前不影響，但是潛在問題（例如 L1 單一 UPF 卻帶 DNAI，坑 7）
  info   ：只記錄
  skip   ：缺資料，無法判斷（例如找不到某個 NF 的設定）
pass  = 沒有 error
exact = 沒有 error 也沒有 extra
"""
import argparse
import ipaddress
import json
import re
import sys

import yaml


# ---------- 讀設定 ----------

def load_configs(path):
    """回傳 {kind: [(cm名稱, 解析後的 dict, 原文)]}。全部用 BaseLoader 讀成字串，避免 010203 被當成八進位。"""
    # runner 存的 configmaps.yaml 是「每個 namespace 一份 kubectl get cm -o yaml」直接接在一起，
    # 中間沒有 '---'；在每個頂層的 apiVersion 前補上分隔，才不會只讀到最後一個 namespace
    text = re.sub(r"(?m)^apiVersion:", "---\napiVersion:", open(path).read())
    items = []
    for doc in yaml.load_all(text, Loader=yaml.BaseLoader):
        if isinstance(doc, dict):
            items += doc.get("items", []) if "items" in doc else ([doc] if doc.get("kind") == "ConfigMap" else [])
    found = {}
    nf_files = {f"{n}cfg.yaml": n for n in
                ["amf", "smf", "upf", "nssf", "ausf", "udm", "udr", "pcf", "nrf", "chf", "nef", "bsf", "scp", "webui"]}
    for cm in items:
        ns = cm.get("metadata", {}).get("namespace", "")
        if ns in ("kube-system", "kube-public", "kube-node-lease"):
            continue
        name = f'{ns}/{cm.get("metadata", {}).get("name", "")}'
        for fname, text in (cm.get("data") or {}).items():
            if not fname.endswith((".yaml", ".yml")):
                continue
            try:
                d = yaml.load(text, Loader=yaml.BaseLoader)
            except yaml.YAMLError:
                continue
            if not isinstance(d, dict):
                continue
            kind = nf_files.get(fname)
            if fname == "uerouting.yaml":
                kind = "uerouting"
            elif kind is None:
                if "supi" in d and "sessions" in d:
                    kind = "ue"
                elif "amfConfigs" in d:
                    kind = "gnb"
            if kind:
                found.setdefault(kind, []).append((f"{name}:{fname}", d, text))
    return found


# ---------- 正規化 ----------

def sst(v):
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return None


def sd_f5gc(v):
    """free5GC 的 sd 是 16 進位字串（"010203"）"""
    if v is None or v == "":
        return None
    s = str(v).lower()
    s = s[2:] if s.startswith("0x") else s
    try:
        return f"{int(s, 16):06x}"
    except ValueError:
        return s


def sd_ueransim(v):
    """UERANSIM 的 sd 是整數，可以寫 0x010203 或十進位"""
    if v is None or v == "":
        return None
    try:
        return f"{int(str(v), 0):06x}"
    except ValueError:
        return str(v)


def snssai(d, sdfn=sd_f5gc):
    if not isinstance(d, dict):
        return None
    return (sst(d.get("sst")), sdfn(d.get("sd")))


def fmt(s):
    return f"{s[0]}/{s[1]}" if s and s[1] else f"{s[0]}/-" if s else "?"


def plmn(d):
    if not isinstance(d, dict):
        return None
    return f'{str(d.get("mcc", "")).strip()}/{str(d.get("mnc", "")).strip()}'


def tac_hex(v):
    """free5GC 的 TAC 是 16 進位字串（"000001"）"""
    try:
        return int(str(v), 16)
    except (TypeError, ValueError):
        return None


def tac_int(v):
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return None


def g(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return default if d is None else d


def lst(v):
    return v if isinstance(v, list) else []


# ---------- 檢查 ----------

class Report:
    def __init__(self):
        self.checks = []

    def add(self, cid, status, msg, **kw):
        self.checks.append({"id": cid, "status": status, "msg": msg, **kw})

    def compare_set(self, cid, what, actual, want, where, missing_status="error", allow_extra=False):
        """actual 必須包含 want；多出來的記為 extra"""
        actual, want = set(actual), set(want)
        miss, more = want - actual, actual - want
        f = lambda xs: sorted(fmt(x) if isinstance(x, tuple) and len(x) == 2 and isinstance(x[0], (int, type(None))) else str(x) for x in xs)
        if miss:
            self.add(cid, missing_status, f"{where} 的 {what} 缺少 {f(miss)}（有 {f(actual)}）")
        elif more and not allow_extra:
            self.add(cid, "extra", f"{where} 的 {what} 多了 {f(more)}")
        else:
            self.add(cid, "ok", f"{where} 的 {what} = {f(actual)}")


def run(cfg, spec, subs=None, services=None):
    R = Report()
    want_plmn = spec.get("plmn")
    want_s = (spec.get("sst"), sd_f5gc(spec.get("sd")))
    want_dnn = spec.get("dnn")
    want_supi = spec.get("supi")

    def one(kind):
        v = cfg.get(kind, [])
        return v[0] if v else None

    amf, smf, nssf, ausf, nrf = one("amf"), one("smf"), one("nssf"), one("ausf"), one("nrf")
    upfs, gnbs, ues = cfg.get("upf", []), cfg.get("gnb", []), cfg.get("ue", [])
    for kind in ["amf", "smf", "upf", "nssf", "ausf", "udm", "udr", "pcf", "nrf", "gnb", "ue"]:
        if not cfg.get(kind):
            R.add(f"found.{kind}", "skip", f"找不到 {kind} 的設定檔")
        elif len(cfg[kind]) > 1 and kind not in ("upf", "gnb", "ue"):
            R.add(f"found.{kind}", "warn", f"{kind} 有 {len(cfg[kind])} 份設定：{[c[0] for c in cfg[kind]]}，只檢查第一份")

    A = g(amf[1], "configuration", default={}) if amf else {}
    S = g(smf[1], "configuration", default={}) if smf else {}
    N = g(nssf[1], "configuration", default={}) if nssf else {}

    # ===== C1 PLMN =====
    if want_plmn:
        srcs = []
        if amf:
            srcs += [("AMF servedGuamiList", plmn(g(x, "plmnId"))) for x in lst(A.get("servedGuamiList"))]
            srcs += [("AMF supportTaiList", plmn(g(x, "plmnId"))) for x in lst(A.get("supportTaiList"))]
            srcs += [("AMF plmnSupportList", plmn(g(x, "plmnId"))) for x in lst(A.get("plmnSupportList"))]
        if smf:
            srcs += [("SMF plmnList", plmn(x)) for x in lst(S.get("plmnList"))]
        if ausf:
            srcs += [("AUSF plmnSupportList", plmn(x)) for x in lst(g(ausf[1], "configuration", "plmnSupportList"))]
        if nrf:
            p = g(nrf[1], "configuration", "DefaultPlmnId")
            if p:
                srcs.append(("NRF DefaultPlmnId", plmn(p)))
        if nssf:
            srcs += [("NSSF supportedPlmnList", plmn(x)) for x in lst(N.get("supportedPlmnList"))]
            srcs += [("NSSF supportedNssaiInPlmnList", plmn(g(x, "plmnId"))) for x in lst(N.get("supportedNssaiInPlmnList"))]
            srcs += [("NSSF taList", plmn(g(x, "tai", "plmnId"))) for x in lst(N.get("taList"))]
        for n, d, _ in gnbs:
            srcs.append((f"gNB {n}", plmn(d)))
        for n, d, _ in ues:
            srcs.append((f"UE {n}", plmn(d)))
        by = {}
        for where, p in srcs:
            by.setdefault(where, set()).add(p)
        for where, ps in by.items():
            if want_plmn not in ps and where == "NSSF taList":
                # chart 預設的 taList 只有範例 PLMN；L1 的註冊／PDU session 流程用不到 NSSF 的 TA 資料（18 個 trial 都能註冊）
                R.add("plmn", "warn", f"{where} 沒有 {want_plmn} 的 TA（只有 {sorted(ps)}）；L1 流程用不到，"
                      "AMF 需要向 NSSF 查 slice 可用性時會查不到", where=where)
            elif want_plmn not in ps:
                R.add("plmn", "error", f"{where} 的 PLMN 是 {sorted(ps)}，規格是 {want_plmn}", where=where)
            elif len(ps) > 1:
                R.add("plmn", "extra", f"{where} 除了 {want_plmn} 還有 {sorted(ps - {want_plmn})}", where=where)
            else:
                R.add("plmn", "ok", f"{where} = {want_plmn}", where=where)
        # NSSF 的 amfSetList／nsiList 是 chart 寫死在 template 的範例資料（466/92），只記錄
        demo = {plmn(g(t, "tai", "plmnId")) for s in lst(N.get("amfSetList"))
                for t in lst(s.get("supportedNssaiAvailabilityData"))} - {want_plmn}
        if demo:
            R.add("plmn.nssf_demo", "info", f"NSSF amfSetList 有其他 PLMN 的範例資料 {sorted(demo)}（chart template 內建）")
    if want_supi and ues:
        for n, d, _ in ues:
            supi = d.get("supi", "")
            st = "ok" if supi == want_supi else "error"
            R.add("supi.ue", st, f"UE {n} 的 SUPI = {supi}")
            if want_plmn and supi.startswith("imsi-"):
                mcc, mnc = want_plmn.split("/")
                if not supi[5:].startswith(mcc + mnc):
                    R.add("supi.plmn", "error", f"SUPI {supi} 的開頭不是 PLMN {mcc}{mnc}")

    # ===== C2 TAC =====
    amf_tacs = {tac_hex(x.get("tac")) for x in lst(A.get("supportTaiList"))
                if plmn(g(x, "plmnId")) == want_plmn} if amf else set()
    for n, d, _ in gnbs:
        t = tac_int(d.get("tac"))
        if amf:
            R.add("tac.gnb_in_amf", "ok" if t in amf_tacs else "error",
                  f"gNB {n} 的 TAC {t} {'在' if t in amf_tacs else '不在'} AMF supportTaiList {sorted(x for x in amf_tacs if x is not None)}")
    if nssf and amf_tacs:
        nssf_tacs = {tac_hex(g(x, "tai", "tac")) for x in lst(N.get("taList")) if plmn(g(x, "tai", "plmnId")) == want_plmn}
        if not nssf_tacs:
            pass  # 上面 plmn／snssai.nssf_ta 已經記過
        elif not nssf_tacs & amf_tacs:
            R.add("tac.nssf", "warn", f"NSSF taList 的 TAC {sorted(x for x in nssf_tacs if x is not None)} 和 AMF {sorted(amf_tacs)} 沒有交集")
        else:
            R.add("tac.nssf", "ok", f"NSSF taList 含 AMF 的 TAC")

    # ===== C3 S-NSSAI =====
    if want_s[0] is not None:
        if amf:
            amf_s = [snssai(s) for x in lst(A.get("plmnSupportList")) if plmn(g(x, "plmnId")) == want_plmn
                     for s in lst(x.get("snssaiList"))]
            R.compare_set("snssai.amf", "S-NSSAI", amf_s, [want_s], "AMF plmnSupportList")
        if smf:
            smf_s = [snssai(x.get("sNssai")) for x in lst(S.get("snssaiInfos"))]
            R.compare_set("snssai.smf", "S-NSSAI", smf_s, [want_s], "SMF snssaiInfos")
        if nssf:
            nssf_s = [snssai(s) for x in lst(N.get("supportedNssaiInPlmnList")) if plmn(g(x, "plmnId")) == want_plmn
                      for s in lst(x.get("supportedSnssaiList"))]
            R.compare_set("snssai.nssf", "S-NSSAI", nssf_s, [want_s], "NSSF supportedNssaiInPlmnList")
            ta_s = [snssai(s) for x in lst(N.get("taList")) if plmn(g(x, "tai", "plmnId")) == want_plmn
                    for s in lst(x.get("supportedSnssaiList"))]
            if ta_s:
                R.compare_set("snssai.nssf_ta", "S-NSSAI", ta_s, [want_s], "NSSF taList", missing_status="warn")
            else:
                R.add("snssai.nssf_ta", "warn", f"NSSF taList 沒有 {want_plmn} 的 TA，所以也沒有 slice 資料")
            nsi = {snssai(x.get("snssai")) for x in lst(N.get("nsiList"))} - {want_s}
            if nsi:
                R.add("snssai.nssf_nsi", "info", f"NSSF nsiList 還有 {sorted(map(fmt, nsi))}（chart template 內建的範例）")
        for n, d, _ in gnbs:
            R.compare_set("snssai.gnb", "S-NSSAI", [snssai(s, sd_ueransim) for s in lst(d.get("slices"))], [want_s], f"gNB {n}")
        for n, d, _ in ues:
            sess = [snssai(s.get("slice"), sd_ueransim) for s in lst(d.get("sessions"))]
            R.compare_set("snssai.ue_session", "PDU session 的 S-NSSAI", sess, [want_s], f"UE {n}")
            R.compare_set("snssai.ue_configured", "configured-nssai",
                          [snssai(s, sd_ueransim) for s in lst(d.get("configured-nssai"))], [want_s], f"UE {n}")
            dn = [snssai(s, sd_ueransim) for s in lst(d.get("default-nssai"))]
            if dn and set(dn) != {want_s}:
                R.add("snssai.ue_default", "warn", f"UE {n} 的 default-nssai 是 {sorted(map(fmt, dn))}，和唯一的 slice {fmt(want_s)} 不同"
                      "（UE 註冊時會一併請求；chart 預設 1/000001）")
            else:
                R.add("snssai.ue_default", "ok", f"UE {n} 的 default-nssai = {sorted(map(fmt, dn))}")
        # AMF 宣告的 slice 都要有 SMF 服務（否則 UE 選到這個 slice 會建不了 session）
        if amf and smf:
            orphan = set(amf_s) - set(smf_s)
            if orphan:
                R.add("snssai.amf_without_smf", "warn", f"AMF 宣告了 {sorted(map(fmt, orphan))}，但 SMF 沒有服務這些 slice")

    # ===== C4 DNN =====
    if want_dnn:
        if amf:
            R.compare_set("dnn.amf", "supportDnnList", lst(A.get("supportDnnList")), [want_dnn], "AMF")
        if smf:
            dnns = [x.get("dnn") for s in lst(S.get("snssaiInfos")) if snssai(s.get("sNssai")) == want_s
                    for x in lst(s.get("dnnInfos"))]
            R.compare_set("dnn.smf", "DNN", dnns, [want_dnn], f"SMF snssaiInfos[{fmt(want_s)}]")
        for n, d, _ in upfs:
            R.compare_set("dnn.upf", "dnnList", [x.get("dnn") for x in lst(d.get("dnnList"))], [want_dnn], f"UPF {n}")
        for n, d, _ in ues:
            R.compare_set("dnn.ue", "PDU session 的 APN", [s.get("apn") for s in lst(d.get("sessions"))], [want_dnn], f"UE {n}")

    # ===== C5 UE IP 位址池：SMF 給 UPF 的 pool 要和 UPF 自己的 dnnList cidr 一致 =====
    up = g(S, "userplaneInformation", default={}) if smf else {}
    nodes = g(up, "upNodes", default={}) or {}
    smf_upfs = {k: v for k, v in nodes.items() if isinstance(v, dict) and v.get("type") == "UPF"}
    ans = {k for k, v in nodes.items() if isinstance(v, dict) and v.get("type") == "AN"}
    smf_pools = set()
    smf_dnai = []
    for k, v in smf_upfs.items():
        for si in lst(v.get("sNssaiUpfInfos")):
            if snssai(si.get("sNssai")) != want_s:
                continue
            for di in lst(si.get("dnnUpfInfoList")):
                if di.get("dnn") == want_dnn:
                    smf_pools |= {p.get("cidr") for p in lst(di.get("pools"))}
                    if lst(di.get("dnaiList")):
                        smf_dnai.append((k, lst(di.get("dnaiList"))))
    upf_cidrs = {x.get("cidr") for n, d, _ in upfs for x in lst(d.get("dnnList")) if x.get("dnn") == want_dnn}
    if smf and upfs and len(smf_upfs) > 1:
        check_multi_upf(R, spec, S, nodes, smf_upfs, ans, upfs, cfg.get("uerouting") or [], want_s, want_dnn, want_supi)
    elif smf and upfs:
        if not smf_pools:
            R.add("pool.smf", "error", f"SMF 沒有給 {fmt(want_s)}／{want_dnn} 任何 UE IP pool")
        elif not upf_cidrs:
            R.add("pool.upf", "error", f"UPF 的 dnnList 沒有 {want_dnn}")
        elif smf_pools != upf_cidrs:
            R.add("pool.match", "error", f"SMF 的 UE IP pool {sorted(smf_pools)} 和 UPF dnnList 的 cidr {sorted(upf_cidrs)} 不同"
                  "（UPF 依 cidr 設路由與 NAT，對不上時下行或 NAT 會失敗）")
        else:
            R.add("pool.match", "ok", f"SMF pool = UPF cidr = {sorted(smf_pools)}")
    # UPF 出口：natifname 或 natIfCIDR 至少要有一個（v4.3.0；坑：natIfCIDR 預設 192.168.0.0/16）
    for n, d, _ in upfs:
        for x in lst(d.get("dnnList")):
            if x.get("dnn") != want_dnn:
                continue
            if x.get("natifname"):
                R.add("upf.nat", "info", f"UPF {n} 用 natifname={x.get('natifname')}")
            elif x.get("natIfCIDR"):
                R.add("upf.nat", "info", f"UPF {n} 用 natIfCIDR={x.get('natIfCIDR')}（是否對得到介面，要看執行時的 Pod IP）")

    # ===== C6 拓樸：UPF 數量、gNB↔UPF 的 link、ULCL 路由 =====
    if smf:
        want_upf = spec.get("upf_count")
        if want_upf is not None:
            R.add("topo.smf_upf_count", "ok" if len(smf_upfs) == want_upf else "error",
                  f"SMF upNodes 有 {len(smf_upfs)} 個 UPF（{sorted(smf_upfs)}），規格 {want_upf}")
        if upfs and want_upf is not None and len(upfs) != len(smf_upfs):
            R.add("topo.upf_cfg_count", "warn", f"UPF 設定檔有 {len(upfs)} 份，SMF upNodes 有 {len(smf_upfs)} 個")
        links = [(l.get("A"), l.get("B")) for l in lst(up.get("links"))]
        for a, b in links:
            for x in (a, b):
                if x not in nodes:
                    R.add("topo.link_node", "error", f"SMF links 用到不存在的節點 {x}")
        linked = {x for l in links for x in l}
        for k in smf_upfs:
            if k not in linked:
                R.add("topo.upf_linked", "error", f"SMF 的 UPF {k} 沒有任何 link")
        an_upf = [l for l in links if (l[0] in ans and l[1] in smf_upfs) or (l[1] in ans and l[0] in smf_upfs)]
        R.add("topo.an_link", "ok" if an_upf else "error",
              f"gNB（AN）直接連到的 UPF：{sorted({x for l in an_upf for x in l} & set(smf_upfs)) or '沒有'}")
        for k, v in smf_upfs.items():
            n3 = [i for i in lst(v.get("interfaces")) if i.get("interfaceType") == "N3"]
            if k in {x for l in an_upf for x in l} and not n3:
                R.add("topo.n3", "error", f"SMF 的 UPF {k} 連到 gNB，但沒有 N3 interface")
            for i in n3:
                ni = lst(i.get("networkInstances"))
                if want_dnn and ni and want_dnn not in ni:
                    R.add("topo.n3_ni", "error", f"UPF {k} 的 N3 networkInstances {ni} 沒有 {want_dnn}")
    ur = cfg.get("uerouting")
    if ur:
        for n, d, _ in ur:
            for name, r in (d.get("ueRoutingInfo") or {}).items():
                for l in lst(r.get("topology")):
                    for x in (l.get("A"), l.get("B")):
                        if x not in nodes:
                            R.add("topo.uerouting", "error", f"uerouting {name} 用到 SMF 沒有的節點 {x}")
                for sp in lst(r.get("specificPath")):
                    for x in lst(sp.get("path")):
                        if x not in nodes:
                            R.add("topo.uerouting", "error", f"uerouting {name} 的 specificPath 用到 SMF 沒有的節點 {x}")

    # ===== C7 DNAI（坑 7）：單一 UPF 卻帶 dnaiList → 有 PCC rule（FlowRules／ChargingDatas）時建不了資料路徑 =====
    if smf_dnai and len(smf_upfs) == 1:
        R.add("dnai.single_upf", "warn", f"單一 UPF 但 SMF 設定帶 dnaiList {smf_dnai}："
              "subscriber 有 flow rule 或 slice 層級計費時，SMF 找不到 DNAI 為空的 UPF（參考答案坑 7）")
    elif smf_dnai:
        R.add("dnai", "info", f"SMF 的 UPF dnaiList：{smf_dnai}")

    # ===== C8 位址參照 =====
    nrf_uris = {}
    for kind, lstc in cfg.items():
        for n, d, _ in lstc:
            u = g(d, "configuration", "nrfUri")
            if u:
                nrf_uris.setdefault(u, []).append(kind)
    if len(nrf_uris) > 1:
        R.add("ref.nrf", "error", f"各 NF 指到不同的 NRF：{nrf_uris}")
    elif nrf_uris:
        R.add("ref.nrf", "ok", f"所有 NF 的 nrfUri = {list(nrf_uris)[0]}")
    if services is not None:
        amf_port = g(A, "ngapPort")
        for n, d, _ in gnbs:
            for c in lst(d.get("amfConfigs")):
                addr, port = c.get("address"), c.get("port")
                svc = services.get(addr.split(".")[0]) if addr else None
                if re.fullmatch(r"[\d.]+", addr or ""):
                    R.add("ref.gnb_amf", "info", f"gNB {n} 用 IP 連 AMF：{addr}:{port}")
                elif not svc:
                    R.add("ref.gnb_amf", "error", f"gNB {n} 的 AMF 位址 {addr} 找不到對應的 Service")
                elif f"{port}/SCTP" not in svc:
                    R.add("ref.gnb_amf", "error", f"gNB {n} 連 {addr}:{port}，但 Service 的 port 是 {svc}")
                else:
                    R.add("ref.gnb_amf", "ok", f"gNB {n} → Service {addr} {port}/SCTP")
                if amf_port and str(port) != str(amf_port):
                    R.add("ref.gnb_amf_port", "warn", f"gNB 連 port {port}，AMF ngapPort 是 {amf_port}（經 Service 轉換時可能正常）")

    # ===== C9 安全演算法：AMF 選的演算法 UE 要支援 =====
    if amf and ues:
        integ = [x for x in lst(g(A, "security", "integrityOrder"))]
        ciph = [x for x in lst(g(A, "security", "cipheringOrder"))]
        for n, d, _ in ues:
            ia = {f"NIA{k[2:]}" for k, v in (d.get("integrity") or {}).items() if str(v).lower() == "true"}
            ea = {f"NEA{k[2:]}" for k, v in (d.get("ciphering") or {}).items() if str(v).lower() == "true"}
            ia.add("NIA0"); ea.add("NEA0")
            ok_i = [x for x in integ if x in ia]
            ok_c = [x for x in ciph if x in ea]
            R.add("sec.integrity", "ok" if ok_i else "error", f"AMF integrityOrder {integ}，UE {n} 支援 {sorted(ia)}")
            R.add("sec.ciphering", "ok" if ok_c else "error", f"AMF cipheringOrder {ciph}，UE {n} 支援 {sorted(ea)}")
            if ok_c and ok_c[0] == "NEA0":
                R.add("sec.null_cipher", "info", "NAS 用 NEA0（不加密），是 free5GC chart 預設")

    # ===== C10 subscriber（UDR）：K／OPc、slice、DNN 要和 UE 一致 =====
    if subs is None:
        R.add("subs", "skip", "沒有 subscriber 資料（舊 trial 沒有存 MongoDB），只能由 V2 間接確認")
    else:
        check_subs(R, subs, ues, want_supi, want_s, want_dnn)

    st = [c["status"] for c in R.checks]
    return {
        "pass": "error" not in st,
        "exact": "error" not in st and "extra" not in st,
        "counts": {k: st.count(k) for k in ["ok", "error", "extra", "warn", "info", "skip"]},
        "checks": R.checks,
    }


# ===== 多 UPF（L2 ULCL，2026-11-23）=====

def net(c):
    try:
        return ipaddress.ip_network(str(c), strict=False)
    except ValueError:
        return None


def covers(cidrs, pool):
    p = net(pool)
    return bool(p) and any(n and p.subnet_of(n) for n in map(net, cidrs) if n and n.version == p.version)


def norm_token(x):
    """SMF 的 nodeID／endpoint → 比對用的關鍵字：取第一段 DNS label，去掉 service 字樣。"""
    x = str(x or "").split(".")[0].lower().replace("_", "-")
    x = re.sub(r"-?service(-name)?$", "", x)
    return x


def map_upf_cfgs(smf_upfs, upfs):
    """SMF upNodes 的每個 UPF → 它的 upfcfg（(名稱, dict)）。
    chart 的設定裡位址是執行時才替換的佔位字（IUPF1_SERVICE_NAME、POD_IP_ADDR），所以依序用：
    (1) 佔位字／DNS 名稱的關鍵字出現在 ConfigMap 名稱裡 (2) nodeID 和 UPF 的 pfcp nodeID／addr 字串相同 (3) 依角色（有沒有 N3）唯一對應"""
    res, used = {}, set()
    for k, v in smf_upfs.items():
        toks = {norm_token(v.get("nodeID"))} | {norm_token(e) for i in lst(v.get("interfaces")) for e in lst(i.get("endpoints"))}
        toks = {t for t in toks if len(t) >= 3 and not re.fullmatch(r"[\\d-]+", t)}
        cand = [(n, d) for n, d, _ in upfs if n not in used and any(t in n.lower().replace("_", "-") for t in toks)]
        if len(cand) != 1:
            ids = {str(v.get("nodeID"))}
            cand = [(n, d) for n, d, _ in upfs if n not in used and
                    ids & {str(g(d, "pfcp", "nodeID")), str(g(d, "pfcp", "addr"))} - {"None", "POD_IP_ADDR"}]
        if len(cand) == 1:
            res[k] = cand[0]
            used.add(cand[0][0])
    # 角色：SMF 端有沒有 N3 ↔ upfcfg 的 ifList 有沒有 N3
    left = {k: v for k, v in smf_upfs.items() if k not in res}
    rest = [(n, d) for n, d, _ in upfs if n not in used]
    def has_n3_smf(v): return any(i.get("interfaceType") == "N3" for i in lst(v.get("interfaces")))
    def has_n3_cfg(d): return any(str(i.get("type")).upper() == "N3" for i in lst(g(d, "gtpu", "ifList")))
    for role in (True, False):
        ks = [k for k, v in left.items() if has_n3_smf(v) == role]
        cs = [c for c in rest if has_n3_cfg(c[1]) == role]
        if len(ks) == 1 and len(cs) == 1:
            res[ks[0]] = cs[0]
    return res


def check_multi_upf(R, spec, S, nodes, smf_upfs, ans, upfs, uerouting, want_s, want_dnn, want_supi):
    pools = {}
    for k, v in smf_upfs.items():
        for si in lst(v.get("sNssaiUpfInfos")):
            if snssai(si.get("sNssai")) != want_s:
                continue
            for di in lst(si.get("dnnUpfInfoList")):
                if di.get("dnn") == want_dnn:
                    pools.setdefault(k, []).extend(p.get("cidr") for p in lst(di.get("pools")))
    m = map_upf_cfgs(smf_upfs, upfs)
    unmapped = sorted(set(smf_upfs) - set(m))
    R.add("upf.map", "ok" if not unmapped else "skip",
          f"SMF 的 UPF ↔ UPF 設定檔：{ {k: v[0] for k, v in m.items()} }" + (f"；對不到：{unmapped}" if unmapped else ""))
    cidr_of = {k: [x.get("cidr") for x in lst(d.get("dnnList")) if x.get("dnn") == want_dnn] for k, (n, d) in m.items()}
    if not pools:
        R.add("pool.smf", "error", f"SMF 沒有給 {fmt(want_s)}／{want_dnn} 任何 UE IP pool")
    # 每個 anchor 的 pool 要在它自己 UPF 的 cidr 裡（UPF 只替自己的 cidr 加 upfgtp 路由與 NAT）
    for k, ps in pools.items():
        if k not in cidr_of or not ps:
            continue
        bad = [p for p in ps if not covers(cidr_of[k], p)]
        R.add("pool.match", "error" if bad else "ok",
              f"UPF {k} 的 SMF pool {ps} {'不在' if bad else '在'} 它的 dnnList cidr {cidr_of[k]} 裡")

    # ---- ULCL ----
    mec = spec.get("mec")
    groups = []
    for n, d, _ in uerouting:
        for name, r in (d.get("ueRoutingInfo") or {}).items():
            groups.append((name, r))
    mine = [(name, r) for name, r in groups if want_supi in lst(r.get("members"))]
    has_sp = any(lst(r.get("specificPath")) for _, r in groups)
    ulcl_on = str(S.get("ulcl", "")).lower() == "true"
    if has_sp or mec:
        R.add("ulcl.enabled", "ok" if ulcl_on else "error",
              f"SMF ulcl = {S.get('ulcl', '（沒設，預設 false）')}" +
              ("" if ulcl_on else "：SMF 會忽略 uerouting 的 specificPath，全部流量走預設路徑（坑 A）"))
    if not mec:
        return
    if not mine:
        R.add("ulcl.members", "error", f"uerouting 沒有任何群組的 members 包含 {want_supi}")
        return
    if len(mine) > 1:
        R.add("ulcl.members", "warn", f"{want_supi} 出現在多個群組 {[n for n, _ in mine]}，只檢查第一個")
    gname, r = mine[0]
    R.add("ulcl.members", "ok", f"uerouting 群組 {gname} 包含 {want_supi}")
    others = [n for n, _ in groups if n != gname]
    if others:
        R.add("ulcl.extra_groups", "extra", f"uerouting 還有其他群組 {others}（chart 預設的 UE2 之類）")

    # 預設路徑（topology）：gNB → … → 預設 anchor
    topo = [(l.get("A"), l.get("B")) for l in lst(r.get("topology"))]
    adj = {}
    for a, b in topo:
        adj.setdefault(a, set()).add(b); adj.setdefault(b, set()).add(a)
    an = next((x for x in adj if x in ans), None)
    branching = sorted(adj.get(an, set()) & set(smf_upfs)) if an else []
    default_anchor = sorted(x for x in adj if x in smf_upfs and x not in branching and len(adj[x]) == 1)
    R.add("ulcl.default_path", "ok" if an and branching and default_anchor else "error",
          f"預設路徑：{an} → {branching} → {default_anchor}")
    ue_pools = [p for a in default_anchor for p in pools.get(a, [])]

    # specificPath：dest 要涵蓋 MEC IP
    sps = lst(r.get("specificPath"))
    hit = [sp for sp in sps if covers([sp.get("dest")], f'{mec["ip"]}/32')]
    R.add("ulcl.mec_dest", "ok" if hit else "error",
          f"群組 {gname} 的 specificPath dest {[sp.get('dest') for sp in sps] or '沒有'}"
          f"{'涵蓋' if hit else '不涵蓋'} MEC {mec['ip']}")
    more = [sp.get("dest") for sp in sps if sp not in hit]
    if more:
        R.add("ulcl.extra_dest", "extra", f"specificPath 還有其他目的地 {more}（chart 預設 1.0.0.1/32 之類）")
    links = {frozenset((l.get("A"), l.get("B"))) for l in lst(g(S, "userplaneInformation", "links"))}
    for sp in hit[:1]:
        path = lst(sp.get("path"))
        missing = [x for x in path if x not in smf_upfs]
        if missing or len(path) < 2:
            R.add("ulcl.path", "error", f"specificPath 的 path {path} 有不存在的 UPF {missing} 或少於 2 個節點")
            continue
        psa = path[-1]
        problems = []
        if branching and path[0] not in branching:
            problems.append(f"第一個節點 {path[0]} 不是接 gNB 的 UPF {branching}")
        if psa in default_anchor:
            problems.append(f"最後一個節點 {psa} 就是預設 anchor（MEC 流量沒有分出去）")
        unlinked = [f"{a}-{b}" for a, b in zip(path, path[1:]) if frozenset((a, b)) not in links]
        if unlinked:
            problems.append(f"SMF links 沒有 {unlinked}")
        R.add("ulcl.path", "error" if problems else "ok", f"specificPath {path}" + ("：" + "；".join(problems) if problems else ""))
        # 坑 C：UE 的 IP 從預設 anchor 的 pool 分配，MEC 的 PSA 要有回 UE 的路由（它的 cidr 要涵蓋 UE pool）
        if psa in cidr_of and ue_pools:
            bad = [p for p in ue_pools if not covers(cidr_of[psa], p)]
            R.add("ulcl.psa_route", "error" if bad else "ok",
                  f"MEC PSA {psa} 的 dnnList cidr {cidr_of[psa]} {'不涵蓋' if bad else '涵蓋'} UE 的 pool {ue_pools}"
                  + ("：PSA 沒有回 UE 的 upfgtp 路由，MEC 連不到（坑 C）" if bad else ""))
        elif psa not in cidr_of:
            R.add("ulcl.psa_route", "skip", f"找不到 {psa} 的 UPF 設定檔")
        # 坑 B：free5GC v4.3.0 SMF 的 SelectPSA2 檢查 PSA2 有沒有 N3（上游 a510e92 才修）
        if str(spec.get("free5gc_version", "")) == "v4.3.0":
            n3 = any(i.get("interfaceType") == "N3" for i in lst(smf_upfs[psa].get("interfaces")))
            R.add("ulcl.psa2_n3", "ok" if n3 else "error",
                  f"MEC PSA {psa} {'有' if n3 else '沒有'} N3 interface" +
                  ("" if n3 else "：v4.3.0 SMF 的 SelectPSA2 會因此跳過這條路徑（DNN mismatch，坑 B；"
                   "要嘛在 SMF 設定給它 N3，要嘛換上含 free5gc/smf a510e92 的 SMF）"))


def check_subs(R, subs, ues, want_supi, want_s, want_dnn):
    """subs：{auth: [...], am: [...], sm: [...]}，由 dataflow／probe 從 MongoDB 匯出"""
    auth = [x for x in subs.get("auth", []) if x.get("ueId") == want_supi]
    am = [x for x in subs.get("am", []) if x.get("ueId") == want_supi]
    sm = [x for x in subs.get("sm", []) if x.get("ueId") == want_supi]
    R.add("subs.count", "info", f"UDR 共有 {len(subs.get('auth', []))} 個 subscriber")
    if len(subs.get("auth", [])) > 1:
        R.add("subs.extra", "extra", f"除了 {want_supi} 還有 {[x.get('ueId') for x in subs['auth'] if x.get('ueId') != want_supi]}")
    if not auth:
        R.add("subs.exists", "error", f"UDR 沒有 {want_supi} 的認證資料")
        return
    a = auth[0]
    k = str(g(a, "encPermanentKey") or g(a, "permanentKey", "permanentKeyValue") or "").lower()
    opc = str(g(a, "encOpcKey") or g(a, "opc", "opcValue") or "").lower()
    for n, d, _ in ues:
        if d.get("supi") != want_supi:
            continue
        R.add("subs.k", "ok" if k == str(d.get("key", "")).lower() else "error", f"K 和 UE {n} {'一致' if k == str(d.get('key','')).lower() else '不一致'}")
        if str(d.get("opType", "")).upper() == "OPC":
            same = opc == str(d.get("op", "")).lower()
            R.add("subs.opc", "ok" if same else "error", f"OPc 和 UE {n} {'一致' if same else '不一致'}")
    if am:
        nssai = g(am[0], "nssai", default={})
        ss = [snssai(x) for x in lst(nssai.get("defaultSingleNssais")) + lst(nssai.get("singleNssais"))]
        R.compare_set("subs.nssai", "訂閱的 S-NSSAI", ss, [want_s], "UDR amData")
    if sm:
        found = []
        for x in sm:
            s = snssai(x.get("singleNssai"))
            for dn in (x.get("dnnConfigurations") or {}):
                found.append(f"{fmt(s)} {dn}")
        R.compare_set("subs.dnn", "訂閱的 slice＋DNN", found, [f"{fmt(want_s)} {want_dnn}"], "UDR smData")


def parse_services(path):
    """cluster-state.txt 的 `kubectl get svc -A -o wide` 段落 → {name: 'port/proto,...'}"""
    out = {}
    try:
        for line in open(path):
            m = re.match(r"\S+\s+service/(\S+)\s+\S+\s+\S+\s+\S+\s+(\S+)", line)
            if m:
                out[m.group(1)] = m.group(2)
    except FileNotFoundError:
        return None
    return out or None


SYM = {"ok": "  ok ", "error": "ERROR", "extra": "extra", "warn": " warn", "info": " info", "skip": " skip"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("configmaps")
    ap.add_argument("spec")
    ap.add_argument("--subs")
    ap.add_argument("--services")
    ap.add_argument("-q", action="store_true", help="摘要只列 ok 以外的項目")
    a = ap.parse_args()
    spec = json.load(open(a.spec)).get("expect", {})
    subs = json.load(open(a.subs)) if a.subs else None
    services = parse_services(a.services) if a.services else None
    r = run(load_configs(a.configmaps), spec, subs, services)
    for c in r["checks"]:
        if a.q and c["status"] == "ok":
            continue
        print(f"[{SYM[c['status']]}] {c['id']:22} {c['msg']}", file=sys.stderr)
    print(f"V5 {'PASS' if r['pass'] else 'FAIL'}  exact={'yes' if r['exact'] else 'no'}  {r['counts']}", file=sys.stderr)
    print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
