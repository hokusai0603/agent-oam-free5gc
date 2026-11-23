#!/usr/bin/env python3
"""把一個 trial 目錄整理成一行 JSON 紀錄（印到 stdout）。

argv: out_dir task prompt model effort timeout_min budget auth snapshot exit_code
"""
import collections
import hashlib
import json
import os
import re
import sys

out, task, prompt, model, effort, timeout_min, budget, auth, snap, exit_code = sys.argv[1:11]
trial_id = os.path.basename(out.rstrip("/"))


def load_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return rows


events = load_jsonl(os.path.join(out, "transcript.jsonl"))
# -p 模式下，agent 的背景工作（run_in_background、Monitor）結束時會再喚醒 agent，
# 一個 trial 會有多個 result 事件：total_cost_usd 是累計值；num_turns、duration_ms 是每段各自的值。
results = [e for e in events if e.get("type") == "result"]
result = results[-1] if results else {}
init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})

models_seen = collections.Counter()
tool_calls = collections.Counter()
stop_reasons = collections.Counter()
bash_cmds = 0
for e in events:
    if e.get("type") != "assistant":
        continue
    msg = e.get("message", {})
    if msg.get("model"):
        models_seen[msg["model"]] += 1
    if msg.get("stop_reason"):
        stop_reasons[msg["stop_reason"]] += 1
    for block in msg.get("content", []) or []:
        if block.get("type") == "tool_use":
            tool_calls[block.get("name")] += 1
            if block.get("name") == "Bash":
                bash_cmds += 1

times = open(os.path.join(out, "agent-times.txt")).read() if os.path.exists(os.path.join(out, "agent-times.txt")) else ""
m = re.search(r"start=(\d+) end=(\d+)", times)
wall_s = int(m.group(2)) - int(m.group(1)) if m else None

try:
    verify = json.load(open(os.path.join(out, "verify.json")))
except (FileNotFoundError, ValueError):
    verify = {"pass": False, "error": "no verify.json"}

prompt_text = open(os.path.join(out, "prompt.md"), "rb").read()
expected_model_prefix = model
# "<synthetic>" 是 Claude Code 自己產生的訊息（例如 API 錯誤），不是換了模型
off_model = {k: v for k, v in models_seen.items()
             if not k.startswith(expected_model_prefix) and not k.startswith("<")}

# ---- 判斷 trial 是否因外部原因中斷（不能算成 agent 失敗）----
# 訂閱有用量上限：被限流切斷的 trial 標成無效、要重跑。
try:
    stderr_text = open(os.path.join(out, "agent-stderr.log"), errors="replace").read()
except FileNotFoundError:
    stderr_text = ""
# 只看真正的 API 錯誤來源；不能掃 agent 自己的指令或最終回報（5G 的回報一定會提到 authentication）
api_error_texts = [stderr_text]
for r in results:
    if r.get("is_error"):
        api_error_texts.append(str(r.get("result") or ""))
for e in events:
    # Claude Code 把 API 錯誤當成 assistant 訊息（isApiErrorMessage / error）放進 stream
    if e.get("type") == "assistant" and (e.get("isApiErrorMessage") or e.get("error")):
        api_error_texts.append(json.dumps(e.get("message", {}).get("content", ""), ensure_ascii=False))
        api_error_texts.append(str(e.get("error")))
    # system 事件只看名稱像錯誤／重試的（task_started 等事件會帶 agent 的指令內容，不能掃）
    if e.get("type") == "system" and re.search(r"error|retry|rate|limit", str(e.get("subtype")), re.I):
        api_error_texts.append(json.dumps(e, ensure_ascii=False)[:2000])
blob = "\n".join(api_error_texts)
RATE_LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached|limit will reset|resets? at|out of usage|"
                           r"\b429\b|overloaded|\b529\b|too many requests", re.I)
AUTH_RE = re.compile(r"invalid api key|authentication|unauthori[sz]ed|\b401\b|oauth token|please run /login|"
                     r"credit balance", re.I)
invalid_reasons = []
if off_model:
    invalid_reasons.append("off_model_messages")
if RATE_LIMIT_RE.search(blob):
    invalid_reasons.append("rate_limited")
if AUTH_RE.search(blob):
    invalid_reasons.append("auth_error")

def setup_untouched(out):
    """spec 的 setup（例如 L2 的 MEC 應用）在 agent 前後的 spec 是否相同；沒有 setup → None"""
    try:
        return open(os.path.join(out, "setup-before.txt")).read() == open(os.path.join(out, "setup-after.txt")).read()
    except FileNotFoundError:
        return None


