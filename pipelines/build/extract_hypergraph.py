"""建库阶段 3/5：超图实体 + 超边提取（含 A-C 自优化，可断点续跑）。

用法：
    python pipelines/build/extract_hypergraph.py
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules.util.batch.batch_extract_all import run   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="超图实体+超边提取（断点续跑）")
    ap.parse_args()      # 暂无参数；保留 argparse 以便 --help 与拼错参数时立即报错
    run()


if __name__ == "__main__":
    main()
