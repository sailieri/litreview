"""并行审稿团队（修订环节，意见制）。

多个不同 persona 的审稿人**并发**审同一份草稿，每人输出结构化意见清单。
**只出意见，不产出补丁、不做 apply**——意见由思考环节消化（事实类→补调研），
撰写环节追加轮统一执行修订。

每条意见带 type 字段：表述（结构/语言/风格，撰写环节直接改）| 事实（数值无源/引用错位/
证据缺口，回流思考环节补调研）。

persona 之一专做 AI-vs-人类比对（喂 _style_refs.md 真实人类综述范本 + 差异清单），
专门消灭"不自然 / 清单腔 / 技术报告腔"。

每个审稿人独立系统提示 → 独立调用 → 并行推理。"""
import glob
import json
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import yaml

from modules.util.llm_client import get_step_config, call_llm, load_config, extract_json

from modules.review.paths import (REV, REVIEWERS, META, HUMAN, REVIEW, round_dir,
                                 review_draft_path, issues_path)
REVIEWERS_DIR = REVIEWERS  # 用户/自动生成的专属审稿人（运行时合并，不写进代码）
_STYLE_PATH = META / "_style_refs.md"
# 风格范本缺失时不炸导入（首次新主题运行时 meta/ 可能还没有该文件）
STYLE_REFS = (_STYLE_PATH.read_text(encoding="utf-8") if _STYLE_PATH.exists()
              else "（未找到风格范本 _style_refs.md；请按通用人类学术综述风格判断：叙述体、单段单论点、连词推进。）")

# ── PRD 主题上下文：审稿人必须知道本综述的主题，判据才不会跑偏 ──
# 领域判据一律来自 PRD，不写死在 prompt 里。
_PRD_PATH = META / "_prd.md"


def _load_prd(max_chars: int = 6000) -> str:
    try:
        if _PRD_PATH.exists():
            return _PRD_PATH.read_text(encoding="utf-8")[:max_chars]
    except Exception:
        pass
    return "（未找到 meta/_prd.md；按通用学术综述标准审，不要预设任何领域。）"


PRD_CTX = ("""
=== 本综述的主题上下文（PRD，唯一真相源）===
""" + _load_prd() + """
=== 主题上下文结束 ===
**你的判据必须落在上述主题上**：PRD 未声明的领域（其他能源类型、其他学科的技术/产品/子类）**不得**作为审稿判据；
PRD 明令「浅铺不展开」的类型，你不得要求展开它。凡涉及「该覆盖什么」的判断，一律以 PRD 的语料边界与组织主线为准。
""")

# 通用输出格式约束（追加到每个 persona 的 system 末尾）
OUT_FMT = """
输出严格为 JSON（不要 markdown fence）：
{
  "reviewer": "<你的 id>",
  "summary": "<一句话总体评价：最强项 + 最需改的一项>",
  "prev_check": [
    {"id":"<上一轮该条意见的 id（若上轮未给 id 就写序号）>","verdict":"FIXED|PARTIAL|NOT_FIXED","note":"<一句话依据>"}
  ],
  "issues": [
    {"id":"<id>","severity":"P0|P1|P2","type":"表述|事实","location":"<章节号+段落关键词>","problem":"<具体问题>","suggestion":"<具体可执行的修改建议>"}
  ]
}
规则：
- type=表述：结构/论证/语言/风格问题，撰写环节可直接改。
- type=事实：数值无源、引用错位、事实存疑、证据缺口——这类将回流思考环节补调研，请把缺什么证据写清楚。
- 每条必须定位到具体位置；P0 排前。
- 数量 0–12 条，按严重程度排序。**没有下限**：本稿在你的聚焦维度上没问题就给 0 条，在 summary 写明「无 P0」「无意见」——
  禁止凑数，禁止把 P1/P2 抬成 P0。
- **P0 = 阻断项**，只有会实质性损害综述可信度或正确性的问题才是 P0（强命题无据、引用与内容对不上、结论方向反了、
  范围性覆盖缺失而未交代），且必须写得可复核（哪个断言、缺什么证据、为何现在过不了关）。
  分档失真会直接误导轮次决策：把可改可不改的表述问题标成 P0，系统会当致命问题处理。
- **上一轮意见的判定（输入里给了就必须写）**：逐条在 prev_check 里判 FIXED / PARTIAL / NOT_FIXED；
  已修正的**不要**再写进 issues（重复上报 = 噪声），未修正的继续写入 issues。"""

