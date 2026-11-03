"""
Module: Paper normalizer — two-step LLM pipeline.

Step 1: Extract structured metadata (JSON)
Step 2: Normalize body text (Markdown, no refs)

Both calls share the paper text as prefix, so provider-side prompt caching can save tokens on step 2.

Outputs:
  data/metadata/<paper>.json   — author/year/journal/doi/keywords (for traceability, NOT for RAG)
  data/normalized/<paper>.md   — title + abstract + keywords + body (intro→conclusion, no refs)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ..util.llm_client import LLMClient
from ..util.deduplicate import PLACEHOLDER_TITLES
from ..util.dedup_gate import (DuplicateError, audit_forced, check_duplicate,
                               first_doi, format_report, heuristic_title,
                               norm_title)


# Shared prefix for both calls — ensures cache hit on step 2
SYSTEM_PROMPT = "You are a scientific text processing assistant."


METADATA_PROMPT = """Extract metadata from the following paper. Output ONLY valid JSON, no other text.

{{
  "title": "TITLE_HERE",
  "authors": "Author1, Author2, ...",
  "year": 2024,
  "journal": "Journal Name",
  "doi": "10.xxxx/xxxxx",
  "keywords": ["keyword1", "keyword2", ...]
}}

Rules:
- title: extract from the paper, not the file name
- authors: comma-separated list of all authors
- year: 4-digit publication year
- journal: full journal name
- doi: the DOI if visible, otherwise empty string
- keywords: from the paper's Keywords section if present, otherwise empty list
- IMPORTANT: The example values above ("TITLE_HERE", "Author1, Author2", etc.) are placeholders ONLY — you MUST replace every one of them with real values from the paper. NEVER output the placeholder text literally.
- If a piece of information is not present in the paper, output an empty string (or empty list for keywords) instead of guessing.

Paper text:
{paper_text}
"""


NORMALIZE_PROMPT = """Clean the following paper into clean Markdown. Remove all noise but preserve scientific content.

Instructions:
1. Start with "# Title" on the first line
2. Follow with ## Abstract and ## Keywords if present
3. Structure the body with proper headings: ## Section, ### Subsection
4. Fix broken words (e.g. "esti-\\nmation" → "estimation")
5. Remove: page markers, publisher boilerplate, copyright lines, PII, DOI lines
6. Preserve ALL numbers, equations, and technical details
7. DO NOT summarize or add interpretation
8. **Stop before the References / Bibliography / Acknowledgments section. Drop them entirely.**
9. Do NOT include author names or affiliations in the output (they go in metadata)

Expected output format:
# Full Title

## Abstract
...

## Keywords
kw1, kw2, ...

## 1. Introduction
...

## Conclusion
...

