#!/usr/bin/env python3
"""全库体检（统一入口）：一条命令看完整库状态。

子命令（转发到 doctor/ 下对应脚本）：
  health     逐篇核对内容量 / 超图 / 摘要 / 向量 / 元数据 + 悬空引用
  graph      超图质量：规模分布、重复实体、极短名、空描述
  smoke      合并后冒烟：实体解析 + 向量检索 + 混合检索
  status     提取状态台账（哪些论文还没走完流程）
  review     综述稿质量检查
  audit      文本质量审计（转发到 audit_normalize.py：raw / coverage / tail）
  ids        实体 ID 双域体检：私有/公有列是否齐、公有 id 有无悬空

用法：
  python tools/doctor/doctor.py health
  python tools/doctor/doctor.py audit coverage
  python tools/doctor/doctor.py --help
"""
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent

SUBCOMMANDS = {
    "health": ("health_check.py", "全库健康检查"),
    "graph": ("health_graph.py", "超图质量检查"),
    "smoke": ("smoke_after_merge.py", "合并后冒烟"),
    "status": ("extraction_status.py", "提取状态台账"),
    "review": ("review_qa.py", "综述稿质量检查"),
    "audit": ("audit_normalize.py", "文本质量审计（子命令：raw/coverage/tail）"),
    "ids": ("id_domain_check.py", "实体 ID 双域体检（只读）"),
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
