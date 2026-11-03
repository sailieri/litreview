"""建库阶段 5/5：补齐向量（chunks / 合并实体 / 跨文档超边）。

用法：
    python pipelines/build/embeds.py
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules.util.batch.batch_embeds import run   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="补齐缺失向量")
    ap.parse_args()      # 暂无参数；保留 argparse 以便 --help 与拼错参数时立即报错
    run()


if __name__ == "__main__":
    main()
