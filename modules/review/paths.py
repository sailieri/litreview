"""综述管线统一目录布局（唯一真相源）。

所有模块从这里导入路径常量，避免 data/reviews 下路径散落、各模块各写一套。

目录结构（按“环节/职能”分组，每轮中间文件可追溯）：

    data/reviews/
    ├── research/      # 调研文件统一存放：RAG 检索(_rag_*) / 网络搜索(_web_*) / 用户给的调研(user_*)
    ├── thinking/      # 思考环节中间文件：thinking_brief.md / thinking_chain.md / thinking_selected.json
    ├── writing/       # 撰写环节中间文件 + 每轮修改历史：round_NN/{review_draft.md, changelog.md}
    ├── review/        # 审核环节中间文件：review_team_rN.json / unapplied_rN.md / curation_report.md / main_agent_pr.json
    ├── human/         # 人类输入：human_review_filled.md / human_writing_input.md / human_review_checklist.md / 归档
    ├── final/         # 最终综述：final_review.md(当前) + final_review_rN.md(每轮保留) + final_review_prev.md(对照基线)
    ├── reviewers/     # 用户/自动生成的专属审稿人（运行时合并，不写进代码）
    ├── meta/          # 配置与状态：_round_state.json / _human_router_spec.md / _style_refs.md / _prd.md / _spec.md
    └── logs/          # 运行日志：_run_roundN.log
"""
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent  # modules/review -> litreview
from modules.paths import data_root  # noqa: E402

REV = data_root() / "reviews"            # 跟随 config.data_root，不写死 data/

# ── 各环节 / 职能目录 ──
RESEARCH = REV / "research"      # 调研文件（RAG / 网络 / 用户）
THINKING = REV / "thinking"      # 思考环节中间文件
WRITING = REV / "writing"        # 撰写环节中间文件 + 每轮历史
REVIEW = REV / "review"          # 审核环节中间文件
HUMAN = REV / "human"            # 人类输入 / 归档 / 清单
FINAL = REV / "final"            # 最终综述（每轮保留）
REVIEWERS = REV / "reviewers"    # 用户 / 自动审稿人
META = REV / "meta"              # 配置与状态
LOGS = REV / "logs"              # 运行日志

# ── 常用单文件（按需从上面组合，便于各模块引用）──
STATE = META / "_round_state.json"
SPEC = META / "_human_router_spec.md"
STYLE_REFS = META / "_style_refs.md"
CURRENT_FINAL = FINAL / "final_review.md"
PREV_FINAL = FINAL / "final_review_prev.md"
PREV = PREV_FINAL  # 别名：上一版终稿文件（调用方按文件使用）


def round_dir(n: int) -> Path:
    """撰写环节每轮目录：writing/round_NN/（含 review_draft.md 与 changelog.md）。"""
    return WRITING / f"round_{n:02d}"


def final_path(n: int) -> Path:
    """第 n 轮最终综述（每轮保留，便于追溯修改历史）。"""
    return FINAL / f"final_review_r{n}.md"


# ── 单智能体管线：轮次化路径（唯一真相源）──
# 规则：每完成一次审核 = 一轮结束，round += 1；所有产物路径带轮次号，禁止跨轮覆盖。
def think_dir(n: int) -> Path:
    """第 n 轮思考产物目录：thinking/round_NN/。"""
    return THINKING / f"round_{n:02d}"


def draft_path(n: int) -> Path:
    """第 n 轮初稿（论证稿，主智能体写，唯一内容源）。"""
    return think_dir(n) / f"draft_r{n}.md"


def key_findings_path(n: int) -> Path:
    """第 n 轮事实锚点清单。"""
    return think_dir(n) / "key_findings.md"


def evidence_pack_path(n: int) -> Path:
    """第 n 轮引用证据包（论文→[S#]→原文片段）。"""
    return think_dir(n) / f"evidence_pack_r{n}.md"


def probe_path(n: int) -> Path:
    return think_dir(n) / f"probe_r{n}.md"


def trace_path(n: int) -> Path:
    return think_dir(n) / f"_trace_r{n}.md"


def review_draft_path(n: int) -> Path:
    """第 n 轮成稿（compile 产物）。"""
    return round_dir(n) / f"review_draft_r{n}.md"


def issues_path(n: int) -> Path:
    """第 n 轮审核意见 JSON。"""
    return REVIEW / f"review_issues_r{n}.json"


def ensure_dirs():
    """创建所有环节目录（幂等）。在管线启动处调用一次即可。"""
    for d in (RESEARCH, THINKING, WRITING, REVIEW, HUMAN, FINAL, REVIEWERS, META, LOGS):
        d.mkdir(parents=True, exist_ok=True)