# ── 基础审稿人（预设、主题无关；项目/用户专属见 data/reviews/reviewers/）──
# 这些维度对任何学术综述都通用，不含任何项目特定内容（如某框架、某学者）。
BASE_REVIEWERS = [
    {
        "id": "A_structure",
        "name": "结构与论证连贯",
        "system": """你是学术综述的「结构与论证」审稿人。
聚焦：段落是否只发展一条论点、首句是否论点句、论证是否用转折/因果连词推进（而非并列罗列）、章节间逻辑是否连贯、是否存在"单点罗列/阅读报告腔"。
同时检查「已知局限」与摘要是否犯了**元叙述**毛病：谈“本文是怎么检索的”“库/数据库/片段”而不是谈学术意义上的局限（文献覆盖、跨研究不可比、证据强度）。

**结构主线（P0）**：PRD「组织主线」节声明了本主题允许的候选主线——全篇是否只贯彻了其中一条？
若一部分章按第一种切分、另一部分章按第二种切分（双轨混排）；或各分支章各自重复一遍完整环节小循环；
或工具/方法类内容与环节章平级并列而无嵌入关系：即报 P0，并给出统一方案。
**核心判断的证据分级（P0）**：文中强命题（“本文认为…”“这表明…”“…主导数值差异”）是否有直接量化证据？
只有间接、跨区域拼接或定性描述支撑却按结论陈述的，报 P0，要求降级为“工作假说”并写明验证路径。
**用不可比案例支撑强判断（P0）**：拿区域/分辨率/排除规则都不同的两个案例的数值对比去推因果结论 = 报 P0。
**重复论述（P1）**：同一总纲（如“跨研究不可比”“潜力分层”）是否在第 4 章及以上反复展开？应集中到第 1 章，后文一句带过。
**小节与路标（P1）**：一章同时承载 ≥2 条方法线/多个对象分支，却用连续长文写下来、没有任何 `### N.M` 小节，
或单段超过 400 字，报 P1——读者需要路标。但短章（<1000 字）无需分节，不要为对称硬加小节。
**篇幅分配（P1，双向）**：各章、各节的篇幅是否与它在主线中的分量相称？两个方向都要抓：
① 承担核心论证的部分**明显偏短**、两三句就被带过（该充实而未充实）；
② 素材多但不承担主线任务的内容展开过长，挤压了其它部分（阅读报告腔）。
报 P1 时必须写清“哪一部分偏短/偏长、应补什么素材或往哪里挪”，不要只说“不均衡”。
注意：篇幅不要求机械平均，真人综述本就有详略不均；你抓的是**与主线任务不匹配**。
**数字关系张力（P1）**：出现“A 的总体指标更优、但 B 条件下系统性偏差”（如 RMSE 更低而回归斜率偏离 1，或晴/阴天优劣互换）
这类张力时，文中是否点明这是“各区间表现不均”还是“整体更优”、对下游潜力估算意味着什么？没点明即报。
**段落密度（P1）**：一段里并列 >2 个案例/研究而无论点收束的，报 P1（拆段或表格化）。
抓这种典型病：一段塞 3+ 篇论文各一句；用 bullet/编号列表代替论述；论点间无连接词。
你的修改建议应把清单改成连贯叙述、补连接词、合并孤立句。""" + OUT_FMT,
    },
    {
        "id": "B_evidence",
        "name": "证据与事实",
        "system": """你是学术综述的「证据与事实」审稿人。
聚焦：（1）[N] 引用是否对应文末表的论文、引用与所述内容是否对得上；（2）同一数字在全文是否前后一致；
（3）数值的口径/条件/区域是否交代清楚（“全球”还是“某省”、“理论”还是“技术”）；（4）是否把单篇结论说成领域共识；
（5）是否残留 [强]/[中]/[弱] 等内部标签或"呼应 Q""aggregation""chunks"等流程词（必须删除）；（6）证据缺口是否坦诚呈现。

**重要边界**：你只能看到草稿，看不到原始文献，因此**不要**质疑某个数值本身是否可能/是否偏高偏低/
量级是否合理（如“31.1 百万 km² 接近全球陆地 20%，存疑”“236.25 GW 偏高”）——你没依据，这类判断不属于你的职责，
数值真伪由系统另一套机制（证据包 + 库内证据片段）核验，且这套机制已阻止“无源数字”进入正文。
若你确实发现**引用与内容对不上**、**同一数字前后矛盾**、**口径未交代**，那才是你要抓的。
对残留标签/流程词，务必逐条指出位置（type=表述）。
**案例选择理由（P1）**：重点展开的案例是否交代了“为何选它”（区域代表性/方法开创性/数据可复现/口径最完整）？没交代即报。
**引用年份（P1）**：引用了晚于当前年份的条目，或在线优先/预印本未注明状态，报 P1 要求核实。
**方向词与数值方向一致（P1）**：出现“高估/低估/偏高/偏低/放大/缩小/保守/乐观”等方向词时，该方向能否对得上所述数值与机制？
方向说反了（示例：参数取值使结果偏高，却写成“保守处理”）报 P1——这是专业性错误，比文笔问题严重。
**并列数值是否同口径（P1）**：把不同气候区/不同总体/不同统计口径的数值并列成“从 X 到 Y”的范围（如“量级从 16% 到 88%”），
读者会当成同一变量的分布；未声明不可比即报 P1。
**同一证据跨章一致（P1）**：同一篇文献的同一数值在多个章节出现时，数字/精度/口径是否一致？
前文写“偏差 2–4 m/s”、后文写“偏差可达数米每秒”（降精度复述）报 P1——重复且不一致比单纯重复更严重。
**篇幅与证据不匹配（P1）**：某方向文献明显偏少，却写出与其它章等长的篇幅（注水），报 P1。""" + OUT_FMT,
    },
    {
        "id": "C_naturalness",
        "name": "自然度与语言风格",
        "system": """你是学术综述的「自然度」审稿人，专门消灭 AI 写作痕迹。
抓：清单腔（bullet/编号罗列）、机械句式（"X 提出了 Y。Z 实现了 W。"重复结构）、以 [N] 领衔的句子、括号标签、过度比喻、长短句无变化、空动词（"揭示了""表明了"堆砌）。
**流程泄漏（P0）**：正文/摘要/已知局限中出现写作流程的自指词——`库内`/`库中`/`本库`/`库`/`检索到`/`未检索到`/`检索片段`/`证据包`/`知识库`/`超边`/`本轮`/`审稿人`/`追问者`/`papers.db`。
人类综述不会对读者说"本项目在一个库里没检到"；正确写法是"现有文献尚未给出……的定量证据"。发现即报 P0（type=表述），并给出改写句。
修改建议应把机械并列改成有节奏的叙述、把 [N] 领衔改成引用融入、删冗余括号。
**限定语过载（P1）**：同一限定说法（“尚未见到…的定量证据”“本综述只能…”“仍只能作定性判断”）出现 ≥3 次 = 噪音，报 P1（把真正重要的限定淹没）。
**万能词（P1）**：“口径”“维度”“范式”等抽象词高频且不写明具体所指（“口径”≥8 次），报 P1——尤其“口径不同”当万能解释时。
**模板收尾（P1）**：各章末尾出现同一句式（“本章的边界判断是…”“需指出的是…”）≥3 次，报 P1。
**批判句式单一（P2）**：全篇批判节奏高度一致（正面陈述→然而→限定→“因此目前只能视为”），报 P2 要求变奏。
**句法过载（P2）**：单句超过约 110 字、内套两层以上限定的（“若…但…即便…也…”），报 P2 并要求拆成 2–3 句——
尤其注意段落末的总结句，它常是三层限定叠在一起的。
**自造术语与比喻回收（P1）**：出现非领域标准术语（把几个既有概念糅成一个自造词）报 P1，并给标准表述；
开头提出的结构隐喻/框架在后文未回收、只出现一次，报 P1（要么删，要么在对应章节回扣）。
**指令性措辞（P1）**：“必须引入”“应该建立”“务须”这类祈使句 = 项目建议书语气，应改“尚需引入”“有待建立”，发现即报。""" + OUT_FMT,
    },
    {
        "id": "D_ai_vs_human",
        "name": "AI-vs-人类比对（风格范本）",
        "system": """你是「AI-vs-人类综述比对」审稿人。下面是真实发表的人类学术综述的风格范本与"人类 vs AI 写作差异清单"，你据此逐条比对当前草稿，找出仍像 AI 的地方。

=== 风格范本与差异清单 ===
""" + STYLE_REFS + """
=== 你的任务 ===
逐条对照差异清单（转折连词 / 单段落单论点 / 数字嵌入叙事 / 讨论处点出为何重要 / 引用自然融入），指出草稿中仍像 AI 的具体位置，并给具体改法。
**过度自证（P1）**：人工作者不会反复向读者声明自己的文献边界。若“本文只覆盖…”“尚未见到…的定量证据”“受文献所限…”这类自证句反复出现（≥4 处），
报 P1——把边界集中到第 1 章与“已知局限”，正文只在真正需要限定处出现一次。
**结构过于工整（P1）**：若每章都是“总述→案例→边界总结”三段式、每段长度接近，报 P1（真人综述的节奏是不均的）。""" + OUT_FMT,
    },
    {
        "id": "E_completeness",
        "name": "完整性与批判性",
        "system": """你是学术综述的「完整性与批判性」审稿人。
聚焦：（1）分析框架/线索是否真成分析透镜——每章是否体现 ≥2 个维度的交互，还是又退回孤立描述；（2）章节深度是否均衡（某章过薄）；（3）是否给出贡献声明 / 方向性判断；（4）矛盾是否以"真分歧 vs 假矛盾（基准/场景差异）"区分并说明根因；（5）已知局限是否诚实。
**横切维度覆盖（缺 2 个以上报 P0）**：维度清单取自 PRD「横切维度」节（不要用你自己熟悉的领域清单）。
逐维检查是否各有落点：整篇缺 2 个以上 = P0（综述盲区），缺 1 个 = P1。PRD 未声明横切维度时，按通用四类兜底：
① 时间维度（长期变化/情景/趋势）；② 系统或外部约束；③ 技术演进；④ 政策制度。
**子类覆盖偏斜（P1）**：本主题的某类对象是否只写了其中一个分支/子类，而该子类并非该类的主体？
未交代“为何只用这个子类”即报 P1；缺失的若是该类别在体量或重要性上的主体分支（依据 PRD 语料边界与正文自身交代判断）报 P0——读者会把子类当成整体。
注意：只报**本主题**的对象分支，不要按 PRD 之外的领域清单点名。
**篇幅与证据/重要性不匹配（P1，双向）**：证据厚度、研究成熟度差异很大的两个维度，是否被写成等长（读者会误以为成熟度相当）？
以及是否有本该展开的关键方向只用了两三句话？按“该长的短了 / 该短的长了”分别指出，并给出具体的补写或压缩建议。
**摘要（P1）**：是否 > 300 字、是否堆了方法细节与缺口清单、是否失去“钩子”功能。
**跨文献综合 vs 逐篇摘要（P1）**：重要主题是否给出共识/分歧/分歧原因/演化趋势，还是停在“A 做了 X，B 做了 Y”。
对"框架仅贴标签未分析"或"深度不足"处，给具体补写建议。""" + OUT_FMT,
    },
    {
        "id": "G_character",
        "name": "人物与归因描写",
        "system": """你是学术综述的「人物与归因描写」审稿人，专门发现"对人物的描写是否得当"这类异常——这是其他审稿维度容易忽略、但人类读者一眼就能看出的问题。

聚焦以下信号：
1. 过度强调单一人物：某位学者被反复提及的次数，是否远超其在该具体论点上的实际贡献？是否存在"为提而提"、与所讨论内容无关的人名堆砌？
2. 归因失当：把领域共识、或本属于多人/多团队的工作，归到单一人物名下；或为衬托某个框架而反复点名提出者。
3. 作者待遇不均：在同等相关度下，是否只突出某一位而冷落其他真正相关的作者/团队？综述应平等对待贡献相近的作者。
4. 人名是否与论点绑定：每次提及人物，是否都在服务于某一具体论证？还是浮于表面的装饰性提及？
5. 框架归属是否自然：分析框架的提出者是否在引入该框架时作一次中性归属即可，而非反复出现。

你的修改建议：删去与论述无关的人名、把反复点名改为"该框架/该模型"等中性指称、把"由某人提出"式的强调改为事实陈述。""" + OUT_FMT,
    },
]


