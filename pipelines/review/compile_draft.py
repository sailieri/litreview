"""compile_draft.py —— V2.3 综述管线脚本④：成稿整合。

初稿（论证稿） → 成稿（表达稿）。**成稿只由本脚本生成，主智能体不要手改成稿。**

用法：
    python compile_draft.py --round 1 --pack-only   # 只生成引用证据包
    python compile_draft.py --round 1               # 先跑硬断言，全绿才编译
    python compile_draft.py --round 1 --skip-check  # 跳过断言（不推荐）
"""
import argparse
import subprocess
import sys
from pathlib import Path


# 把仓库根加入 sys.path（脚本在子目录里运行）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from modules.review.paths import draft_path
from modules.review.writing import compiler
from modules.review import rounds


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    ap = argparse.ArgumentParser(description="litreview V2.3 成稿整合")
    ap.add_argument("--round", type=int, default=None)
    ap.add_argument("--pack-only", action="store_true", help="只生成引用证据包")
    ap.add_argument("--skip-check", action="store_true", help="跳过硬断言（不推荐）")
    ap.add_argument("--reset-budget", action="store_true")
    args = ap.parse_args()

    r = args.round or rounds.get_round()
    if args.reset_budget:
        rounds.reset_budget(r)

    if not draft_path(r).exists():
        sys.exit(f"[compile] 找不到初稿 {draft_path(r)}")

    if args.pack_only:
        compiler.run(r, pack_only=True)
        return

    if not args.skip_check:
        print("[compile] 先跑硬断言 check_draft …")
        rc = subprocess.call([sys.executable, "check_draft.py", "--round", str(r)])
        if rc != 0:
            sys.exit("[compile] 硬断言有 ERROR，已中止。先修初稿（SOP §4：ERROR 清零才能 compile）")

    rounds.spend("compile_draft", r)
    compiler.run(r)


if __name__ == "__main__":
    main()
