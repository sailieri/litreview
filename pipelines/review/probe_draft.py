"""probe_draft.py —— V2.3 综述管线脚本②：苏格拉底式追问者（支持多轮深挖）。

读本轮初稿（draft_rN.md）+ 事实锚点（key_findings.md），以独立视角挑刺，
产出 thinking/round_NN/probe_rN[_k].md。**主智能体不要自扮追问者**——同一模型审自己
会系统性放过自己的盲区（撰写人与审稿人共享同一个模型的盲区）。

支持多轮追问：第 2 次及以后的调用会把「上一轮问题 + 主智能体回应」喂回追问者，
让它先判定哪些真解决了、再往下深挖一层（或不重复地问新方面）。
历史存 thinking/round_NN/probe_history.json。

用法（CLI）：
    python probe_draft.py --round 1                # 首次追问
    python probe_draft.py --round 1 --response "..." # 第 N 次（回应摘要）

程序化（main_agent.py 用）：
    from probe_draft import run_probe
    res = run_probe(round_no=1, response="已补 X 的证据")   # {"assessment","questions","file","seq"}
"""
import argparse
import json
import sys
from pathlib import Path


# 把仓库根加入 sys.path（脚本在子目录里运行）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from modules.review.paths import think_dir, draft_path, key_findings_path, META
from modules.review import probe as probe_tool
from modules.review import rounds


def _history_file(rdir):
    return rdir / "probe_history.json"


def load_history(rdir) -> list[dict]:
    p = _history_file(rdir)
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def run_probe(round_no: int | None = None, reset_budget: bool = False,
              response: str = "") -> dict:
    """跑一次追问者。

    response：主智能体对「上一轮追问」的处理说明（首轮留空）。
              非空即视为深挖轮——追问者拿到上一轮问题，先判定再继续追问。
    初稿不存在时抛 FileNotFoundError（由调用方转成工具错误）。
    """
    r = round_no or rounds.get_round()
    if reset_budget:
        rounds.reset_budget(r)

    d = draft_path(r)
    if not d.exists():
        raise FileNotFoundError(f"[probe] 找不到初稿 {d}（主智能体先写 draft_r{r}.md）")

    ddir = think_dir(r)
    ddir.mkdir(parents=True, exist_ok=True)
    text = probe_tool.assemble_deliverables(
        r, filenames=[f"draft_r{r}.md", "key_findings.md"], ddir=ddir)
    # 把 PRD（主题/大纲倾向）也给追问者——否则它判不了“覆盖是否达到大纲要求的范围”，
    # 只能就已有文字提问，看不见“该有而没有”的东西。
    try:
        prd_p = META / "_prd.md"
        if prd_p.exists():
            text = ("### 文件：meta/_prd.md（主题与大纲倾向——用它判断“应该覆盖什么”、"
                    "再对照下方初稿看“实际覆盖了什么”）\n\n"
                    + prd_p.read_text(encoding="utf-8") + "\n\n---\n\n" + text)
    except Exception as e:
        print(f"[probe] PRD 注入失败（不影响追问）：{e}")

    hist = load_history(ddir)
    seq = len(hist) + 1

    rounds.spend("probe_draft", r)
    res = probe_tool.run(text, r, ddir=ddir,
                         history=(hist if (hist and response.strip()) else None),
                         response=response, seq=seq)

    hist.append({"seq": seq, "assessment": res.get("assessment", ""),
                 "questions": res.get("questions") or [],
                 "response": response.strip()[:2000]})
    _history_file(ddir).write_text(json.dumps(hist, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    res["seq"] = seq
    return res


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    ap = argparse.ArgumentParser(description="litreview V2.3 追问者")
    ap.add_argument("--round", type=int, default=None)
    ap.add_argument("--reset-budget", action="store_true")
    ap.add_argument("--response", default="", help="对上一轮追问的处理说明（深挖轮必填）")
    args = ap.parse_args()

    res = run_probe(args.round, reset_budget=args.reset_budget, response=args.response)
    print(f"[probe] 第 {res.get('seq', 1)} 次：{len(res['questions'])} 条追问 -> {res['file']}")
    print("\n## 总体评价\n" + res.get("assessment", ""))
    if res.get("resolved"):
        print("\n## 上一轮判定")
        for x in res["resolved"]:
            print(f"- [{x.get('verdict','?')}] {x.get('q','')} — {x.get('note','')}")
    print("\n## 追问清单")
    for i, q in enumerate(res["questions"], 1):
        print(f"{i}. {q}")


if __name__ == "__main__":
    main()