def load_reviewers():
    """合并基础审稿人 + data/reviews/reviewers/ 下用户/自动生成的审稿人。

    文件名以下划线开头的（如 _example.yaml）视为说明/模板，跳过。
    每个文件：{'id','name', system?|focus?}，system 缺失时由 focus 自动拼装并追加 OUT_FMT。
    """
    reviewers = [dict(r) for r in BASE_REVIEWERS]
    if REVIEWERS_DIR.is_dir():
        files = (sorted(glob.glob(str(REVIEWERS_DIR / "*.yaml")))
                 + sorted(glob.glob(str(REVIEWERS_DIR / "*.yml")))
                 + sorted(glob.glob(str(REVIEWERS_DIR / "*.json"))))
        for f in files:
            stem = Path(f).stem
            if stem.startswith("_"):
                continue
            try:
                if f.endswith(".json"):
                    spec = json.loads(Path(f).read_text(encoding="utf-8"))
                else:
                    spec = yaml.safe_load(Path(f).read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[review] skip {Path(f).name}: parse error {e}")
                continue
            if not isinstance(spec, dict) or "id" not in spec or "name" not in spec:
                print(f"[review] skip {Path(f).name}: missing id/name")
                continue
            system = spec.get("system")
            if not system:
                focus = spec.get("focus", "")
                system = f"你是综述的「{spec['name']}」审稿人。\n聚焦：{focus}\n" + OUT_FMT
            elif OUT_FMT not in system:
                system = system + OUT_FMT
            reviewers.append({"id": spec["id"], "name": spec["name"],
                              "system": system, "source": spec.get("source", "user")})
    return reviewers

# ── 人工审稿清单（人类作为第六位审稿人）──
HUMAN_CHECKLIST = (HUMAN / "human_review_checklist.md")
HUMAN_FILLED = (HUMAN / "human_review_filled.md")

HUMAN_CHECKLIST_TMPL = """# 人工审稿清单（F_human 槽位，可选）

> 主通道：直接在 `human_review_filled.md` 用**自然语言**写你的意见即可，
> 主智能体会按 `data/reviews/_human_router_spec.md` 自动分类：
>   · 疑问 / 建议 → 注入「问答讨论环节」（思考链调研后回答，再决定改）
>   · 强制约束（必须 / 不能 / 绝不允许）→ 自动生成一个新审稿人卡关
> 本清单是**可选**的结构化补充：若你更习惯逐项勾选，可填此表，流水线会并入 F_human 槽位。
> 评分建议：✓ 通过 / △ 待改 / ✗ 严重问题，并写一句说明。

## F1. 结构与论证
- [ ] 段落是否只发展一条论点、首句即论点句？说明：
- [ ] 论证是否用转折/因果连词推进，而非并列罗列？说明：

## F2. 证据与事实
- [ ] 是否存在无源数字 / 单篇结论当共识？说明：
- [ ] [N] 引用是否都对应文末文献表？说明：

## F3. 自然度（消灭 AI 腔）
- [ ] 是否有清单腔 / 编号罗列 / 单点读报腔？说明：
- [ ] 引用是否融入叙述而非以 [N] 领衔？说明：

## F4. 完整性与批判性
- [ ] 各章深度是否均衡？哪章过薄？说明：
- [ ] 矛盾是否区分"真分歧 vs 假矛盾（基准/场景差异）"？说明：

## F5. 贡献与局限
- [ ] 是否给出贡献声明 / 方向性判断？说明：
- [ ] 已知局限是否诚实呈现？说明：

---
填完后此文件即作为你的评审正文；流水线会自动并入 F_human 槽位。自然语言意见请写在 `human_review_filled.md`。
"""


def emit_human_checklist():
    # 目录可能不存在（归档/全新环境）：先建目录再 write_text，否则整轮审核直接失败。
    HUMAN_CHECKLIST.parent.mkdir(parents=True, exist_ok=True)
    HUMAN_CHECKLIST.write_text(HUMAN_CHECKLIST_TMPL, encoding="utf-8")
    print(f"[review] human checklist -> {HUMAN_CHECKLIST}")
    return str(HUMAN_CHECKLIST)


def _parse_human_filled():
    if not HUMAN_FILLED.exists():
        return None
    txt = HUMAN_FILLED.read_text(encoding="utf-8")
    return {"reviewer": "F_human", "name": "人工审稿（人类）",
            "summary": "人类审稿人填写", "issues": [],
            "filled": True, "raw": txt[:6000]}


def _fmt_prev_issues(issues: list) -> str:
    out = []
    for i, it in enumerate(issues or [], 1):
        if not isinstance(it, dict):
            continue
        out.append(f"{i}. [{it.get('severity', '?')}|{it.get('type', '?')}] "
                   f"{it.get('location', '')} — {it.get('problem', '')}")
    return "\n".join(out) or "（上一轮无意见）"


# 上一轮成稿进上下文的截断上限（与「上一轮自己的意见」「本轮成稿」合构成三段式记忆）
PREV_DRAFT_MAX = 45000


def _load_prev(round_no: int | None):
    """三段式跨轮记忆：上一轮成稿 + 上一轮该审稿人自己的意见 + 本轮成稿。

    只看最近一轮（更早轮次不注入）：目的是让审稿人能看到自己上轮指出的问题是否被修，
    同时控制上下文长度。返回 (prev_draft_text, {reviewer_id: [issues]})。"""
    if not round_no or round_no <= 1:
        return "", {}
    prev_draft = ""
    try:
        p = review_draft_path(round_no - 1)
        if p.exists():
            prev_draft = p.read_text(encoding="utf-8")[:PREV_DRAFT_MAX]
    except Exception as e:
        print(f"[review] 上一轮成稿读取失败（不影响审稿）：{e}")
    prev_map: dict = {}
    try:
        ip = issues_path(round_no - 1)
        if ip.exists():
            data = json.loads(ip.read_text(encoding="utf-8"))
            for rv in data.get("reviewers", []):
                prev_map[rv.get("reviewer")] = rv.get("issues") or []
    except Exception as e:
        print(f"[review] 上一轮意见读取失败（不影响审稿）：{e}")
    return prev_draft, prev_map


def _review_one(cfg, reviewer, draft, prev_draft: str = "", prev_issues: list | None = None):
    parts = []
    if prev_issues:
        parts.append("=== 上一轮你（" + reviewer["id"] + "）提出的意见 ===\n" + _fmt_prev_issues(prev_issues))
    if prev_draft:
        parts.append("=== 上一轮定稿全文（供你核对修改情况）===\n" + prev_draft +
                     "\n=== 上一轮定稿结束 ===")
        parts.append("=== 说明 ===\n先逐条判定「上一轮你提的意见」在本轮稿中的状态，写入 prev_check"
                     "（FIXED / PARTIAL / NOT_FIXED）；已修正的**不要**再写进 issues，未修正的继续报；"
                     "然后再报本轮新发现的问题。只输出一个 JSON 对象。")
    parts.append(f"以下是待审综述定稿全文（本轮）。请作为「{reviewer['name']}」审稿人，按你的聚焦给出结构化意见。\n\n"
                 f"=== 草稿开始 ===\n{draft}\n=== 草稿结束 ===")
    user = "\n\n".join(parts)
    messages = [{"role": "system", "content": reviewer["system"]},
                {"role": "user", "content": user}]
    raw = call_llm(messages, cfg, max_tokens=6000, temperature=0.3)
    data = extract_json(raw)
    if not isinstance(data, dict):
        # 解析失败不能静默变成「0 条意见」（会被当成「审稿人无异议」）：
        # 原提示重试一次 + 强化 JSON 约束，仍失败则标记 parse_failed 供上层告警。
        print(f"[review] {reviewer['id']} JSON 解析失败，重试一次…")
        retry = messages + [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content": "上一条输出不是合法 JSON，无法解析。"
                                         "请**只输出一个 JSON 对象**，不要 markdown fence、不要任何解释文字、"
                                         "不要在 JSON 前后添加任何内容；字段：reviewer, summary, issues"
                                         "（每条含 id, severity, type, location, problem, suggestion）。"}]
        raw2 = call_llm(retry, cfg, max_tokens=6000, temperature=0.1)
        data = extract_json(raw2)
        if not isinstance(data, dict):
            print(f"[review] {reviewer['id']} 重试仍失败 —— 本轮该审稿人无意见产出（已标记 parse_failed）")
            return {"reviewer": reviewer["id"], "name": reviewer["name"],
                    "summary": "解析失败（已重试）", "issues": [],
                    "parse_failed": True, "raw": (raw + "\n---重试---\n" + raw2)[:6000]}
    data["reviewer"] = reviewer["id"]
    data["name"] = reviewer["name"]
    if isinstance(data.get("prev_check"), list):
        data["prev_check"] = [x for x in data["prev_check"] if isinstance(x, dict)]
    # 净化 issues：仅保留 dict 且含 problem/patch/suggestion 之一的条目，
    # 丢弃 json_repair 偶发混入的脏元素（int/str），避免 apply_patches 崩溃
    raw_issues = data.get("issues")
    if isinstance(raw_issues, list):
        data["issues"] = [it for it in raw_issues
                          if isinstance(it, dict)
                          and ("problem" in it or "patch" in it or "suggestion" in it)]
    return data