# agent 對 VM／K3s 的變更（verify/sidefx.py）；舊的 trial 沒有基準 → None
try:
    side = json.load(open(os.path.join(out, "sidefx.json")))["summary"]
except (FileNotFoundError, ValueError, KeyError):
    side = {"infra_changes": None, "tooling_changes": None, "infra": [], "tooling": [], "note": "no baseline captured"}

def layer(fname):
    """V5／V6 的結果摘要：pass、exact、各狀態數量、error 與 extra 的項目"""
    try:
        r = json.load(open(os.path.join(out, fname)))
    except (FileNotFoundError, ValueError):
        return None
    bad = [f'{c["id"]}: {c["msg"]}' for c in r.get("checks", []) if c.get("status") in ("error", "extra")]
    return {"pass": r.get("pass"), "exact": r.get("exact"), "counts": r.get("counts"), "issues": bad[:30]}


record = {
    "trial_id": trial_id,
    "task": task,
    "prompt_variant": prompt,
    "prompt_sha256": hashlib.sha256(prompt_text).hexdigest()[:16],
    "model": model,
    "effort": effort,
    "harness": {"name": "claude-code", "version": init.get("claude_code_version"),
                "tools": init.get("tools"), "disallowed": ["WebSearch", "WebFetch"]},
    "limits": {"timeout_min": int(timeout_min), "budget_usd": float(budget)},
    "auth": auth,
    "start_snapshot": snap,
    "agent": {
        "exit_code": exit_code,
        "result_subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "num_turns": sum(r.get("num_turns") or 0 for r in results) if results else None,
        "result_segments": len(results),
        "cost_usd": result.get("total_cost_usd"),
        # 訂閱時這是 Claude Code 依 API 價格算的估計值，不是實際扣款
        "cost_is_estimate": auth != "api_key",
        "duration_ms": sum(r.get("duration_ms") or 0 for r in results) if results else None,
        "wall_s": wall_s,
        # usage 也是每段各自的值，加總起來
        "usage": ({k: sum((r.get("usage") or {}).get(k) or 0 for r in results)
                   for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
                  if results else None),
        "models_seen": dict(models_seen),
        "off_model_messages": off_model,
        "stop_reasons": dict(stop_reasons),
        "refusal": stop_reasons.get("refusal", 0) > 0,
        "tool_calls": dict(tool_calls),
        # 最長的一段通常才是完整回報；後面的段落常只是「背景工作結束了」
        "final_text": (max((r.get("result") or "" for r in results), key=len) if results else "")[:4000],
    },
    "verify": {
        "pass": verify.get("pass"),
        "v1": verify.get("v1", {}).get("pass"),
        "v2": verify.get("v2", {}).get("pass"),
        "v3": verify.get("v3", {}).get("pass"),
        "req": verify.get("req", {}).get("pass"),
        # V3x：經 uesimtun0 抓網頁、DNS、第一跳是 UPF（2026-11-19 起）。舊 trial 沒有 → None
        "v3x": verify.get("v3x", {}).get("pass"),
        "v3x_checks": {k: c.get("pass") for u in verify.get("v3x", {}).get("ues", [])[:1]
                       for k, c in u.get("checks", {}).items()},
        "pass_strict": verify.get("pass_strict"),
        # V4 MEC 路徑（L2，2026-11-23）：spec 有 expect.mec 才有，已併進 pass
        "v4": verify.get("v4", {}).get("pass") if "v4" in verify else None,
        "v4_checks": {k: c.get("pass") for u in verify.get("v4", {}).get("ues", [])[:1]
                      for k, c in u.get("checks", {}).items()},
        "setup_untouched": setup_untouched(out),
        "failed_req": [k for k, c in verify.get("req", {}).get("checks", {}).items() if not c.get("pass")],
        # V5 跨 NF 設定一致性、V6 NF 間 dataflow 與封包內容（2026-11-20 起）。只記錄，先不併進 pass
        "v5": layer("consistency.json"),
        "v6": layer("dataflow.json"),
    },
    # 只記錄，不影響 pass；infra 類的變更要人工判斷是否必要（E7）
    "side_effects": side,
    # 中途換模型、被限流、認證失敗 → 無效，要重跑，不能算成 agent 失敗
    "valid": not invalid_reasons,
    "invalid_reasons": invalid_reasons,
    "human_interventions": 0,        # headless，沒有人介入
    "error_class": None,             # 人工分析後填（E1–E8）
    "notes": "",
}
print(json.dumps(record, ensure_ascii=False))
