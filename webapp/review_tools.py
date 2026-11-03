# -*- coding: utf-8 -*-
"""综述撰写流程的 MCP 工具。

定位：让**自带智能体**（Claude Code / Codex / Cursor / 自建）不必自己实现工具，
挂上 MCP 就能按 skill/SKILL.md 的流程跑完整轮次。

分工：
  - 内容层（写初稿、消化意见、叙述化）由**用户自己的智能体**做；
  - 本模块只提供流程需要的机械动作：查库、追问、硬断言、成稿、并行审核、引用登记。

所有写库/写文件的动作都委托给 `pipelines/review/*.py` 或 `modules/review/*`，
这里不复制任何业务逻辑。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

LITROOT = Path(__file__).resolve().parent.parent
PIPE_DIR = LITROOT / "pipelines" / "review"
if str(LITROOT) not in sys.path:
    sys.path.insert(0, str(LITROOT))

from modules.review import refs as refs_mod                      # noqa: E402
from modules.review import rounds as rounds_mod                  # noqa: E402
from modules.review.paths import (                               # noqa: E402
    draft_path, evidence_pack_path, final_path, issues_path,
    key_findings_path, probe_path, review_draft_path, think_dir,
)

DEFAULT_TIMEOUT = 900          # 秒；kb_qa / 审核可能跑很久，客户端超时应 >= 此处


def _cur_round(round_no: int | None) -> int:
    return int(round_no) if round_no else rounds_mod.get_round()


def _run(script: str, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> dict:
    """跑一篇管线脚本，返回 {returncode, stdout, stderr}。"""
    cmd = [sys.executable, str(PIPE_DIR / script), *args]
    try:
        p = subprocess.run(cmd, cwd=str(LITROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"超时（>{timeout}s）"}


def _tail(text: str, n: int = 4000) -> str:
    return text if len(text) <= n else text[-n:]


# ────────────────────────── 状态与内容层文件 ──────────────────────────

def review_status(round_no: int | None = None) -> dict:
    """查看本轮进度：轮次号、已有产物、各脚本已用预算。

    BYO 智能体每轮开始时先调它，判断下一步该做什么。
    """
    n = _cur_round(round_no)
    files = {
        "初稿": draft_path(n),
        "事实锚点": key_findings_path(n),
        "追问": probe_path(n),
        "证据包": evidence_pack_path(n),
        "成稿": review_draft_path(n),
        "终稿": final_path(n),
        "审核意见": issues_path(n),
    }
    budget = {}
    for name in ("kb_qa", "probe_draft", "review_draft"):
        try:
            budget[name] = rounds_mod.spent(name, n)
        except Exception:  # noqa: BLE001
            budget[name] = None
    return {
        "round": n,
        "dir": str(think_dir(n)),
        "files": {k: {"path": str(v), "exists": v.exists(),
                      "chars": (len(v.read_text(encoding="utf-8", errors="replace")) if v.exists() else 0)}
                  for k, v in files.items()},
        "budget_spent": budget,
        "next_hint": "初稿不存在→写初稿；有初稿未追问→run_probe；成稿缺失→compile_draft；"
                     "成稿有了没审核→run_review（审核会 round+1）",
    }


def read_draft(round_no: int | None = None) -> str:
    """读取本轮初稿全文（内容层唯一可改的文件，draft_rN.md）。"""
    n = _cur_round(round_no)
    p = draft_path(n)
    if not p.exists():
        return f"[初稿尚未创建] 路径：{p}"
    return p.read_text(encoding="utf-8", errors="replace")


def write_draft(content: str, round_no: int | None = None) -> dict:
    """整篇写入初稿（覆盖）。改几句请用 patch_draft，避免整篇重写。"""
    n = _cur_round(round_no)
    p = draft_path(n)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(p), "chars": len(content)}


def patch_draft(old: str, new: str, round_no: int | None = None) -> dict:
    """在初稿里做一次精确替换（old 必须唯一命中）。返回替换处数。"""
    n = _cur_round(round_no)
    p = draft_path(n)
    if not p.exists():
        return {"ok": False, "error": f"初稿不存在：{p}"}
    text = p.read_text(encoding="utf-8", errors="replace")
    hits = text.count(old)
    if hits == 0:
        return {"ok": False, "error": "未找到 old 片段，未改动"}
    if hits > 1:
        return {"ok": False, "error": f"old 片段命中 {hits} 处，请写更长、更唯一的片段"}
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return {"ok": True, "path": str(p), "chars": len(p.read_text(encoding='utf-8'))}


def save_key_findings(content: str, round_no: int | None = None) -> dict:
    """写入本轮事实锚点清单（追问者会读它，写“缺什么证据”时以它为准）。"""
    n = _cur_round(round_no)
    p = key_findings_path(n)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(p), "chars": len(content)}


# ────────────────────────── 引用（[S#] 登记） ──────────────────────────

def list_refs(round_no: int | None = None, limit: int = 200) -> list[dict]:
    """列出已登记的引用编号（[S#] → 题录），写正文引用时用它对齐编号。"""
    refs = refs_mod.load()
    out = []
    for sid, rec in list(refs.items())[:limit]:
        out.append({
            "id": sid,
            "type": rec.get("type") or "",
            "title": (rec.get("title") or "")[:120],
            "year": rec.get("year"),
            "journal": (rec.get("journal") or "")[:60],
            "paper_id": rec.get("paper_id"),
        })
    return out


def expand_ref(sids: str, max_chars: int = 6000) -> str:
    """展开若干引用的原文片段。sids 用逗号分隔，如 "S12,S15"（能用编号就不要整篇贴）。"""
    refs = refs_mod.load()
    ids = [s.strip() for s in (sids or "").replace("，", ",").split(",") if s.strip()]
    if not ids:
        return "请提供引用编号，如 S12 或 S12,S15"
    return refs_mod.expand(refs, ids, max_chars=max_chars)


# ────────────────────────── 流程脚本 ──────────────────────────

def kb_query(question: str, round_no: int | None = None,
          expand: str = "", no_budget: bool = False) -> dict:
    """查库调研（数据库板块的智能体检索）。返回调研纪要正文 + 新登记引用编号。

    调研纪要会落 data/reviews/research/，正文里出现 [S#] 即可在初稿中引用。
    expand 传逗号分隔的编号可同时展开原文（如 "S12,S15"）。
    """
    n = _cur_round(round_no)
    args = ["--q", question, "--round", str(n)]
    if expand:
        args += ["--expand", *[s.strip() for s in expand.replace("，", ",").split(",") if s.strip()]]
    if no_budget:
        args.append("--no-budget")
    r = _run("kb_qa.py", args)
    return {"round": n, "returncode": r["returncode"],
            "output": _tail(r["stdout"], 8000), "stderr": _tail(r["stderr"], 1000)}


def run_probe(round_no: int | None = None, response: str = "") -> dict:
    """跑独立追问者：读初稿+事实锚点+PRD，产出“对象—覆盖地图 + 追问清单”。

    每轮建议至少 1 次、最多 3 次。response 用于深挖轮（说明上一轮追问怎么处理的）。
    """
    n = _cur_round(round_no)
    args = ["--round", str(n)]
    if response:
        args += ["--response", response]
    r = _run("probe_draft.py", args)
    p = probe_path(n)
    return {"round": n, "returncode": r["returncode"],
            "artifacts": [str(x) for x in p.parent.glob(f"probe_r{n}*.md")],
            "output": _tail(r["stdout"], 6000), "stderr": _tail(r["stderr"], 1000)}


def run_check(round_no: int | None = None, file: str = "") -> dict:
    """机械硬断言：章节完整性 / [S#] 可解析 / 数值可溯源 / 流程自指 / 长句长段 / 摘要重复。"""
    n = _cur_round(round_no)
    args = ["--round", str(n), "--json"]
    if file:
        args += ["--file", file]
    r = _run("check_draft.py", args, timeout=300)
    out = r["stdout"].strip()
    try:
        return {"round": n, "returncode": r["returncode"], "result": json.loads(out)}
    except Exception:  # noqa: BLE001
        return {"round": n, "returncode": r["returncode"], "raw": _tail(out, 4000),
                "stderr": _tail(r["stderr"], 800)}


def run_gap_audit(round_no: int | None = None) -> dict:
    """缺口断言核实：把稿中“尚无/缺失/空白”类断言逐条拿到全库核实，返回可拦截清单。"""
    n = _cur_round(round_no)
    from modules.review import gap_audit as gap_mod  # 惰性导入，避免启动开销
    try:
        res = gap_mod.audit(n)
        return {"round": n, "result": res,
                "blocking_text": gap_mod.render_blocking(res)}
    except Exception as e:  # noqa: BLE001
        return {"round": n, "error": f"{type(e).__name__}: {e}"}


def compile_draft(round_no: int | None = None, pack_only: bool = False) -> dict:
    """初稿 → 成稿（表达层转写）+ 生成引用证据包。pack_only=True 时只出证据包。"""
    n = _cur_round(round_no)
    args = ["--round", str(n)]
    if pack_only:
        args.append("--pack-only")
    r = _run("compile_draft.py", args)
    p = review_draft_path(n)
    return {"round": n, "returncode": r["returncode"],
            "成稿": {"path": str(p), "exists": p.exists()},
            "output": _tail(r["stdout"], 6000), "stderr": _tail(r["stderr"], 1000)}


def run_review(round_no: int | None = None) -> dict:
    """并行审核（多位审稿人 + 人工意见槽位）。**跑完即本轮结束：round += 1**。

    审核意见落 data/reviews/review/review_issues_rN.json，下一轮开场会注入。
    """
    n = _cur_round(round_no)
    r = _run("review_draft.py", args=["--round", str(n)])
    issues = issues_path(n)
    new_round = rounds_mod.get_round()
    return {"reviewed_round": n, "next_round": new_round,
            "returncode": r["returncode"],
            "issues": {"path": str(issues), "exists": issues.exists()},
            "output": _tail(r["stdout"], 6000), "stderr": _tail(r["stderr"], 1000)}


TOOLS = [
    review_status, read_draft, write_draft, patch_draft, save_key_findings,
    list_refs, expand_ref,
    kb_query, run_probe, run_check, run_gap_audit, compile_draft, run_review,
]
