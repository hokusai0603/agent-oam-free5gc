#!/usr/bin/env python3
"""V5 多 UPF／ULCL 的反向測試：拿 L2 參考答案的設定（2026-11-20 從 VM 匯出），每次故意改錯一處，確認檢查抓得到。

    python3 verify/tests/test_consistency_l2.py

L2 參考答案留著 chart 預設的多餘 slice／PLMN（extra），所以基準只要求 pass（沒有 error），不要求 exact。
"""
import json
import os
import re
import sys
import tempfile

import yaml

R = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(R, "verify"))
import consistency as C  # noqa: E402

FIX = os.path.join(R, "verify/tests/fixtures/l2-ref")
SPEC = json.load(open(os.path.join(R, "tasks/L2/spec.json")))["expect"]

N3_PSA2 = ("        interfaces:\n          - interfaceType: N3\n            endpoints:\n              - PSAUPF2_SERVICE_NAME\n"
           "            networkInstances:\n              - internet\n          - interfaceType: N9\n            endpoints:\n"
           "              - PSAUPF2_SERVICE_NAME")
# (名稱, 檔名, ConfigMap 名稱要包含的字（None＝不限）, 原字串, 新字串, 期望的 (id, status)；None＝不該有 error)
MUT = [
    ("坑 A：沒有 ulcl: true", "smfcfg.yaml", None, "  ulcl: true\n", "", ("ulcl.enabled", "error")),
    ("坑 B：AnchorUPF2 沒有 N3", "smfcfg.yaml", None, N3_PSA2,
     "        interfaces:\n          - interfaceType: N9\n            endpoints:\n              - PSAUPF2_SERVICE_NAME", ("ulcl.psa2_n3", "error")),
    ("坑 C：psaupf2 的 cidr 不含 UE pool", "upfcfg.yaml", "psaupf2", "cidr: 10.60.0.0/16", "cidr: 10.60.128.0/17",
     ("ulcl.psa_route", "error")),
    ("導流目的地照抄 chart 預設 1.0.0.1", "uerouting.yaml", None, "dest: 10.43.200.10/32", "dest: 1.0.0.1/32", ("ulcl.mec_dest", "error")),
    ("群組沒有規格的 SUPI", "uerouting.yaml", None, "imsi-208930000000001", "imsi-208930000007487", ("ulcl.members", "error")),
    ("specificPath 走回預設 anchor", "uerouting.yaml", None, "path: [BranchingUPF, AnchorUPF2]", "path: [BranchingUPF, AnchorUPF1]",
     ("ulcl.path", "error")),
    ("specificPath 用到不存在的 UPF", "uerouting.yaml", None, "path: [BranchingUPF, AnchorUPF2]", "path: [BranchingUPF, AnchorUPF3]",
     ("topo.uerouting", "error")),
    ("specificPath 不從接 gNB 的 UPF 開始", "uerouting.yaml", None, "path: [BranchingUPF, AnchorUPF2]", "path: [AnchorUPF1, AnchorUPF2]",
     ("ulcl.path", "error")),
    ("AnchorUPF1 的 SMF pool 不在它的 cidr", "smfcfg.yaml", None, "cidr: 10.60.0.0/17", "cidr: 10.61.0.0/17", ("pool.match", "error")),
    ("留著 chart 的 UE2 群組", "uerouting.yaml", None, "    specificPath:\n",
     "    specificPath:\n", ("ulcl.extra_groups", "extra")),   # 由 EXTRA_UE2 另外處理
    ("（不該報錯）導流目的地寫成 /24", "uerouting.yaml", None, "dest: 10.43.200.10/32", "dest: 10.43.200.0/24", None),
]
EXTRA_UE2 = ("  UE2:\n    members:\n      - imsi-208930000000004\n    topology:\n      - A: gNB1\n        B: BranchingUPF\n"
             "      - A: BranchingUPF\n        B: AnchorUPF1\n    specificPath:\n      - dest: 1.0.0.1/32\n        path: [BranchingUPF, AnchorUPF2]\n")


def load_docs():
    t = re.sub(r"(?m)^apiVersion:", "---\napiVersion:", open(os.path.join(FIX, "configmaps.yaml")).read())
    return [d for d in yaml.safe_load_all(t) if d]


def run(docs, subs=None):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump_all(docs, f, allow_unicode=True)
    f.close()
    try:
        return C.run(C.load_configs(f.name), SPEC, subs, C.parse_services(os.path.join(FIX, "cluster-state.txt")))
    finally:
        os.unlink(f.name)


def main():
    subs = json.load(open(os.path.join(FIX, "subs.json")))
    base = run(load_docs(), subs)
    errs = [c["msg"] for c in base["checks"] if c["status"] == "error"]
    print(f"基準（L2 參考答案）：pass={base['pass']} exact={base['exact']} {base['counts']}")
    fails = 0 if base["pass"] else 1
    if errs:
        print(f"[FAIL] 基準有 error：{errs}")
    for name, fname, cmsub, old, new, expect in MUT:
        docs = load_docs()
        hit = 0
        for doc in docs:
            for cm in doc.get("items", []):
                data = cm.get("data") or {}
                if fname in data and (cmsub is None or cmsub in cm["metadata"]["name"]) and old in data[fname]:
                    data[fname] = (data[fname] + EXTRA_UE2) if "UE2" in name else data[fname].replace(old, new, 1)
                    hit += 1
        if hit != 1:
            print(f"[SETUP?] {name}：要改的字串出現 {hit} 次"); fails += 1; continue
        r = run(docs, subs)
        got = {(c["id"], c["status"]) for c in r["checks"]}
        if expect is None:
            ok = r["pass"]
            detail = "沒有誤報" if ok else f"誤報：{[c['msg'] for c in r['checks'] if c['status'] == 'error']}"
        else:
            ok = expect in got
            detail = next((c["msg"] for c in r["checks"] if (c["id"], c["status"]) == expect), f"沒抓到，結果 {r['counts']}")
            others = [c["id"] for c in r["checks"] if c["status"] == "error" and (c["id"], c["status"]) != expect]
            if others:
                detail += f"（另外的 error：{others}）"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}：{detail}")
        fails += not ok
    n = len(MUT) + 1
    print(f"{n - fails}/{n} 通過")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
