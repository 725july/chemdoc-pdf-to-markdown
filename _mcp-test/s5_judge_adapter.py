# -*- coding: utf-8 -*-
"""S5 视觉判定后端适配器（SPEC v2 §4.2 / U-09）。

把"**谁来判**"与"**判什么**"解耦：换模型（本 agent / 豆包 / 其它 VLM）= 改配置，不改代码。

用法:
  python -X utf8 s5_judge_adapter.py backends
  python -X utf8 s5_judge_adapter.py replay --run <run dir> --verdicts <verdicts.jsonl> --config <json>
  python -X utf8 s5_judge_adapter.py agent  --run <run dir> --config <json>          # 生成待判作业包
  python -X utf8 s5_judge_adapter.py http   --run <run dir> --config <json> [--dry-run]
  python -X utf8 s5_judge_adapter.py selftest

配置（judge-config.json）示例：
  { "backend": "http_vision", "model": "doubao-…", "endpoint": "https://…/v1/chat/completions",
    "api_key_env": "DOUBAO_API_KEY", "params": { "max_tokens": 2048, "temperature": 0 } }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from contracts import VERDICTS_S3

DEFAULT_CONFIG = {"backend": "replay", "model": "deepseek-v4.1f (in-session agent)",
                  "endpoint": None, "api_key_env": None, "params": {}}
PROMPT_VERSION = "s5-pilot-v1"


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        cfg.update(json.load(open(path, encoding="utf-8")))
    return cfg


def build_request(cfg: dict, page_item: dict, crops: list[str], images_b64: list[str]) -> dict:
    """纯函数：把一页的待判项装配成后端请求体。换后端只需改 cfg（可自测）。"""
    lines = [f"你在为化学教材 PDF 的转录产物做**存在性判定**。只回答问题：**这在页面上存在吗？**",
             f"页面：pdf_page={page_item.get('pdf_page')}  渲染={page_item.get('render')}",
             f"判定词表：{', '.join(VERDICTS_S3)}（no_content_diff = 按 D2 不算内容差异）。",
             f"必须为每一项给出 {', '.join(VERDICTS_S3)} 之一 + 0–1 置信度 + 你在图上看到的依据。",
             "输出 JSON 数组，元素形如 {\"item_id\":…,\"verdict\":…,\"confidence\":0.0,\"what_i_see\":\"…\"}。",
             "", "待判项："]
    for it in page_item.get("items", []):
        lines.append(json.dumps({k: it.get(k) for k in
                                 ("item_id", "operation", "bbox", "crop", "claim", "span_diff")},
                                ensure_ascii=False))
    content = [{"type": "text", "text": "\n".join(lines)}]
    for b64 in images_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    return {
        "model": cfg["model"],
        "temperature": cfg.get("params", {}).get("temperature", 0),
        "max_tokens": cfg.get("params", {}).get("max_tokens", 2048),
        "prompt_version": PROMPT_VERSION,
        "messages": [{"role": "user", "content": content}],
        "_meta": {"crops": crops, "backend": cfg.get("backend")},
    }


def read_packets(run: str) -> list[dict]:
    pk = os.path.join(run, "packets")
    if not os.path.isdir(pk):
        return []
    out = []
    for name in sorted(os.listdir(pk)):
        if name.endswith(".packet.json"):
            out.append(json.load(open(os.path.join(pk, name), encoding="utf-8")))
    return out


def write_verdicts(path: str, rows: list[dict], cfg: dict, mode: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for v in rows:
            fh.write(json.dumps({**v, "judge": {"model": cfg["model"], "mode": mode,
                                                "prompt_version": PROMPT_VERSION}},
                                ensure_ascii=False, sort_keys=True) + "\n")


# ------------------------------------------------------------------ backends
def backend_replay(args) -> int:
    cfg = load_config(args.config)
    if not args.verdicts or not os.path.exists(args.verdicts):
        print("replay 需要 --verdicts <已判定 jsonl>（离线回放桩）", file=sys.stderr)
        return 2
    rows = [json.loads(l) for l in open(args.verdicts, encoding="utf-8") if l.strip()]
    out = os.path.join(args.run, "verdicts.replay.jsonl")
    write_verdicts(out, [{k: r[k] for k in ("item_id", "verdict", "confidence", "evidence") if k in r}
                         for r in rows], cfg, "replay")
    print(json.dumps({"backend": "replay", "items": len(rows), "out": out,
                      "model": cfg["model"]}, ensure_ascii=False, indent=2))
    return 0


def backend_agent(args) -> int:
    """本 agent 在会话内判定：只生成待判作业包（判定结果由 agent 写入 verdicts.<arm>.jsonl）。"""
    cfg = load_config(args.config)
    packets = read_packets(args.run)
    jobs = {"backend": "agent", "model": cfg["model"], "prompt_version": PROMPT_VERSION,
            "pages": [{"pdf_page": p["pdf_page"], "render": p["render"],
                       "item_count": p["item_count"],
                       "items": [{k: it.get(k) for k in ("item_id", "operation", "bbox", "crop",
                                                         "claim", "span_diff")}
                                 for it in p["items"]]} for p in packets]}
    out = os.path.join(args.run, "judge-jobs.agent.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(jobs, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"backend": "agent", "pages": len(packets),
                      "items": sum(p["item_count"] for p in packets), "out": out},
                     ensure_ascii=False, indent=2))
    return 0


def backend_http(args) -> int:
    cfg = load_config(args.config)
    packets = read_packets(args.run)
    key_env = cfg.get("api_key_env")
    have_key = bool(key_env and os.environ.get(key_env))
    requests_ = [build_request(cfg, p, [it.get("crop") for it in p["items"]], []) for p in packets]
    out = os.path.join(args.run, "judge-requests.http.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"_meta": {"endpoint": cfg.get("endpoint"), "api_key_env": key_env,
                             "key_present": have_key, "model": cfg["model"]},
                   "requests": requests_}, fh, ensure_ascii=False, indent=2)
    if args.dry_run or not have_key:
        print(json.dumps({"backend": "http_vision", "model": cfg["model"],
                          "endpoint": cfg.get("endpoint"), "api_key_env": key_env,
                          "key_present": have_key, "requests_built": len(requests_), "out": out,
                          "note": "未发送请求（--dry-run 或缺凭证）——U-09 的凭证到位后去掉 "
                                  "--dry-run 即可发送"}, ensure_ascii=False, indent=2))
        return 0 if have_key else 3
    data = json.dumps(requests_[0]).encode("utf-8")  # 真实发送留待凭证到位后逐页执行
    req = urllib.request.Request(cfg["endpoint"], data=data,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {os.environ[key_env]}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    print(json.dumps({"backend": "http_vision", "first_page_reply_keys": list(body.keys())},
                     ensure_ascii=False, indent=2))
    return 0


def selftest() -> int:
    """证明"换后端 = 改配置"，并锁住请求体形状。"""
    cases, passed = [], 0
    base = {"backend": "http_vision", "model": "deepseek-v4.1f-vision",
            "endpoint": "https://api.example.com/v1/chat/completions",
            "api_key_env": "DEEPSEEK_API_KEY", "params": {"temperature": 0, "max_tokens": 1024}}
    swapped = {**base, "model": "doubao-vision-pro", "endpoint": "https://ark.example.com/v1/chat",
               "api_key_env": "DOUBAO_API_KEY", "params": {"temperature": 0, "max_tokens": 2048}}
    page = {"pdf_page": 3, "render": "renders/pages/page_0003.png",
            "items": [{"item_id": "SCI-0007", "operation": None, "bbox": [1009, 2026, 1221, 2077],
                       "crop": "crops/SCI-0007.png", "claim": {"claim": "首先,温度 T1 不变"}}]}
    r1, r2 = build_request(base, page, ["crops/SCI-0007.png"], []), build_request(swapped, page, [], [])

    cases.append(("换后端只改配置：model 生效", r1["model"] == "deepseek-v4.1f-vision"
                  and r2["model"] == "doubao-vision-pro"))
    cases.append(("换后端只改配置：max_tokens 生效", r1["max_tokens"] == 1024 and r2["max_tokens"] == 2048))
    cases.append(("请求体带 item_id 与 claim", "SCI-0007" in json.dumps(r1, ensure_ascii=False)
                  and "首先" in json.dumps(r1, ensure_ascii=False)))
    cases.append(("请求体带判定词表", all(v in json.dumps(r1, ensure_ascii=False) for v in VERDICTS_S3)))
    cases.append(("请求体带 prompt_version", r1["prompt_version"] == PROMPT_VERSION))
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rc = backend_http(argparse.Namespace(run=td, config=None, dry_run=True))
    cases.append(("无凭证时 http 后端不发送且退出码为 3", rc == 3))
    for name, ok in cases:
        print(("PASS  " if ok else "FAIL  ") + name)
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} adapter tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("replay", backend_replay), ("agent", backend_agent)):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        p.add_argument("--config")
        if name == "replay":
            p.add_argument("--verdicts", required=True)
        p.set_defaults(func=fn)
    h = sub.add_parser("http")
    h.add_argument("--run", required=True); h.add_argument("--config"); h.add_argument("--dry-run", action="store_true")
    h.set_defaults(func=backend_http)
    sub.add_parser("backends").set_defaults(
        func=lambda a: print(json.dumps({"available": ["replay", "agent", "http_vision"],
                                         "default": DEFAULT_CONFIG}, ensure_ascii=False, indent=2)) or 0)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
