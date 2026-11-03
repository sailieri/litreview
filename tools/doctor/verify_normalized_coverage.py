#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 normalized 文本质量，检测截断/占位/提取失败（确定性规则，不依赖 LLM）。

信号设计（避免 LLM 质检的盲区）：
  A 末尾裸标题       —— normalized 最后一行是章节标题且无正文 → 确定性截断
  B 末尾真半句       —— 最后一行是普通长文本且无结尾标点（排除表格/列表/URL/图标题）→ 疑似截断
  C1 LLM 占位话术    —— 输出是 "Please provide..." 之类的元话术 → normalize 完全失败
  C2 无结论类标题    —— 全文无 Conclusion/Discussion/结论 标题 → 提示级，需人工复核
  D 提取失败         —— extracted 原文过短或只有页面标记 → PDF 提取失败（图片型/扫描版）

用法:
  python tools/doctor/audit_normalize.py coverage                 # 全量，终端汇总
  python tools/doctor/audit_normalize.py coverage --csv out.csv   # 输出明细
  python tools/doctor/audit_normalize.py coverage --only Meyer    # 只看某篇
"""
import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.paths import extracted_dir, normalized_dir  # noqa: E402
NORM_DIR = normalized_dir()
EXTRACT_DIR = extracted_dir()

# 信号 A：末尾裸标题
RE_BARE_TITLE = re.compile(r"^#{1,6}\s+\S")

# 信号 B：合法结尾（不判截断）
OK_ENDINGS = (".", "。", "!", "？", "?", ")", "】", "]", "}", '"', "'", "”", "’", "…", ":", "：", ">", "；", ";")

# 信号 C1：LLM 元话术/占位输出（只查开头 800 字符——占位输出必然在开头；
# 正文中段的 "the document" 等短语是正常引用，不判）
RE_PLACEHOLDER = re.compile(
    r"Please provide|The provided text|no actual scientific content|"
    r"没有可用的文本|未检测到(文本|内容)|无法提供|请提供|"
    r"I cannot|I'?m unable|i am unable|This paper appears to be empty",
    re.I,
)

# 信号 C2：结论类标题
RE_CONCL_TITLE = re.compile(
    r"^#{1,4}\s*.*(Conclusion|Conclusions|conclusion|总结|结论|"
    r"Discussion|discussion|Synthesis|synthesis|Closing)", re.M
)

# 信号 D：extracted 疑似提取失败（只含页面标记/过短）
RE_PAGE_MARK = re.compile(r"--- Page \d+ ---")

# ── 信号 E：raw→normalized 覆盖率（检测"截断在完整段落收尾"的隐蔽截断）──
# 原理：raw 正文滑窗取特异性 n-gram（在 normalized 中低频出现的锚点），命中率>=阈值即覆盖。
# 完整论文 cov 通常 >0.5，截断后明显偏低。
# 边界：双栏/乱序排版的 raw（如博士论文）cov 天然偏低，会误报 → 只做"人工复核"提示，
# 不自动判截断。这类样本的截断由信号 A/B 覆盖（截断点通常是裸标题或半句）。
E_NGRAM = 12
E_WINDOW = 400
E_STEP = 200
E_HIT = 0.5
E_SPEC_MAX = 3
E_COV_WARN = 0.45  # 低于此值提示人工复核


def last_nonempty_line(text: str) -> str:
    lines = text.rstrip().split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-1].strip() if lines else ""


def is_bare_title_end(norm: str) -> bool:
    return bool(RE_BARE_TITLE.match(last_nonempty_line(norm)))


def is_truncated_tail(norm: str) -> bool:
    """信号 B：最后一行是普通长文本且无结尾标点 → 疑似半句截断。"""
    s = last_nonempty_line(norm)
    if not s:
        return False
    if RE_BARE_TITLE.match(s):
        return False                      # 标题归信号 A
    if s.endswith(OK_ENDINGS):
        return False
    if "|" in s:
        return False                      # 表格行
    if s.startswith(("*", "-", "+", ">", "`")):
        return False                      # 列表/引用/代码
    if "http://" in s or "https://" in s:
        return False                      # URL
    if re.match(r"^(Figure|Fig\.|Table|图|表)\s*\d", s):
        return False                      # 图/表标题
    if len(s) < 30:
        return False                      # 短行宽容（关键词/页脚等）
    return True


def has_placeholder(norm: str) -> bool:
    """信号 C1：占位输出必然在开头 800 字符内。"""
    return bool(RE_PLACEHOLDER.search(norm[:800]))


def has_conclusion_title(norm: str) -> bool:
    return bool(RE_CONCL_TITLE.search(norm))


def extracted_suspect(raw: str) -> bool:
    """信号 D：提取文本疑似失败（过短或几乎只有页面标记）。"""
    if len(raw.strip()) < 1500:
        return True
    body = RE_PAGE_MARK.sub("", raw).strip()
    if len(body) < 500:
        return True
    return False


def coverage_signal(raw: str, norm: str) -> float:
    """信号 E：raw 正文滑窗特异性锚点覆盖率（0~1）。"""
    from collections import Counter
    counts = Counter(norm[i:i + E_NGRAM] for i in range(len(norm) - E_NGRAM + 1))
    spec = {g for g, c in counts.items() if c <= E_SPEC_MAX}
    if not spec:
        return 0.0
    wins, hits = 0, 0
    start = 0
    while start < len(raw):
        chunk = raw[start:start + E_WINDOW]
        cg = {chunk[i:i + E_NGRAM] for i in range(len(chunk) - E_NGRAM + 1)} if len(chunk) >= E_NGRAM else set()
        if cg:
            wins += 1
            if sum(1 for g in cg if g in spec) / len(cg) >= E_HIT:
                hits += 1
        start += E_STEP
    return hits / wins if wins else 0.0


def analyze_one(stem: str) -> dict:
    r = {"stem": stem, "error": None}
    norm_path = NORM_DIR / f"{stem}.md"
    raw_path = EXTRACT_DIR / f"{stem}_raw.md"
    if not norm_path.exists() or not raw_path.exists():
        r["error"] = f"missing norm={norm_path.exists()} raw={raw_path.exists()}"
        return r

    norm = norm_path.read_text(encoding="utf-8", errors="replace")
    raw = raw_path.read_text(encoding="utf-8", errors="replace")
    r["raw_chars"] = len(raw)
    r["norm_chars"] = len(norm)

    r["sigA_bare_title"] = is_bare_title_end(norm)
    r["sigB_truncated_tail"] = is_truncated_tail(norm)
    r["sigC1_placeholder"] = has_placeholder(norm)
    r["sigC2_no_conclusion"] = not has_conclusion_title(norm)
    r["sigD_extract_fail"] = extracted_suspect(raw)
    r["sigE_coverage"] = round(coverage_signal(raw, norm), 3)
    r["sigE_low"] = r["sigE_coverage"] < E_COV_WARN
    r["tail_preview"] = last_nonempty_line(norm)[:80]
    return r


def load_stems() -> list[str]:
    return sorted(p.stem for p in NORM_DIR.glob("*.md"))


def main():
    ap = argparse.ArgumentParser(description="normalized 质量验证（确定性规则）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    stems = load_stems()
    if args.only:
        stems = [s for s in stems if args.only.lower() in s.lower()]
    if args.limit:
        stems = stems[: args.limit]

    print(f"待验证 {len(stems)} 篇 …", file=sys.stderr)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(analyze_one, s): s for s in stems}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: r["stem"])

    # 汇总
    nA = sum(1 for r in results if r.get("sigA_bare_title"))
    nB = sum(1 for r in results if r.get("sigB_truncated_tail"))
    nC1 = sum(1 for r in results if r.get("sigC1_placeholder"))
    nC2 = sum(1 for r in results if r.get("sigC2_no_conclusion"))
    nD = sum(1 for r in results if r.get("sigD_extract_fail"))
    nE = sum(1 for r in results if r.get("sigE_low"))
    print(f"\n===== 汇总（{len(results)} 篇）=====")
    print(f"信号A 末尾裸标题(截断)      : {nA}")
    print(f"信号B 末尾真半句(疑似截断)  : {nB}")
    print(f"信号C1 LLM占位(normalize失败): {nC1}")
    print(f"信号C2 无结论标题(提示级)   : {nC2}")
    print(f"信号D 提取失败(源PDF问题)   : {nD}")
    print(f"信号E 覆盖率<{E_COV_WARN}(人工复核)  : {nE}")

    flagged = [r for r in results
               if r.get("sigA_bare_title") or r.get("sigB_truncated_tail")
               or r.get("sigC1_placeholder") or r.get("sigD_extract_fail")]
    print(f"\n===== 必检清单（A/B/C1/D 任一命中，{len(flagged)} 篇）=====")
    for r in flagged:
        tags = []
        if r.get("sigA_bare_title"): tags.append("A截断")
        if r.get("sigB_truncated_tail"): tags.append("B半句")
        if r.get("sigC1_placeholder"): tags.append("C1占位")
        if r.get("sigD_extract_fail"): tags.append("D提取失败")
        print(f"  [{','.join(tags)}] {r['stem'][:70]}")
        print(f"        tail: {r['tail_preview'][:70]}")

    low_cov = [r for r in results if r.get("sigE_low") and r not in flagged]
    if low_cov:
        print(f"\n===== 覆盖率低（信号E，{len(low_cov)} 篇，人工复核）=====")
        for r in sorted(low_cov, key=lambda x: x["sigE_coverage"])[:25]:
            print(f"  cov={r['sigE_coverage']:.2f} {r['stem'][:65]}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "stem", "raw_chars", "norm_chars",
                "sigA_bare_title", "sigB_truncated_tail",
                "sigC1_placeholder", "sigC2_no_conclusion",
                "sigD_extract_fail", "sigE_coverage", "sigE_low",
                "tail_preview", "error"])
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nCSV 已输出: {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