Paper text:
{paper_text}
"""


def _strip_code_fence(text: str) -> str:
    """Remove markdown code block fences if present."""
    if text.startswith("```"):
        lines = text.split("\n", 1)
        if len(lines) > 1:
            text = lines[1]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    return text


def extract_metadata(raw_text: str, client: LLMClient | None = None,
                     model: str | None = None, max_attempts: int = 3,
                     stem: str | None = None, force: bool = False) -> dict:
    """Step 1: Extract structured metadata via LLM.

    后校验：title 为空或命中占位符清单（如 "Full paper title"）判定提取失败，
    自动重试（默认最多 3 次）。双重保险：提示词已要求找不到就留空+不照抄示例。

    查重闸门：
      ① LLM 前：启发式标题 + raw 首个 DOI + 文件指纹免费预查（“先不提取”，零成本）
      ② LLM 后：用提取出的精确 title/doi/authors 再查一次（防①因标题没提准而漏拦）
      命中且 force=False → 抛 DuplicateError，调用方不会写 json；
      force=True → 放行但打印报告并写审计 data/dedup_forced.jsonl（须先人工核对）。

    Returns dict with keys: title, authors, year, journal, doi, keywords.
    """
    ctx = f"元数据线 extract_metadata(stem={stem or '?'})"

    def gate(title, doi, authors="", stage: str = ""):
        """查重；命中且非 force 则抛 DuplicateError。force 放行会打印+审计。"""
        nt = norm_title(title)
        if not nt or len(nt) < 12:  # 太短的启发式标题不作比对键（防误拦）
            title = None
        hits = check_duplicate(title=title, doi=doi, stem=stem, authors=authors,
                               raw_text=raw_text, exclude_stem=stem)
        if not hits:
            return
        cur = {"title": title or "", "doi": doi or "", "stem": stem}
        if force:
            audit_forced(ctx + stage, cur, hits, reason="force 放行（调用方已人工核对）")
            print(format_report(hits, ctx + stage + " [force 已放行]", cur))
            return
        raise DuplicateError(hits, ctx + stage, cur)

    if not client:
        return {"title": "(dry run)", "authors": "", "year": 0,
                "journal": "", "doi": "", "keywords": []}

    # ① 免费预查（启发式标题 + raw 首个 DOI + raw 指纹）——命中则根本不跑 LLM
    gate(heuristic_title(raw_text), first_doi(raw_text), stage=" · LLM前预查")

    prompt = METADATA_PROMPT.format(paper_text=raw_text)
    result = None
    for attempt in range(max_attempts):
        result = client.chat_json(prompt, model=model)

        # Ensure all expected keys exist
        for key in ["title", "authors", "year", "journal", "doi", "keywords"]:
            if key not in result:
                result[key] = "" if key != "year" else 0
            if key == "keywords" and isinstance(result[key], list):
                result[key] = [k for k in result[key] if k]  # strip empties

        # 后校验：title 必须是有效非占位文本
        title = str(result.get("title", "")).strip()
        if title and title.lower() not in PLACEHOLDER_TITLES:
            # ② LLM 后复查：用精确 title/doi/authors（第二道保险）
            gate(title, result.get("doi", ""),
                 authors=result.get("authors", ""), stage=" · LLM后复查")
            return result

        if attempt < max_attempts - 1:
            print(f"  [metadata] title 校验失败 ('{title[:40]}' 为占位/空)，"
                  f"重试 {attempt + 2}/{max_attempts}")

    # 最终仍失败：返回最后一次结果（调用方可感知并处理）
    assert result is not None
    return result


def normalize_text(raw_text: str, client: LLMClient | None = None,
                   model: str | None = None) -> str:
    """Step 2: Normalize paper body text via LLM.

    Returns clean Markdown: title → abstract → keywords → body (no refs, no authors).
    """
    if not client:
        return f"(dry-run: {len(raw_text)} chars raw → cleaned)"

    prompt = NORMALIZE_PROMPT.format(paper_text=raw_text)
    result = client.chat(prompt, model=model, temperature=0.1)
    return _strip_code_fence(result).strip()


def normalize_paper(raw_text: str, client: LLMClient | None = None,
                    model: str | None = None,
                    stem: str | None = None, force: bool = False) -> tuple[dict, str]:
    """Full pipeline: metadata + normalized text (two LLM calls, shares prefix for cache).

    stem/force 透传给查重闸门：闸门命中时元数据步骤就抛出，两次 LLM 都不花。"""
    meta = extract_metadata(raw_text, client=client, model=model,
                            stem=stem, force=force)
    body = normalize_text(raw_text, client=client, model=model)
    return meta, body


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="2-step paper normalizer: metadata + clean text")
    parser.add_argument("--extracted", "-i", required=True, help="Path to extracted _raw.md file")
    parser.add_argument("--metadata-dir", default=None, help="Output dir for metadata JSON (default: auto)")
    parser.add_argument("--normalized-dir", default=None, help="Output dir for normalized .md (default: auto)")
    parser.add_argument("--no-llm", action="store_true", help="Dry run without LLM")
    parser.add_argument("--model", default=None, help="Model override")
    parser.add_argument("--force", action="store_true",
                        help="查重命中时仍继续导入（须已人工核对非重复；会写审计）")
    args = parser.parse_args()

    # Derive base name from input
    base = os.path.splitext(os.path.basename(args.extracted))[0]
    if base.endswith("_raw"):
        base = base[:-4]

    with open(args.extracted, "r", encoding="utf-8") as f:
        raw_text = f.read()
    print(f"Loaded {len(raw_text)} chars from {args.extracted}")

    # Default output dirs
    from modules.paths import data_root
    data_dir = data_root()
    meta_dir = Path(args.metadata_dir) if args.metadata_dir else data_dir / "metadata"
    norm_dir = Path(args.normalized_dir) if args.normalized_dir else data_dir / "normalized"

    client = None if args.no_llm else LLMClient(step="normalize")
    try:
        meta, body = normalize_paper(raw_text, client=client, model=args.model,
                                     stem=base, force=args.force)
    except DuplicateError as e:
        print(str(e))
        sys.exit(2)

    # Save metadata
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = meta_dir / f"{base}.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"Metadata: {meta_path}")

    # Save normalized
    norm_dir.mkdir(parents=True, exist_ok=True)
    norm_path = norm_dir / f"{base}.md"
    with open(norm_path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"Normalized: {norm_path}  ({len(body)} chars)")
