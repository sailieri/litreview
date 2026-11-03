#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""raw 与 normalized 文本对照审核：把同一篇的原始提取文本和规范化文本一起送判官模型。

判官（决策模型）地址/密钥/模型名读 config.yaml 的 workstation provider。
  - 输入：raw + norm 全文（各截到 250K 字符，防超长输入）
  - 审核点：完整性（norm 是否覆盖 raw 正文）、忠实性（幻觉/数字错误）、结构
  - 用途：确认规范化后的正文既没漏章节，也没编造内容

用法:
  python tools/doctor/audit_normalize.py raw --dry-run                  # 只打印清单
  python tools/doctor/audit_normalize.py raw --old                      # 按 OLD_DATES 日期筛选
  python tools/doctor/audit_normalize.py raw --only "Meyer"             # 指定关键词
  python tools/doctor/audit_normalize.py raw --all                      # 全量
  python tools/doctor/audit_normalize.py raw --workers 4

输出: data/audit_raw_norm_report.csv
"""
import argparse
import csv
import datetime
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.util.llm_client import load_config

from modules.paths import data_root, extracted_dir, normalized_dir  # noqa: E402
NORM_DIR = normalized_dir()
EXTRACT_DIR = extracted_dir()
DEFAULT_CSV = data_root() / "audit_raw_norm_report.csv"

# --old 筛选依据：规范化文件的修改日期白名单（YYYY-MM-DD）
OLD_DATES = ("", "")

AUDIT_PROMPT = """你是学术论文规范化质量审核员。下面同时给出【提取原文 raw】和【规范化文本 norm】。

【提取原文】（PDF 直接提取，含页眉页脚/参考文献/噪音）
---BEGIN RAW---
{raw}
---END RAW---

【规范化文本】（LLM 清洗输出）
---BEGIN NORM---
{norm}
---END NORM---

请审核：
1. 完整性：norm 是否覆盖 raw 正文主体（摘要/引言/方法/结果/讨论/结论）？是否在中途截断？末尾是否完整收束？
2. 忠实性：norm 有无编造 raw 没有的内容（幻觉）？有无明显数字/事实错误？
3. 结构：章节顺序和标题层级是否合理？

注意：参考文献、作者信息、致谢、CV 属于规范化应删除内容，缺失不算问题。

