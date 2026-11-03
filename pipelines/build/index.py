"""建库阶段 4/5：规范文本 → sections + chunks 入库。

用法：
    python pipelines/build/index.py [--dir <normalized 目录>]
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules.paths import normalized_dir                      # noqa: E402
from modules.util.batch.batch_index import index_all_papers    # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="批量索引：normalized → sections + chunks")
    ap.add_argument("--dir", default=str(normalized_dir()), help="normalized 目录")
    args = ap.parse_args()
    index_all_papers(args.dir)


if __name__ == "__main__":
    main()
