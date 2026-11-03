"""建库阶段 1/5：PDF → 原始文本（pymupdf，不调用 LLM）。

用法：
    python pipelines/build/extract_pdfs.py [--limit N]
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules.util.batch.batch_extract_pdfs import run   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="批量提取 PDF → raw text")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 篇（调试用）")
    args = ap.parse_args()
    run(limit=args.limit)


if __name__ == "__main__":
    main()