严格输出 JSON（不要任何其他文字）：
{{"ok": true/false, "completeness": 0到1的小数, "fidelity": 0到1的小数, "truncated": true/false, "missing_sections": ["..."], "issues": ["最多3条，每条40字以内"]}}"""


def is_old_file(path: Path) -> bool:
    mt = datetime.datetime.fromtimestamp(path.stat().st_mtime)
    return mt.strftime("%Y-%m-%d") in OLD_DATES


def load_stems() -> list[str]:
    return sorted(p.stem for p in NORM_DIR.glob("*.md"))


def find_raw(stem: str) -> Path | None:
    p = EXTRACT_DIR / f"{stem}_raw.md"
    if p.exists():
        return p
    cands = sorted(EXTRACT_DIR.glob(f"{stem}*"))
    return cands[0] if cands else None


def audit_one(stem: str) -> dict:
    norm = (NORM_DIR / f"{stem}.md").read_text(encoding="utf-8", errors="replace")
    raw_p = find_raw(stem)
    r = {"stem": stem, "norm_chars": len(norm), "raw_chars": 0}
    if raw_p is None:
        r["error"] = "raw 缺失"
        return r
    raw = raw_p.read_text(encoding="utf-8", errors="replace")
    r["raw_chars"] = len(raw)
    raw_clean = re.sub(r"--- Page \d+ ---\s*", "", raw).strip()

    # 输入保护：raw / norm 各截到 250K 字符，超出部分不送审（防超长输入吃满上下文）
    prompt = AUDIT_PROMPT.format(raw=raw_clean[:250_000], norm=norm[:250_000])
    try:
        cfg = load_config()
        p = cfg["providers"]["workstation"]
        base = p["base_url"].rstrip("/")
        key = p["api_key"]
        model = p["models"]["default"]
        resp = httpx.post(f"{base}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model,
                                "messages": [{"role": "user", "content": prompt}],
                                "max_tokens": 800, "temperature": 0.0, "stream": True,
                                "chat_template_kwargs": {"enable_thinking": False}},
                          timeout=600, trust_env=False)   # 不读环境变量代理，避免请求被本机代理改写
        resp.raise_for_status()
        parts = []
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                obj = json.loads(data)
                delta = obj["choices"][0].get("delta", {})
                piece = delta.get("content", "")
                if piece:
                    parts.append(piece)
            except json.JSONDecodeError:
                continue
        out = "".join(parts).strip()
        m = re.search(r"\{.*\}", out, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
            r.update(result)
        else:
            r["error"] = f"parse-fail: {out[:150]}"
    except Exception as e:
        r["error"] = f"LLM-error: {str(e)[:150]}"
    return r


def main():
    ap = argparse.ArgumentParser(description="raw+norm 对照审核")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--old", action="store_true",
                    help="只审修改日期落在 OLD_DATES 内的篇目")
    ap.add_argument("--only", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    args = ap.parse_args()

    stems = load_stems()
    if args.only:
        stems = [s for s in stems if args.only.lower() in s.lower()]
    elif args.old:
        stems = [s for s in stems if is_old_file(NORM_DIR / f"{s}.md")]
    elif not args.all:
        # 默认：按 OLD_DATES 日期筛选
        stems = [s for s in stems if is_old_file(NORM_DIR / f"{s}.md")]

    print(f"待审核 {len(stems)} 篇（raw+norm 全量对比，Qwen 本地 vLLM）")
    for s in stems:
        print(f"  {s[:75]}")
    if args.dry_run:
        print("\n[dry-run] 未调用 LLM。去掉 --dry-run 执行。")
        return

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(audit_one, s): s for s in stems}
        done = 0
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            if "error" in r:
                print(f"[{done:>2}/{len(stems)}] ERROR {r['error'][:80]} | {r['stem'][:45]}", flush=True)
            else:
                ok = "OK " if r.get("ok") is True else ("BAD" if r.get("ok") is False else "?  ")
                comp = r.get("completeness", "?")
                tr = "T" if r.get("truncated") else "."
                print(f"[{done:>2}/{len(stems)}] {ok} comp={comp} trunc={tr} {r['stem'][:45]}", flush=True)

    results.sort(key=lambda r: r["stem"])
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stem", "norm_chars", "raw_chars", "ok",
                                          "completeness", "fidelity", "truncated",
                                          "missing_sections", "issues", "error"])
        w.writeheader()
        for r in results:
            w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v)
                        for k, v in r.items()})
    print(f"\nCSV: {args.csv}")

    judged = [r for r in results if "ok" in r]
    ok_n = sum(1 for r in judged if r.get("ok") is True)
    bad_n = sum(1 for r in judged if r.get("ok") is False)
    trunc_n = sum(1 for r in judged if r.get("truncated"))
    err_n = sum(1 for r in results if "error" in r)
    print(f"\n===== 汇总（{len(judged)} 篇有判定，{err_n} 篇错误）=====")
    print(f"合格 ok: {ok_n} | 有问题: {bad_n} | 截断: {trunc_n}")
    for r in judged:
        if r.get("ok") is not True:
            print(f"\n  [{r['stem'][:65]}]")
            print(f"    comp={r.get('completeness')} fidelity={r.get('fidelity')} trunc={r.get('truncated')}")
            if r.get("missing_sections"):
                print(f"    缺: {r['missing_sections']}")
            for i in r.get("issues", [])[:3]:
                print(f"    - {i[:130]}")


if __name__ == "__main__":
    main()