# ---------------------------------------------------------------------------
# 主编自检（H_curation）：稽核主智能体 / 人工提交的修订 PR
# ---------------------------------------------------------------------------
CURATION_PROMPT = """你是一位严谨的「主编自检」审稿人。你不负责发现新问题，只稽核【主智能体 / 人工提交的修订 PR】是否真正解决了所标注的问题，以及是否引入新的不一致。

输入：
1. 修订清单：每条含 问题ID、问题描述、修订前片段、修订后片段
2. 修订后的全文草稿

请逐条判定每条修订的稽核结论：
- RESOLVED：修订后片段确实消除了原问题（如机械收束词消失、公式已补充、指令腔融化、人物归属中性、重复数字已去、深度已补）
- PARTIAL：部分解决，仍有残留
- REGRESSION：修订引入新问题（引用错位、数字矛盾、语句断裂、逻辑重复、出现新的机械词）

仅输出 JSON 数组（不要 markdown fence），每条：
{"id":"<问题ID>","verdict":"RESOLVED|PARTIAL|REGRESSION","note":"<一句话说明>"}
"""


def run_curation(draft_text: str, checklist: list, cfg=None):
    """稽核主智能体 / 人工提交的修订 PR。

    checklist: list of {id, desc, before, after}
    返回：list of {id, verdict, note}
    """
    if cfg is None:
        cfg = get_step_config("review_local", load_config())
    if not checklist:
        return []
    items = "\n".join(
        f"- id={c.get('id')} | 问题：{c.get('desc', '')} | 修订前：{(c.get('before') or '')[:240]} | 修订后：{(c.get('after') or '')[:240]}"
        for c in checklist)
    user = (f"=== 修订清单（待稽核）===\n{items}\n\n"
            f"=== 修订后全文草稿 ===\n{draft_text}\n=== 结束 ===")
    messages = [{"role": "system", "content": CURATION_PROMPT},
                {"role": "user", "content": user}]
    raw = call_llm(messages, cfg, max_tokens=2000, temperature=0.2)
    data = extract_json(raw)
    if isinstance(data, list):
        return data
    return []


