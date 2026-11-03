"""单智能体管线：轮次状态 + 每轮脚本预算（唯一真相源）。

规则：
- `_round_state.json` 的 `round` 是轮次唯一真相源；
- **每完成一次审核（review_draft.py 跑完）= 本轮结束，round += 1**；
- 所有产物路径带轮次号（paths.draft_path / review_draft_path / issues_path），禁止跨轮覆盖。

state 文件缺失时不能默认 round = 1，否则首轮产物会覆盖已有轮次的目录。
本模块强制读写 state，缺文件时**按已有轮次目录推断**。
"""
import datetime
import json
import re
from pathlib import Path

from modules.review.paths import META, THINKING

ROUND_STATE = META / "_round_state.json"
BUDGET_FILE = META / "_budget.json"

# 每轮脚本预算上限（SOP §5）
BUDGET_LIMITS = {
    "kb_qa": 24,          # 每主题 2 次查询不足以支撑深度；按「问题数」计，一次可并行 4 问
    "probe_draft": 3,     # 强制 ≥1 次，且允许一次深挖轮
    "compile_draft": 2,
    "review_draft": 2,
}


class BudgetExceeded(RuntimeError):
    """脚本每轮预算用尽——需人工明确授权才能清计数。"""


def _existing_rounds() -> list[int]:
    if not THINKING.is_dir():
        return []
    out = []
    for p in THINKING.iterdir():
        m = re.fullmatch(r"round_(\d+)", p.name)
        if m and p.is_dir():
            out.append(int(m.group(1)))
    return sorted(out)


def load_state() -> dict:
    if ROUND_STATE.exists():
        try:
            d = json.loads(ROUND_STATE.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("round"):
                return d
        except Exception:
            pass
    # 缺 state：按已有轮次目录推断当前轮（不默认 1，避免覆盖历史）
    rounds = _existing_rounds()
    cur = (rounds[-1] if rounds else 1)
    return {"round": cur, "updated": None, "note": "state 缺失，按目录推断"}


def save_state(s: dict):
    META.mkdir(parents=True, exist_ok=True)
    s = dict(s)
    s["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    ROUND_STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def get_round() -> int:
    return int(load_state()["round"])


def set_round(n: int):
    s = load_state()
    s["round"] = int(n)
    save_state(s)


def bump_round() -> int:
    """本轮结束：round += 1，返回新轮次号。"""
    s = load_state()
    s["round"] = int(s.get("round", 1)) + 1
    save_state(s)
    return s["round"]


# ── 预算计数（按轮次隔离）──

def _load_budget() -> dict:
    if BUDGET_FILE.exists():
        try:
            d = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def _save_budget(d: dict):
    META.mkdir(parents=True, exist_ok=True)
    BUDGET_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def spent(script: str, round_no: int | None = None) -> int:
    n = get_round() if round_no is None else int(round_no)
    return int(_load_budget().get(str(n), {}).get(script, 0))


def spend(script: str, round_no: int | None = None, limit: int | None = None) -> int:
    """记一次调用。超限抛 BudgetExceeded（不记账）。返回本次调用后的次数。"""
    n = get_round() if round_no is None else int(round_no)
    cap = BUDGET_LIMITS.get(script) if limit is None else limit
    b = _load_budget()
    cur = int(b.get(str(n), {}).get(script, 0))
    if cap is not None and cur >= cap:
        raise BudgetExceeded(
            f"[budget] 第 {n} 轮 {script} 已用 {cur}/{cap} 次，超出上限。"
            f"如确需继续，请确认后手动编辑 {BUDGET_FILE} 或运行 `--reset-budget`。")
    b.setdefault(str(n), {})[script] = cur + 1
    _save_budget(b)
    return cur + 1


def reset_budget(round_no: int | None = None):
    n = get_round() if round_no is None else int(round_no)
    b = _load_budget()
    b.pop(str(n), None)
    _save_budget(b)


def refund(script: str, round_no: int | None = None) -> int:
    """退还一次计数（调用失败、没拿到任何产出时用，避免故障吃掉预算）。"""
    n = get_round() if round_no is None else int(round_no)
    b = _load_budget()
    cur = int(b.get(str(n), {}).get(script, 0))
    if cur <= 0:
        return 0
    b[str(n)][script] = cur - 1
    _save_budget(b)
    return cur - 1
