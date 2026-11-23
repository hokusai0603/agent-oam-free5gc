#!/usr/bin/env python3
"""V5 的反向測試：拿一個全部正確的 trial（phase1-r2 Haiku 3），每次故意改錯一處，確認檢查抓得到。

    python3 verify/tests/test_consistency.py
"""
import copy
import json
import os
import sys
import tempfile

import yaml

R = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(R, "verify"))
import consistency as C  # noqa: E402

BASE = os.path.join(R, "results/phase1-r2/L1-normal-haiku-5-5-high-20261119-144822")
SPEC = json.load(open(os.path.join(R, "tasks/L1/spec.json")))["expect"]

# (名稱, 要改的檔案, 原字串, 新字串, 期望出現的 (id, status))
MUT = [
    ("AMF slice 的 SD 打錯", "amfcfg.yaml", "sd: 010203", "sd: 010204", ("snssai.amf", "error")),
    ("AMF 多一個 slice", "amfcfg.yaml", "        - sst: 1\n          sd: 010203",
     "        - sst: 1\n          sd: 010203\n        - sst: 1\n          sd: 112233", ("snssai.amf", "extra")),
    ("SMF 的 UE pool 和 UPF 不同", "smfcfg.yaml", "cidr: 10.60.0.0/16", "cidr: 10.61.0.0/16", ("pool.match", "error")),
    ("SMF 的 slice 打錯", "smfcfg.yaml", "    - sNssai:\n        sst: 1\n        sd: 010203",
     "    - sNssai:\n        sst: 1\n        sd: 000001", ("snssai.smf", "error")),
    ("SMF 的 PLMN 打錯", "smfcfg.yaml", 'mnc: "93"', 'mnc: "94"', ("plmn", "error")),
    ("SMF 的 link 指到不存在的節點", "smfcfg.yaml", "      - A: gNB1\n        B: UPF", "      - A: gNB1\n        B: UPF2",
     ("topo.link_node", "error")),
    ("UPF 的 DNN 打錯", "upfcfg.yaml", "dnn: internet", "dnn: internet2", ("dnn.upf", "error")),
    ("gNB 的 TAC 不在 AMF", "gnb-config.yaml", "tac: 1 ", "tac: 2 ", ("tac.gnb_in_amf", "error")),
    ("gNB 的 slice 打錯", "gnb-config.yaml", "sd: 0x010203", "sd: 0x010204", ("snssai.gnb", "error")),
    ("gNB 連錯 AMF Service", "gnb-config.yaml", "address: free5gc-free5gc-amf-amf-n2", "address: amf-n2-typo",
     ("ref.gnb_amf", "error")),
    ("UE 的 MNC 打錯", "ue-config.yaml", 'mnc: "93"', 'mnc: "093"', ("plmn", "error")),
    ("UE 的 APN 打錯", "ue-config.yaml", 'apn: "internet"', 'apn: "ims"', ("dnn.ue", "error")),
    ("UE 的 SUPI 不是規格", "ue-config.yaml", "imsi-208930000000001", "imsi-208930000000002", ("supi.ue", "error")),
    ("AMF 只接受 UE 不支援的完整性演算法", "amfcfg.yaml", "      - NIA2", "      - NIA9", ("sec.integrity", "error")),
    ("AMF 的 nrfUri 和其他 NF 不同", "amfcfg.yaml", "nrfUri: http://nrf-nnrf:8000", "nrfUri: http://nrf-other:8000",
     ("ref.nrf", "error")),
    # 正規化：同一個值的不同寫法不能誤報
    ("（不該報錯）UE 的 SD 改寫成十進位", "ue-config.yaml", "      sd: 0x010203", "      sd: 66051", None),
]


# subscriber（UDR）資料的反向測試：(名稱, 改法, 期望的 (id, status))
SUBS = os.path.join(R, "verify/tests/fixtures/subs-l1-haiku3.json")   # 2026-11-20 從 VM 上的 Haiku 3 部署匯出
SUB_MUT = [
    ("subscriber 的 K 和 UE 不同", lambda s: s["auth"][0].update(encPermanentKey="00" * 16), ("subs.k", "error")),
    ("subscriber 的 OPc 和 UE 不同", lambda s: s["auth"][0].update(encOpcKey="11" * 16), ("subs.opc", "error")),
    ("subscriber 訂閱的 slice 錯", lambda s: s["am"][0]["nssai"].update(defaultSingleNssais=[{"sst": 1, "sd": "112233"}], singleNssais=[]),
     ("subs.nssai", "error")),
    ("subscriber 多一個人", lambda s: s["auth"].append({**s["auth"][0], "ueId": "imsi-208930000000099"}), ("subs.extra", "extra")),
    ("沒有這個 SUPI 的 subscriber", lambda s: s.update(auth=[]), ("subs.exists", "error")),
]


def load_raw():
    return yaml.safe_load(open(os.path.join(BASE, "configmaps.yaml")))


def write(doc):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(doc, f, allow_unicode=True)
    f.close()
    return f.name


def run(doc):
    p = write(doc)
    try:
        return C.run(C.load_configs(p), SPEC, None, C.parse_services(os.path.join(BASE, "cluster-state.txt")))
    finally:
        os.unlink(p)


def main():
    base = run(load_raw())
    fails = 0
    print(f"基準：pass={base['pass']} exact={base['exact']} {base['counts']}")
    if not base["exact"]:
        print("基準本身不是全對，無法做反向測試"); sys.exit(1)
    for name, fname, old, new, expect in MUT:
        doc = load_raw()
        hit = 0
        for cm in doc["items"]:
            data = cm.get("data") or {}
            if fname in data and old in data[fname]:
                data[fname] = data[fname].replace(old, new, 1)
                hit += 1
        if not hit:
            print(f"[SETUP?] {name}：找不到要改的字串"); fails += 1; continue
        r = run(doc)
        got = {(c["id"], c["status"]) for c in r["checks"]}
        if expect is None:
            ok = r["exact"]
            detail = "沒有誤報" if ok else f"誤報：{[c['msg'] for c in r['checks'] if c['status'] in ('error', 'extra')]}"
        else:
            ok = expect in got
            detail = next((c["msg"] for c in r["checks"] if (c["id"], c["status"]) == expect),
                          f"沒抓到，結果 {r['counts']}")
        print(f"[{'PASS' if ok else 'FAIL'}] {name}：{detail}")
        fails += not ok
    cfg = C.load_configs(os.path.join(BASE, "configmaps.yaml"))
    base_s = C.run(cfg, SPEC, json.load(open(SUBS)), None)
    sub_bad = [c["msg"] for c in base_s["checks"] if c["id"].startswith("subs") and c["status"] in ("error", "extra")]
    print(f"subscriber 基準：{'沒有問題' if not sub_bad else sub_bad}")
    fails += bool(sub_bad)
    for name, fn, expect in SUB_MUT:
        sb = json.load(open(SUBS))
        fn(sb)
        r = C.run(cfg, SPEC, sb, None)
        hit = next((c["msg"] for c in r["checks"] if (c["id"], c["status"]) == expect), None)
        print(f"[{'PASS' if hit else 'FAIL'}] {name}：{hit or '沒抓到'}")
        fails += not hit
    n = len(MUT) + len(SUB_MUT) + 1
    print(f"{n - fails}/{n} 通過")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
