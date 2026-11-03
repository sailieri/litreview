#!/usr/bin/env python3
"""文本质量审计（统一入口）：三类审计收成一个命令。

子命令：
  raw       原始抽取 vs 规范化文本的对照审计（是否漏章节、压缩比是否合理）
  coverage  规范化覆盖率（哪些论文有 raw 却没 normalized）
  tail      截断检查（规范化文本尾部是否被切断）

典型用法（入库后必查）：
  python tools/doctor/audit_normalize.py raw
  python tools/doctor/audit_normalize.py coverage
  python tools/doctor/audit_normalize.py tail

提示：规范化误删 Methods 这类问题，用 raw 子命令抽查最直接。
"""
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent

SUBCOMMANDS = {
    "raw": ("audit_raw_norm.py", "原始 vs 规范化 对照审计"),
    "coverage": ("verify_normalized_coverage.py", "规范化覆盖率"),
    "tail": ("check_truncation_tail.py", "尾部截断检查"),
}


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        print("可用子命令：")
        for k, (_, desc) in SUBCOMMANDS.items():
            print(f"  {k:10s} {desc}")
        return 0
    sub = argv[1]
    if sub not in SUBCOMMANDS:
        print(f"[ERR] 未知子命令 {sub}")
        print("可用：", ", ".join(SUBCOMMANDS))
        return 2
    script = HERE / SUBCOMMANDS[sub][0]
    if not script.exists():
        print(f"[ERR] 找不到 {script}")
        return 1
    return subprocess.run([sys.executable, str(script), *argv[2:]]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
