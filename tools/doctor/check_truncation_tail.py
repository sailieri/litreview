#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截断检测：对比【原文末尾】vs【规范化末尾】，判断规范化是否覆盖到正文结尾（可全量跑）。

检测目标：规范化输出在 max_tokens 耗尽时的静默截断——
输出停在中途但末尾可能"看起来正常"（恰好停在完整段落）。检测只看末尾：
- raw 最后 N 字符（正文结尾=讨论/结论区，忽略参考文献/水印）
- normalized 最后 M 字符
由判官模型判断规范化是否覆盖到了原文正文结尾。模型配置读 config.yaml 的 normalize 步骤。

用法:
  python tools/doctor/audit_normalize.py tail --control          # 对照验证（6 样本）
  python tools/doctor/audit_normalize.py tail                    # 全量
  python tools/doctor/audit_normalize.py tail --only Meyer       # 只看某篇
"""
import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.paths import data_root, extracted_dir, normalized_dir  # noqa: E402
NORM_DIR = normalized_dir()
EXTRACT_DIR = extracted_dir()

sys.path.insert(0, str(ROOT))
from modules.util.llm_client import get_step_config, call_llm, extract_json

RAW_TAIL = 8000    # 原文末尾取多少字符
NORM_TAIL = 4000   # 规范化末尾取多少字符

PROMPT = """你是一名论文完整性检查员。论文在做规范化（LLM清洗）时，可能因为输出长度限制而【静默截断】：输出在中途停止，但不会报错，末尾看起来可能正常（比如恰好停在一个完整段落的末尾）。

下面给出同一篇论文的两段文本：
【原文末尾】= PDF提取文本的最后一段。可能包含参考文献、致谢、附录、页眉页脚或水印。
【规范化末尾】= 清洗后文本的最后一段。

重要：规范化会【有意识地删除】参考文献、致谢、附录、作者贡献声明、作者信息、页眉页脚、DOI等——这些缺失是【正常行为】，绝不能当作截断。

请判断：【规范化文本】是否覆盖到了【原文正文的结尾】（即讨论/结论部分的实质内容）？
- truncated=false：规范化末尾与原文正文结尾（讨论/结论）的实质内容对应，覆盖完整。即使它不含致谢/参考文献/附录/作者声明，也是正常的。
- truncated=true：规范化文本停在原文正文的【中途】（如方法/结果/讨论的中间某处），而原文正文的结尾部分（后续的讨论/结论）完全没有被覆盖。
- 如果原文本身没有明显的讨论/结论结构（如标准文件、报告、政策文件），以规范化是否覆盖了原文最后一段实质性正文内容为准。

