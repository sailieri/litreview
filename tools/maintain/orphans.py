#!/usr/bin/env python3
"""孤儿 PDF 治理（统一入口）：一个命令管完 data/pdfs 里没登记的文件。

适用场景：data/pdfs 里的文件与 papers 表不一定一一对应——手工下了但没入库、
入库失败留了半截、同一篇从两条路径各入一行。这条流水线按顺序处理：

  inspect  体检：列出「有文件但没有论文行引用」的孤儿 PDF
  match    核对 data/back 里手工下载的 PDF 与缺失清单的匹配情况
  dups     查重：逐个判定「与库内某篇重复 / 是新文献」
  ingest   批量入库孤儿 PDF（extract → normalize → index → 向量，逐篇）
  list     把补不回来的 PDF 导成人工下载清单（CSV + Markdown）
  dedupe   论文重复行合并（并入保留行后删除重复行及派生数据）

用法：
  python tools/maintain/orphans.py inspect
  python tools/maintain/orphans.py dups
  python tools/maintain/orphans.py ingest --limit 5     # 先小批量试
  python tools/maintain/orphans.py --help

⚠ ingest / dedupe 会写库，跑之前先确认 doctor health 是绿的。
"""
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent

SUBCOMMANDS = {
    "inspect": ("inspect_orphan_pdfs.py", "列出孤儿 PDF（只读）"),
    "match": ("match_back_pdfs.py", "核对 back 目录与缺失清单（只读）"),
    "dups": ("check_orphan_dups.py", "孤儿 PDF 查重判定（只读）"),
    "ingest": ("ingest_orphan_batch.py", "批量入库孤儿 PDF（写库）"),
    "list": ("make_manual_download_list.py", "导出人工下载清单"),
    "dedupe": ("dedupe_papers.py", "论文重复行合并（写库）"),
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
