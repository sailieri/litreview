"""check_draft.py —— V2.3 综述管线脚本③：初稿硬断言。

**ERROR 未清零不许 compile。** 断言清单（SOP §4）：

| 断言 | 级别 | 治什么 |
|---|---|---|
| 每章存在且字数 ≥ 300 | ERROR | 空章 / 占位 |
| 每章至少 1 个 [S#]/[E#] | ERROR | 无证据章 |
| 正文每个编号在 _kb_refs.json 中存在 | ERROR | 编造引用 |
| 正文无“内部流程词”（库内/库中/本库/数据库/检索片段…） | ERROR | 暴露写作流程 |
| 正文数值能在 evidence_pack 原文中命中 | WARN | 数值不可溯源 |

用法：
    python check_draft.py --round 1
    python check_draft.py --round 1 --min-chars 300 --json
"""
import argparse
import datetime
import json
import re
import sys
import difflib
from pathlib import Path


# 把仓库根加入 sys.path（脚本在子目录里运行）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from modules.review.paths import draft_path, evidence_pack_path, think_dir
from modules.review import refs as refs_mod
from modules.review.sections import parse_sections, STOP_RE  # noqa: E402

# 章节解析在 modules/review/sections.py（初稿与成稿共用一套实现）
CITE_RE = re.compile(r"\[([SE]\d+)\]")
LEAD_CITE_RE = re.compile(r"(?:^|[。；！？\n])\s*(?:[-*·]\s*)?\[([SE]\d+)\]")   # 句首编号（禁）
NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# 内部流程词（ERROR）：综述面向人类读者，不得暴露“本项目在一个库里检索”的写作流程。
# 提示词本身会鼓励这类措辞（例如“查不到就写库中未检索到”），这里用机械断言兜住。
PROCESS_WORDS = [
    "库内", "库中", "本库", "该库", "入库", "库里", "论文库", "文献库",
    "papers.db", "papers_db", "数据库检索", "检索片段", "检索到", "未检索到",
    "证据包", "kb_query", "知识库", "超边", "chunks", "aggregation", "呼应Q",
    "本轮", "审稿人", "追问者", "主智能体", "流水线", "管线",
]
# 把“库中未检索到”改成“就本文所覆盖的文献而言”，仍会暴露检索过程——
# 只要主语是“本文/本综述”，读者就看得出这是检索范围的自我报告。
# 区分：**正文与摘要里禁止**；「已知局限」一节里允许（那是交代分析范围的正规位置）。
PROCESS_WORDS_SCOPE = [
    "本文覆盖", "本文所覆盖", "覆盖的文献", "本文的分析范围", "本文检索",
    "本综述覆盖", "本文所涉", "本文纳入", "本文选取的文献",
]
PROCESS_OK_CTX = ["数据库", "数据集", "图谱", "知识"]      # 白名单：只当整词“数据库”等独立出现时不报（如“公开数据库”）

# 场景对照词表（WARN）：一个能源类型只写了它的一个子类，
# 而那个子类不是装机体量主体（典型：风能只写海上、陆上 0 次）——读者会把子类当成整个能源。
# 元组 =（描述, 一侧词, 另一侧词）
SCENE_PAIRS = [
    ("风能：海上 ↔ 陆上", ("海上",), ("陆上",)),
    ("光伏：屋顶/分布式 ↔ 集中式/地面电站", ("屋顶", "分布式"), ("集中式", "地面电站")),
    ("生物质：农业残量 ↔ 木质/能源作物", ("秸秆", "农业残量"), ("木质", "森林残量", "能源作物")),
]


def find_process_words(text: str) -> list[dict]:
    """扫描内部流程词（跳过文末参考文献表与代码块）。"""
    hits = []
    for w in PROCESS_WORDS:
        for m in re.finditer(re.escape(w), text):
            ln = text.count("\n", 0, m.start()) + 1
            ctx = text[max(0, m.start() - 25):m.end() + 25].replace("\n", " ")
            hits.append({"word": w, "line": ln, "context": ctx})
    return hits



