"""建库阶段 2/5：原始文本 → 规范 markdown + 元数据（走 step=normalize 的 provider）。

已存在 normalized + metadata 的论文会自动跳过，可反复跑。

用法：
    python pipelines/build/normalize.py [--limit N]
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules.util.batch.batch_normalize import run   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="批量规范化 + 元数据提取")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 篇（调试用）")
    args = ap.parse_args()
    run(limit=args.limit)


if __name__ == "__main__":
    main()
