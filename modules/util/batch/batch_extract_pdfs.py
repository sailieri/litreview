"""批量提取 PDF → 原始文本（pymupdf，不调用 LLM）。

只提供函数，不提供命令行；入口在 `pipelines/build/extract_pdfs.py`。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules.normalize.extractor import extract_pdf   # noqa: E402
from modules.paths import extracted_dir, pdfs_dir     # noqa: E402


def run(limit: int | None = None) -> dict:
    """把 data/pdfs/*.pdf 全部转成 data/extracted/<stem>_raw.md。

    返回 {"total", "ok", "chars"}。
    """
    out_dir = extracted_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(pdfs_dir().glob("*.pdf"))
    if limit:
        pdfs = pdfs[:limit]
    print(f"共 {len(pdfs)} 篇 PDF，输出到 {out_dir}\n")

    ok = 0
    total_chars = 0
    for i, pdf_path in enumerate(pdfs, 1):
        t0 = time.time()
        try:
            result = extract_pdf(str(pdf_path), str(out_dir))
            chars = len(result["full_text"])
            ok += 1
            total_chars += chars
            print(f"  [{i}/{len(pdfs)}] {result['title'][:50]:50s} {chars:>6}ch {time.time()-t0:.1f}s")
        except Exception as e:  # noqa: BLE001  单篇失败不阻断整批
            print(f"  [{i}/{len(pdfs)}] ❌ {pdf_path.stem[:50]:50s} {e}")

    print(f"\n完成：{ok}/{len(pdfs)} 篇，共 {total_chars} 字符 → {out_dir}")
    return {"total": len(pdfs), "ok": ok, "chars": total_chars}