只输出JSON：{"truncated": true或false, "confidence": 0到1, "reason": "≤20字"}"""


def build_prompt(raw: str, norm: str) -> str:
    return (PROMPT
            + f"\n\n【原文末尾】\n---\n{raw[-RAW_TAIL:]}\n---\n"
            + f"\n【规范化末尾】\n---\n{norm[-NORM_TAIL:]}\n---\n")


def check_one(stem: str, norm_override: str | None = None) -> dict:
    """norm_override 用于人为构造截断样本（对照验证用）。"""
    r = {"stem": stem, "error": None}
    norm_path = NORM_DIR / f"{stem}.md"
    raw_path = EXTRACT_DIR / f"{stem}_raw.md"
    if not norm_path.exists() or not raw_path.exists():
        r["error"] = f"missing norm={norm_path.exists()} raw={raw_path.exists()}"
        return r
    raw = raw_path.read_text(encoding="utf-8", errors="replace")
    norm = norm_override if norm_override is not None else norm_path.read_text(
        encoding="utf-8", errors="replace")
    r["norm_chars"] = len(norm)
    cfg = get_step_config("normalize")
    prompt = build_prompt(raw, norm)
    try:
        text = call_llm(
            [{"role": "system", "content": "你是一名严谨的论文质量检查员。只输出JSON。"},
             {"role": "user", "content": prompt}],
            cfg, max_tokens=200, temperature=0.0)
        data = extract_json(text)
        if not isinstance(data, dict):
            r["error"] = f"JSON解析失败: {text[:200]}"
            return r
        r["truncated"] = data.get("truncated")
        r["confidence"] = data.get("confidence")
        r["reason"] = str(data.get("reason", ""))[:60]
    except Exception as e:
        r["error"] = str(e)[:200]
    return r


def load_stems() -> list[str]:
    return sorted(p.stem for p in NORM_DIR.glob("*.md"))


def run_controls() -> list[dict]:
    """对照验证：检查检测器对 max_tokens 静默截断是否敏感。

    标准对照（计入通过率）：2 个阴性（完整文本）+ 2 个阳性（人为截断到 60%）。
    边界样本（只观察，不计入通过率）：乱序排版的博士论文、源文件本身残缺的论文——
    两者的末尾都不代表正文结尾，不能反映管线截断。
    """
    thormeyer = "Thormeyer 等 - 2020 - Spatially-explicit models should consider real-world diffusion of renewable electricity solar PV ex"
    du = next(p.stem for p in NORM_DIR.glob("Du 等 - 2025 - Maize crop residue*.md"))
    meyer = "Meyer - 2016 - Spatially explicit environmental assessment of global biomass production for bioenergy at the region"
    schierbaum = "Schierbaum - 2022 - A Bayesian Hierarchical Model for Remote Sensing Analyses with Categorical Outcomes"

    controls = [
        ("阴性-完整 Thormeyer", thormeyer, False, False),
        ("阴性-完整 Du2025", du, False, False),
        ("阳性-人为截断 Thormeyer", thormeyer, True, True),
        ("阳性-人为截断 Du2025", du, True, True),
        ("边界-乱序博士论文 Meyer2016", meyer, None, None),
        ("边界-源文件残缺 Schierbaum", schierbaum, None, None),
    ]
    results = []
    for label, stem, truncated_override, expected in controls:
        norm_path = NORM_DIR / f"{stem}.md"
        if truncated_override is True:
            norm_full = norm_path.read_text(encoding="utf-8", errors="replace")
            override = norm_full[: int(len(norm_full) * 0.6)]
        else:
            override = None
        r = check_one(stem, override)
        r["label"] = label
        r["expected"] = expected
        results.append(r)
        if expected is None:
            print(f"  [观察] {label}: truncated={r.get('truncated')} "
                  f"conf={r.get('confidence')} reason={r.get('reason')}")
        else:
            verdict = "✅" if r.get("truncated") == expected else "❌"
            print(f"  [{verdict}] {label}: truncated={r.get('truncated')} "
                  f"conf={r.get('confidence')} reason={r.get('reason')}")
        if r.get("error"):
            print(f"        ERROR: {r['error']}")
    judged = [r for r in results if r.get("expected") is not None]
    ok = sum(1 for r in judged if r.get("truncated") == r.get("expected"))
    print(f"\n标准对照: {ok}/{len(judged)} 通过")
    return results


def main():
    ap = argparse.ArgumentParser(description="末尾对比截断检测")
    ap.add_argument("--control", action="store_true", help="跑 4 样本对照验证")
    ap.add_argument("--only", default=None, help="只看文件名包含该子串的论文")
    ap.add_argument("--csv", default=str(data_root() / "truncation_check.csv"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if args.control:
        print("===== 对照验证 =====")
        run_controls()
        return

    stems = load_stems()
    if args.only:
        stems = [s for s in stems if args.only.lower() in s.lower()]
    print(f"待检测 {len(stems)} 篇（并发 {args.workers}）…")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(check_one, s): s for s in stems}
        done = 0
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            if r.get("truncated") is True:
                print(f"  [{done}/{len(stems)}] ⚠️截断? {r.get('reason')} {r['stem'][:45]}")
            elif r.get("error"):
                print(f"  [{done}/{len(stems)}] ERROR {r['error'][:80]} {r['stem'][:35]}")
            elif done % 50 == 0:
                print(f"  [{done}/{len(stems)}] …")

    results.sort(key=lambda x: x["stem"])
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stem", "truncated", "confidence",
                                          "reason", "norm_chars", "error"])
        w.writeheader()
        for r in results:
            w.writerow(r)

    judged = [r for r in results if "truncated" in r]
    pos = [r for r in judged if r.get("truncated") is True]
    neg = [r for r in judged if r.get("truncated") is False]
    err = [r for r in results if r.get("error")]
    print(f"\n===== 汇总（{len(judged)} 篇有判定，{len(err)} 篇错误）=====")
    print(f"判定截断: {len(pos)} 篇 | 判定完整: {len(neg)} 篇")
    if pos:
        print("\n疑似截断清单（需人工复核）:")
        for r in pos:
            print(f"  conf={r.get('confidence')} {r.get('reason')} | {r['stem'][:60]}")
    if err:
        print("\n错误清单:")
        for r in err:
            print(f"  {r['stem'][:60]} → {r['error'][:80]}")
    print(f"\nCSV: {args.csv}")


if __name__ == "__main__":
    main()
