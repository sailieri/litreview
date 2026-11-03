"""review_draft.py —— V2.3 综述管线脚本⑤：并行审核 + 收敛判定。

输入定稿（writing/round_NN/review_draft_rN.md），多 persona 并行审稿，
产出 review/review_issues_rN.json（每条带 location 章节锚点），并给出 converged 字段。

**每完成一次审核 = 本轮结束，round 自动 +1**（SOP §1 硬规则）。

用法（CLI）：
    python review_draft.py --round 1

程序化（main_agent.py 用）：
    from review_draft import run_review
    res = run_review(round_no=1)   # {"round","p0","total","converged","issues_path",...}
"""
import argparse
import json
import shutil
import sys
from pathlib import Path


# 把仓库根加入 sys.path（脚本在子目录里运行）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from modules.review.paths import (review_draft_path, issues_path, final_path,
                                  CURRENT_FINAL, REVIEW)
from modules.review.critique import review_team as rt
from modules.review import rounds

# 收敛判据：致命问题（P0）为 0，意见总数不参与判定。
# 意见总数是有界配额下的无界量（每人 0–12 条 × 6 人），拿它当收敛依据会恒为假；
# 真正该关心的是其中有多少条是**真有问题**的——分级靠 P0 纪律 + 跨轮 prev_check。


def stats(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"p0": 99, "total": 99}
    total = p0 = 0
    for rv in data.get("reviewers", []):
        for it in (rv.get("issues") or []):
            if not isinstance(it, dict):
                continue
            total += 1
            if str(it.get("severity", "")).upper() == "P0":
                p0 += 1
    return {"p0": p0, "total": total}


def run_review(round_no: int | None = None, no_bump: bool = False,
               reset_budget: bool = False) -> dict:
    """跑一次并行审核；定稿不存在时抛 FileNotFoundError。"""
    r = round_no or rounds.get_round()
    if reset_budget:
        rounds.reset_budget(r)

    dp = review_draft_path(r)
    if not dp.exists():
        raise FileNotFoundError(f"[review] 找不到定稿 {dp}（提交审核前先写 review_draft_r{r}.md）")

    rounds.spend("review_draft", r)
    out = issues_path(r)
    out.parent.mkdir(parents=True, exist_ok=True)
    rt.run_with_human(draft_text=dp.read_text(encoding="utf-8"),
                      round_no=r, out_path=out)

    st = stats(out)
    converged = (st["p0"] == 0)

    # 归档本轮定稿：目录可能不存在（归档/全新环境），先建目录再 copy，
    # 否则审核跑完却在最后一步报 FileNotFoundError，白花一次审稿成本。
    final_path(r).parent.mkdir(parents=True, exist_ok=True)
    CURRENT_FINAL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(dp, final_path(r))
    shutil.copy(dp, CURRENT_FINAL)

    # 每审核一次 = 本轮结束，round += 1
    new_round = r if no_bump else rounds.bump_round()

    return {"round": r, "p0": st["p0"], "total": st["total"],
            "converged": converged, "issues_path": str(out),
            "final": str(final_path(r)), "next_round": new_round}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    ap = argparse.ArgumentParser(description="litreview V2.3 并行审核")
    ap.add_argument("--round", type=int, default=None)
    ap.add_argument("--reset-budget", action="store_true")
    ap.add_argument("--no-bump", action="store_true", help="不自动轮次 +1（调试用）")
    args = ap.parse_args()

    res = run_review(args.round, no_bump=args.no_bump, reset_budget=args.reset_budget)
    print("\n=== review_draft 结果 ===")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res["converged"]:
        print(f"\n✅ 收敛（P0=0；本轮意见合计 {res['total']} 条，仅作参考）。终稿：{CURRENT_FINAL}")
    else:
        print(f"\n❌ 未收敛（真 P0={res['p0']}；意见合计 {res['total']} 条）。"
              f"下一轮 round={res['next_round']}：读 {res['issues_path']} 只消 P0，其余按性价比处理")
    return 0 if res["converged"] else 2


if __name__ == "__main__":
    sys.exit(main())
