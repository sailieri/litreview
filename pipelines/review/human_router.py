"""人类参与路由引擎（主智能体）。

人类在 data/reviews/human_review_filled.md 用自然语言提意见。
本引擎按 data/reviews/_human_router_spec.md（预设规格）把每条意见分类为：
  - discussion（疑问 / 建议）：交给思考链的问答讨论环节，RAG 调研后回答、再决定改不改；
  - constraint（强制约束）：生成一个新审稿人 persona，落 data/reviews/reviewers/<id>.yaml，
    由 review_team.load_reviewers() 自动加载合并，在审稿阶段卡这道关。

设计原则：本文件只含**通用路由逻辑**，不含任何项目 / 实例特定内容；
具体内容（人类原话、生成的审稿人、可调规格）全部在 data/ 下，不污染 modules/ 代码。
"""
import glob
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules.util.llm_client import get_step_config, call_llm, load_config, extract_json  # noqa: E402

from modules.paths import data_root  # noqa: E402

REV = data_root() / "reviews"            # 跟随 config.data_root，不写死 data/
from modules.review.paths import META, REVIEWERS
SPEC = META / "_human_router_spec.md"
REVIEWERS_DIR = REVIEWERS

DEFAULT_SPEC = """# 人类参与路由规格（预设）

本规格指导主智能体如何把人类在 human_review_filled.md 的自然语言意见分类并路由。

## 分类为两类
1. discussion（疑问 / 建议）：人类对内容有疑问、想讨论、或提出可商议的改进建议。
   → 路由到「问答讨论环节」：思考链（proposer / socratic）将其作为问题调研（RAG）后回答，
     再判断是否需要改。人类作为提问者，不直接下指令。
2. constraint（强制约束）：人类明确说「必须 / 不能 / 绝不允许 / 一定要」之类的硬性要求，
   或明确指出某类问题「不行」。→ 路由到「审核环节」：生成一个新审稿人，专门卡这道关。

## 判定要点
- 含 必须 / 不能 / 禁止 / 绝不 / 一定要 / 不允许 / 应该 / 务必要 等强约束语气，且无商量余地 → constraint。
- 以问号结尾、或「是不是 / 能不能 / 为什么 / 是否 / 怎么」等探究语气 → discussion。
- 「建议 / 可以考虑 / 或许 / 不妨」等商议语气 → discussion。
- 一条意见若同时含强制与疑问，按主导意图归类；无法判断默认归 discussion（更温和）。

## 输出
对每条意见输出 JSON：
{"items":[{"bucket":"discussion|constraint","content":"<原意的自然语言要点>",
"reviewer_name":null|"<若为 constraint，给一个简短审稿人命名>",
"reviewer_focus":null|"<若为 constraint，给一句话该审稿人聚焦检查什么>"}]}
"""


ROUTER_SYS = """你是「人类参与路由主智能体」。你按下面给定的路由规格，把人类审稿意见逐条分类为
discussion（疑问 / 建议）或 constraint（强制约束），并抽取要点。
你只做分类与抽取，不做任何修改、不引入项目特定知识。

=== 路由规格 ===
{spec}

请严格按规格输出 JSON：{{"items":[{{"bucket":"discussion|constraint","content":"<要点>","reviewer_name":null|"<constraint 时命名>","reviewer_focus":null|"<constraint 时聚焦>"}}]}}。"""


def _ensure_spec():
    if not SPEC.exists():
        SPEC.write_text(DEFAULT_SPEC, encoding="utf-8")
        print(f"[human_router] wrote default spec -> {SPEC}")
    return SPEC.read_text(encoding="utf-8")


def classify_and_route(text: str, cfg=None) -> dict:
    """分类 human_review_filled.md 文本，自动落盘 constraint 审稿人，返回 discussion 问题列表。

    返回：{"discussion": [str,...], "constraints": [{"name","file"}...], "raw": [...]}
    """
    if not text or not text.strip():
        return {"discussion": [], "constraints": [], "raw": []}
    spec = _ensure_spec()
    if cfg is None:
        cfg = get_step_config("review_local", load_config())
    system = ROUTER_SYS.format(spec=spec)
    user = f"=== 人类意见原文 ===\n{text}\n=== 结束 ==="
    raw = call_llm([{"role": "system", "content": system},
                    {"role": "user", "content": user}], cfg, max_tokens=3000, temperature=0.2)
    data = extract_json(raw)
    items = data.get("items", []) if isinstance(data, dict) else []
    discussion, constraints = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        content = (it.get("content") or "").strip()
        if not content:
            continue
        if it.get("bucket") == "constraint":
            fname = _spawn_reviewer(it)
            constraints.append({"name": it.get("reviewer_name") or "匿名约束审稿人",
                                "file": fname, "content": content})
        else:
            discussion.append(content)
    print(f"[human_router] discussion={len(discussion)} constraints={len(constraints)}")
    return {"discussion": discussion, "constraints": constraints, "raw": items}


def _slug(s: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", s.strip())[:40].strip("_").lower()
    return s or "constraint"


def _spawn_reviewer(it: dict) -> str:
    """为一条 constraint 生成一个 reviewer YAML 落 data/reviews/reviewers/。

    同时保留「原始人类措辞」content 与「主智能体聚焦解读」focus：
    以人类原话为准，避免 LLM 对 focus 的转述丢掉用户原话的关键约束。
    """
    import yaml
    REVIEWERS_DIR.mkdir(parents=True, exist_ok=True)
    name = (it.get("reviewer_name") or "约束审稿人").strip()
    focus = (it.get("reviewer_focus") or "").strip()
    content = (it.get("content") or "").strip()
    rid = f"U_{_slug(name)}"
    existing = {Path(p).stem for p in glob.glob(str(REVIEWERS_DIR / "*.yaml"))}
    if rid in existing:
        rid = f"{rid}_{len(existing)}"
    # 组合约束正文：优先给聚焦解读，再附人类原话（务必以原话为准）。
    parts = []
    if focus:
        parts.append(f"约束内容（主智能体聚焦解读）：{focus}")
    if content and content != focus:
        parts.append(f"人类原话（务必以此为准）：{content}")
    elif content:
        parts.append(f"约束内容：{content}")
    constraint_block = "\n".join(parts) + "\n"
    system = (
        f"你是综述的「{name}」审稿人，由人类的一条强制约束自动生成。\n"
        f"你的唯一任务：严格检查草稿是否违反以下约束，违反则给 P0 意见并尽量给 patch。\n"
        f"{constraint_block}"
        f"判定标准：只要草稿出现与该约束相悖的表述（包括语气、用词、结构、缺失），即视为违规。\n"
        f"其他维度（结构 / 证据 / 自然度等）不属于你职责，不要评价。"
    )
    spec = {"id": rid, "name": name, "source": "human_constraint", "system": system}
    path = REVIEWERS_DIR / f"{rid}.yaml"
    path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    print(f"[human_router] spawned reviewer -> {path}")
    return str(path)


if __name__ == "__main__":
    import sys
    txt = sys.stdin.read()
    print(json.dumps(classify_and_route(txt), ensure_ascii=False, indent=2))