def run(draft_text: str = None, round_no: int = None, out_path: Path = None):
    cfg = get_step_config("review_local", load_config())
    if draft_text is None:
        rd = round_dir(round_no) if round_no else round_dir(1)
        draft_text = (rd / "review_draft.md").read_text(encoding="utf-8")
    reviewers = load_reviewers()
    for _r in reviewers:                      # 注入 PRD 主题上下文（判据不跑偏）
        if PRD_CTX not in _r["system"]:
            _r["system"] = _r["system"] + PRD_CTX
    prev_draft, prev_map = _load_prev(round_no)
    if prev_draft or prev_map:
        print(f"[review] 跨轮记忆：上一轮成稿 {len(prev_draft)} 字符 + 上一轮意见 "
              f"{sum(len(v) for v in prev_map.values())} 条")
    print(f"[review] draft {len(draft_text)} chars, {len(reviewers)} reviewers concurrent")

    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_review_one, cfg, r, draft_text, prev_draft, prev_map.get(r["id"])): r
                for r in reviewers}
        for f in futs:
            r = futs[f]
            try:
                res = f.result()
                results.append(res)
                print(f"[review] {r['id']} done: {len(res.get('issues', []))} issues")
            except Exception as e:
                results.append({"reviewer": r["id"], "name": r["name"],
                                "summary": f"ERROR: {e}", "issues": []})
                print(f"[review] {r['id']} ERROR: {e}")

    out = out_path or (REVIEW / "review_team_r1.json")
    out.write_text(json.dumps({"reviewers": results}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[review] -> {out}")
    return str(out)


def run_with_human(draft_text: str = None, round_no: int = None, out_path: Path = None):
    """同 run()，但额外保留第六位"人类审稿"槽位：发出清单，若已填写则并入。"""
    cfg = get_step_config("review_local", load_config())
    if draft_text is None:
        rd = round_dir(round_no) if round_no else round_dir(1)
        draft_text = (rd / "review_draft.md").read_text(encoding="utf-8")
    reviewers = load_reviewers()
    for _r in reviewers:                      # 注入 PRD 主题上下文（判据不跑偏）
        if PRD_CTX not in _r["system"]:
            _r["system"] = _r["system"] + PRD_CTX
    prev_draft, prev_map = _load_prev(round_no)
    if prev_draft or prev_map:
        print(f"[review] 跨轮记忆：上一轮成稿 {len(prev_draft)} 字符 + 上一轮意见 "
              f"{sum(len(v) for v in prev_map.values())} 条")
    print(f"[review] draft {len(draft_text)} chars, {len(reviewers)} LLM reviewers concurrent")

    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_review_one, cfg, r, draft_text, prev_draft, prev_map.get(r["id"])): r
                for r in reviewers}
        for f in futs:
            r = futs[f]
            try:
                res = f.result()
                results.append(res)
                print(f"[review] {r['id']} done: {len(res.get('issues', []))} issues")
            except Exception as e:
                results.append({"reviewer": r["id"], "name": r["name"],
                                "summary": f"ERROR: {e}", "issues": []})
                print(f"[review] {r['id']} ERROR: {e}")

    # 第六位审稿人：人类（随时可填，流水线保留槽位）
    emit_human_checklist()
    human = _parse_human_filled()
    if human:
        results.append(human)
        print("[review] human review filled -> merged as F_human")
    else:
        results.append({"reviewer": "F_human", "name": "人工审稿（人类）",
                        "status": "pending", "checklist": str(HUMAN_CHECKLIST),
                        "summary": "人类审稿待填写（清单已生成）", "issues": []})

    out = out_path or (REVIEW / "review_team_r1.json")
    out.write_text(json.dumps({"reviewers": results}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[review] -> {out} (含 F_human 槽位)")
    return str(out)


if __name__ == "__main__":
    run()