def _keep_num(s: str) -> bool:
    if "." in s:
        return len(s) >= 3
    return len(s) >= 2


def build_pack_if_missing(r: int, draft_text: str) -> str:
    """证据包缺失时按初稿引用的编号现建（幂等，不覆盖已有文件）。"""
    p = evidence_pack_path(r)
    if p.exists():
        return p.read_text(encoding="utf-8")
    sids = sorted(set(CITE_RE.findall(draft_text)), key=lambda s: int(s[1:]))
    pack = refs_mod.build_evidence_pack(refs_mod.load(), sids)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pack, encoding="utf-8")
    print(f"[check] 证据包不存在，已按初稿引用现建 -> {p}")
    return pack


def check(round_no: int | None = None, draft_file=None, min_chars: int = 300) -> dict:
    """硬断言（程序化入口，main_agent.py 用）。返回 result dict；不打印。"""
    from modules.review import rounds
    r = round_no or rounds.get_round()
    dp = Path(draft_file) if draft_file else draft_path(r)
    if not dp.exists():
        return {"round": r, "draft": str(dp), "sections": 0, "citations": 0,
                "ok": False, "errors": [f"找不到初稿 {dp}"], "warnings": [],
                "unsourced_numbers": []}

    draft = dp.read_text(encoding="utf-8")
    secs = parse_sections(draft)
    refs = refs_mod.load()
    pack = build_pack_if_missing(r, draft)

    errors, warns = [], []

    if not secs:
        errors.append("初稿没有解析出任何章节（需 `## N. 标题` 或 `## 第N章 标题` 格式；小节用 `### N.M 标题`）")

    for s in secs:
        n = len(s["body"])
        if n < min_chars:
            errors.append(f"第{s['num']}章「{s['title']}」正文 {n} 字 < {min_chars}（空章/占位）")
        if not CITE_RE.search(s["body"]):
            errors.append(f"第{s['num']}章「{s['title']}」没有任何 [S#]/[E#] 引用（无证据章）")

    all_ids = CITE_RE.findall(draft)
    missing = sorted({i for i in all_ids if i not in refs})
    if missing:
        errors.append(f"正文引用了引用表中不存在的编号：{missing}（编造引用）")
    if not all_ids:
        errors.append("初稿全文没有任何 [S#]/[E#] 引用")

    # 句首编号（禁）：如「[S29] 库内证据显示…」——只查正文章节，不查文末文献表
    lead = []
    for s in secs:
        for m in LEAD_CITE_RE.finditer(s["body"]):
            ctx = s["body"][max(0, m.start() - 20):m.end() + 40].replace("\n", " ")
            lead.append({"section": s["num"], "cite": m.group(1), "context": ctx})
    if lead:
        errors.append(f"{len(lead)} 处句子以引用编号开头（禁止编号领衔句，编号应跟在事实句末）")

    # 内部流程词（ERROR）：不能暴露写作流程
    # 扫全文（标题+摘要+章节+已知局限），只排除文末参考文献表
    main_text = draft.split("## 参考文献")[0]
    proc = find_process_words(main_text)
    if proc:
        uniq = sorted({h["word"] for h in proc})
        errors.append(
            f"{len(proc)} 处内部流程词（{', '.join(uniq)}）——综述面向人类读者，"
            f"不得暴露“库/检索/轮次/审稿”等写作流程。改写例："
            f"“库中未检索到 X”→ “现有文献尚未给出 X 的定量证据”；"
            f"“库内证据显示”→ 直接陈述事实并给 [S#]。首次出现："
            + "; ".join(f"第{h['line']}行「{h['word']}」…{h['context']}" for h in proc[:3]))

    # “本文覆盖…”这类自指（ERROR，但限定在正文+摘要；已知局限里允许）
    body_scope = re.split(r"^##\s*已知局限", main_text, flags=re.M)[0]
    scope_spans = []
    for w in PROCESS_WORDS_SCOPE:
        for m in re.finditer(re.escape(w), body_scope):
            scope_spans.append((m.start(), m.end(), w))
    # 合并重叠命中：“就本文覆盖的文献而言”同时含“本文覆盖”与“覆盖的文献”，
    # 不去重会把一处自指算成两处。
    scope_hits = []
    for s, e, w in sorted(scope_spans):
        if scope_hits and s < scope_hits[-1][1]:
            scope_hits[-1][1] = max(scope_hits[-1][1], e)
            continue
        scope_hits.append([s, e, w])
    if scope_hits:
        uniq = sorted({h[2] for h in scope_hits})
        errors.append(
            f"{len(scope_hits)} 处“本文/本综述+覆盖/检索”式自指（{', '.join(uniq)}）——主语是“本文”就是在向读者报告检索范围。"
            f"正文与摘要里一律去掉主语：“本文覆盖的文献中未见 X”→“X 仍未见报道”；"
            f"“本文覆盖的以农业残量为主”→“现有定量证据以农业残量为主”。"
            f"需要交代分析范围时，集中写到「已知局限」一节。首次出现："
            + "; ".join(f"第{body_scope.count(chr(10), 0, h[0]) + 1}行「{h[2]}」…"
                        f"{body_scope[max(0, h[0] - 25):h[1] + 25].replace(chr(10), ' ')}"
                        for h in scope_hits[:3]))

    # 模板句重复（WARN）：同一口头禅反复出现 = AI 味
    for phrase in ("本章的适用条件判断", "库内证据显示"):
        cnt = draft.count(phrase)
        if cnt >= 2:
            warns.append(f"模板句「{phrase}」出现 {cnt} 次（建议换措辞/自然叙述）")

    # ── 语言体检（WARN）──────────────────────────────
    # 治：限定语过载 / 万能词 / 模板收尾 / 摘要过长 / 引用年份异常
    abstract = ""
    if "## 摘要" in draft:
        tail = draft.split("## 摘要", 1)[1]
        abstract = tail.split("\n## ", 1)[0]
        na = len(re.sub(r"\s", "", abstract))
        if na > 300:
            warns.append(f"摘要 {na} 字（目标 ≤300）——方法细节与缺口清单应移到引言，摘要只留问题+方法链+核心发现+最大缺口")
        # 摘要与引言重复（WARN）：同一套判断在两处近乎逐字重复
        intro = ""
        for s in secs:
            if s["num"] == "1":
                intro = s["body"]
                break
        if intro and abstract:
            a = re.sub(r"\s|\[[SE]\d+\]", "", abstract)
            worst = (0.0, "")
            for p in [x for x in re.split(r"\n\s*\n", intro) if x.strip()]:
                b = re.sub(r"\s|\[[SE]\d+\]", "", p)
                sim = difflib.SequenceMatcher(None, a, b).ratio()
                if sim > worst[0]:
                    worst = (sim, p)
            if worst[0] > 0.45:
                warns.append(f"摘要与引言某段重复度 {worst[0]:.2f}（>0.45）——摘要只留压缩版，引言保留展开版："
                             f"「{worst[1][:40]}…」")

    # 长句（WARN）：句法过载也是读者负担。
    # 阈值定在 110 字：这类文本天生句子长，阈值太低只会制造噪音；
    # 只做“优先拆最长的几句”的导航，不要求清零。
    long_sents = []
    for s in secs:
        body = re.sub(r"\[[SE]\d+\]", "", s["body"])
        for sent in re.split(r"(?<=[。！？；])", body):
            sent = sent.strip()
            n = len(re.sub(r"\s", "", sent))
            if n > 110:
                long_sents.append({"section": s["num"], "chars": n, "head": sent[:36]})
    if len(long_sents) >= 3:
        top = sorted(long_sents, key=lambda x: -x["chars"])[:3]
        warns.append(f"{len(long_sents)} 个句子超过 110 字（句法过载，不必清零，优先拆最长的几句）："
                     + "; ".join(f"第{x['section']}章 {x['chars']}字「{x['head']}…」" for x in top)
                     + "——限定条件多时宁可另起一句，不要把三层限定套在一句里")
    for phrase in ("尚未见到", "尚未给出", "本综述只能", "只能作定性判断", "仍只能作定性", "本文只覆盖"):
        cnt = draft.count(phrase)
        if cnt >= 3:
            warns.append(f"限定语「{phrase}」出现 {cnt} 次（同一限定说法全篇 ≤2 次，限定要写明具体缺什么）")
    for word, lim in (("口径", 6), ("维度", 12), ("范式", 8)):
        cnt = draft.count(word)
        if cnt > lim:
            warns.append(f"万能词「{word}」出现 {cnt} 次（建议 ≤{lim}）——每次出现都要写明具体所指（如折减系数/排除规则/分辨率）")
    for phrase in ("本章的边界判断", "本章需指出", "需指出的是", "本章的适用条件"):
        cnt = draft.count(phrase)
        if cnt >= 3:
            warns.append(f"模板收尾「{phrase}」出现 {cnt} 次（章末句式重复，建议参差化或删部分总结句）")
    # 章末总结句式雷同：统计每章最后一句的开头引导词
    closes = []
    for s in secs:
        lines = [l.strip() for l in s["body"].split("\n") if l.strip()]
        if not lines:
            continue
        sent = re.split(r"(?<=[。！？])", lines[-1])
        sent = [x for x in sent if x.strip()]
        if sent:
            closes.append(sent[-1][:14])
    for kw in ("因此", "这说明", "这提醒", "这意味着", "需要说明的是", "可见", "综上"):
        n = sum(1 for c in closes if c.startswith(kw))
        if n >= 3:
            warns.append(f"{n} 个章节以「{kw}」开头的总结句收尾（章末句式雷同，建议错开或删部分总结）")
    y_now = datetime.date.today().year
    # ── 长段与分节（WARN：呈现问题，不是内容问题）────
    long_paras = []
    for s in secs:
        for p in [x for x in re.split(r"\n\s*\n", s["body"]) if x.strip()]:
            n = len(re.sub(r"\s", "", p))
            if n > 400 and not p.lstrip().startswith("|"):
                long_paras.append({"section": s["num"], "chars": n, "head": p[:40]})
    if long_paras:
        top = sorted(long_paras, key=lambda x: -x["chars"])[:4]
        warns.append(
            f"{len(long_paras)} 个段落超过 400 字（最长 {top[0]['chars']} 字）："
            + "; ".join(f"第{x['section']}章 {x['chars']}字「{x['head']}…」" for x in top)
            + "——长段应拆段；承载多条方法线的章应用 `### N.M` 小节给出路标")
    for s in secs:
        n = len(re.sub(r"\s", "", s["body"]))
        if n > 2000 and not s.get("subs"):
            warns.append(f"第{s['num']}章「{s['title']}」{n} 字且无小节（`### N.M`）——"
                         f"已达需要小标题分层的规模，连续长文会让读者失去路标")
        if s.get("sub_mismatch"):
            warns.append(f"第{s['num']}章内有小节编号与所在章不一致：{s['sub_mismatch'][:3]}"
                         f"——小节编号必须写成 本章号.序号（如第2章内写 `### 2.1`）")

    # ── 场景覆盖偏斜（WARN）────────
    for label, side_a, side_b in SCENE_PAIRS:
        na = sum(draft.count(w) for w in side_a)
        nb = sum(draft.count(w) for w in side_b)
        if na >= 3 and nb == 0:
            warns.append(f"场景覆盖偏斜（{label}）：只出现「{side_a[0]}」一侧 {na} 次，另一侧全篇 0 次"
                         f"——若该侧素材确实少，需写明“本文覆盖的该子类证据以 X 为主”；"
                         f"若它是装机体量的主体（如陆上风电），必须补上")

    # ── 术语与语气（WARN）────────────
    for w in ("多重回乘",):
        cnt = draft.count(w)
        if cnt:
            warns.append(f"非标准术语「{w}」出现 {cnt} 次——改用标准表述（如“需依次乘以……”）")
    cmds = []
    for m in re.finditer(r"(?:必须|务须|应该|应当)(?:引入|采用|考虑|建立|开展|纳入|依赖|把|走向)[^。；\n]{0,30}", main_text):
        cmds.append(m.group(0))
    if cmds:
        warns.append(f"{len(cmds)} 处指令性措辞（综述不是项目建议书）："
                     + "; ".join(f"「{c}」" for c in cmds[:3])
                     + "——改写例：“必须引入动态技术参数”→“这一问题的回答尚需引入动态技术参数")

    future_years = []
    for sid, rec in (refs or {}).items():
        y = rec.get("year") if isinstance(rec, dict) else None
        if isinstance(y, int) and y > y_now and sid in set(all_ids):
            future_years.append(f"{sid}({y})")
    if future_years:
        warns.append(f"{len(future_years)} 个引用年份晚于当前年（{y_now}）：{future_years[:6]}——需核实是否在线优先/预印本并在文献表注明")

    # 数值可溯源（WARN）：正文数值必须能在证据包原文中逐字命中
    pack_nums = {m.group(0) for m in NUM_RE.finditer(pack)}
    unsourced = []
    for s in secs:
        body = CITE_RE.sub("", s["body"])
        for m in NUM_RE.finditer(body):
            tok = m.group(0)
            if not _keep_num(tok):
                continue
            if tok not in pack_nums:
                ctx = body[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")
                unsourced.append({"section": s["num"], "number": tok, "context": ctx})
    if unsourced:
        warns.append(f"{len(unsourced)} 处数值未在证据包原文中命中（可能无源或证据包被截断）")

    return {
        "round": r, "draft": str(dp), "sections": len(secs),
        "citations": len(set(all_ids)), "ok": not errors,
        "errors": errors, "warnings": warns,
        "unsourced_numbers": unsourced[:30], "lead_citations": lead[:20],
    }


def render(res: dict) -> str:
    """把 check() 结果渲染成人/LLM 可读文本。"""
    out = [f"=== check_draft Round {res['round']} ===",
           f"章节 {res['sections']} ｜ 引用编号 {res['citations']} ｜ "
           f"ERROR {len(res['errors'])} ｜ WARN {len(res['warnings'])}"]
    if res["errors"]:
        out.append("\n## ERROR（必须清零才能提交审核）")
        out += ["  ✗ " + e for e in res["errors"]]
        for u in (res.get("lead_citations") or [])[:8]:
            out.append(f"    - 第{u['section']}章 [{u['cite']}] … {u['context']}")
    if res["warnings"]:
        out.append("\n## WARN（逐条确认：补证据 或 降级措辞）")
        out += ["  ! " + w for w in res["warnings"]]
        for u in res["unsourced_numbers"][:15]:
            out.append(f"    - 第{u['section']}章 {u['number']} … {u['context']}")
        if len(res["unsourced_numbers"]) > 15:
            out.append(f"    … 其余见 --json")
    out.append("\n" + ("✅ 全绿" if res["ok"] else "❌ 有 ERROR，先修初稿"))
    return "\n".join(out)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    ap = argparse.ArgumentParser(description="litreview V2.3 初稿硬断言")
    ap.add_argument("--round", type=int, default=None)
    ap.add_argument("--file", default=None, help="指定被检查文件（缺省 thinking/round_NN/draft_rN.md）")
    ap.add_argument("--min-chars", type=int, default=300)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = check(args.round, draft_file=args.file, min_chars=args.min_chars)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render(res))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
