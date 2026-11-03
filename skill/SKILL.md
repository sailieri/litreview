---
name: litreview
description: 文献综述系统 — 建库管线 + 超图增强 RAG 检索 + 轮次制综述撰写（两种运行模式：内置主智能体 / 自带智能体 BYO；独立追问者 + 多位审稿人 + 硬断言）
tags: [literature-review, hypergraph, rag, multi-agent, review, human-in-the-loop]
---

# litreview: 文献综述系统

两阶段架构：**先建知识库，再基于知识库写综述**。撰写阶段是**轮次制**的：每一轮 = 查库调研 → 写初稿 → 独立追问 → 硬断言 → 写定稿 → 并行审核；**跑完一次审核就是一轮结束**（`round += 1`），所有中间产物带轮次号、跨轮复用、禁止跨轮覆盖。

**同一个 skill 支持两种运行方式**：内置主智能体（`pipelines/review/main_agent.py`）或**自带智能体**（你加载本 skill，自己当主智能体）——步骤、脚本、文件契约完全相同，差别只在【谁来做需要思考的那几步】。先读下一节选定模式。

---

## 运行模式：两种用法（动手前先选）

本 skill 描述的是**一套步骤 + 一套脚本 + 一套文件契约**，与「谁来做主智能体」解耦。两种用法：

| | **模式 A：内置主智能体** | **模式 B：自带智能体（BYO）** |
|---|---|---|
| 谁是主智能体 | `pipelines/review/main_agent.py` 内置的 agent 循环（模型由 `config.yaml` 的 `steps.agent` 指定） | **你**——加载本 skill 的任意 agent（pi / Claude Code / Codex / Cursor / 自建） |
| 怎么启动 | `python pipelines/review/main_agent.py --round N` | 无启动命令：按下面「模式 B 步骤手册」逐步执行 |
| 谁出 token | 主智能体（写稿）+ 脚本侧 LLM（检索问答 / 追问者 / 审稿人）全在 `config.yaml` | 只有**脚本侧**走 `config.yaml`；写稿、改稿、判断用**你自己**的模型与额度 |
| 适用 | 无人值守跑批、弱模型托管、定时补轮 | 想用自己的 agent / 自己的模型写稿；想在 IDE 里边看边改；想边跑边人工插话 |
| 产物 | 同一套 `data/reviews/` 路径、同一套状态机与预算文件 | 同左（**与模式 A 逐字节同构**，两种模式可交替接手） |

### 工具面：脚本 / MCP 两个入口

流程需要的工具都在 `pipelines/review/`。为了方便用自己的智能体跑（Claude Code / Codex / Cursor / 自建），仓库把**全部工具包装成了一份 MCP 服务**（`webapp/mcp_server.py`，streamable-http，默认 `http://127.0.0.1:8791/mcp`）。挂上它，BYO 智能体不必自己实现任何 tool。

| 分组 | MCP 工具 | 对应脚本 |
|---|---|---|
| 进度与初稿 | `review_status` `read_draft` `write_draft` `patch_draft` `save_key_findings` | 直接读写 `data/reviews/` 文件 |
| 引用登记 | `list_refs` `expand_ref` | `modules/review/refs.py` |
| 查库调研 | `kb_query` | `pipelines/review/kb_qa.py` |
| 追问 | `run_probe` | `pipelines/review/probe_draft.py` |
| 硬断言 | `run_check` `run_gap_audit` | `check_draft.py` / `modules/review/gap_audit.py` |
| 成稿与审核 | `compile_draft` `run_review` | `compile_draft.py` / `review_draft.py` |
| 文献库（数据库板块） | `search_papers` `read_paper` `read_section` `search_hyperedges` `hyper_rag` `get_stats` `list_papers` `get_paper_details` `get_import_status` `ask_librarian` `get_litread_web_url` | `modules/kb_agent.py` / `modules/database/` |

> `run_review` 跑完即本轮结束（`round += 1`）；`kb_query` / `run_review` 可能跑数分钟，客户端超时建议 ≥ 900 秒。

- **BYO 智能体**：挂 MCP 取检索能力 → 按流程调 `pipelines/review/*.py` 写稿、追问、送审 → 人工意见直接对话提出
- **托管模式**：`python pipelines/review/main_agent.py --round N` 一次跑完 → 人工意见写进 `data/reviews/human/human_review_filled.md`

**硬约束（两种模式共同）**：内容层唯一可改的文件是初稿 `thinking/round_NN/draft_rN.md`；成稿、审核意见、证据包一律由脚本产出——引用编号 `[S#]` 的全局登记在脚本里，手写会破坏审计链。

### 模式 B 的四条铁律

1. **先读 `pipelines/review/main_agent_prompt.md`，把它当你的作业纪律。** 模式 A 下它是 `pipelines/review/main_agent.py` 注入的 system prompt；模式 B 下它就是**你**的规矩（问题面怎么铺、四维覆盖、缺口怎么写、禁止流程自指、数值必须可溯源）。
2. **开场上下文用脚本取，别自己拼**：`python pipelines/review/main_agent.py --print-kickoff --round N` —— 输出与模式 A 的首条 user 消息等价（当前轮次 + PRD + 上轮产物 + 已问过的问题清单）。
3. **简单任务可以自己顺手做，但“记账”必须走脚本** —— kb 检索、追问、审稿本身都是“简单几步”的活（弱模型就够），你可以自己扮演；但 **`[S#]` 登记 / 证据包 / 轮次推进 / 预算计数只有脚本会做**——绕开它们，稿子会慢慢与审计链脱钩（数值无法溯源、轮次错乱）。见下「角色分工」。
4. **轮次推进只由 `pipelines/review/review_draft.py` 触发**（跑一次审核 = 本轮结束，`round += 1`）。别手改 `_round_state.json`，别覆盖往轮目录。

### 角色分工：谁是“重”的，谁是“简单几步”

模式 B 替换的**只是“写综述”这个身份**（内容层：铺问题面、论证、消化意见、叙述化）——它是全流程里唯一“重”的活。剩下四个角色本质上都是**简单几步的弱模型任务**，所以脚本只是它们的**默认实现**，不是硬依赖：

| 角色 | 任务性质 | 模式 A 的实现 | 模式 B 的两种实现 |
|---|---|---|---|
| **撰写主智能体**（内容层） | **重**：四维问题面、数值论证、消化意见、叙述化 | `pipelines/review/main_agent.py` 内置 LLM + SOP | **必须换成你自己的 agent**（这就是迁移要换的那一个身份） |
| kb agent（检索问答） | 简单几步：问题→检索→读回→抽数值 | `modules/kb_agent.py`（便宜模型即可） | ① 调 `pipelines/review/kb_qa.py`（默认，兼任 `[S#]` 登记）② 自己查库抽数（但编号登记仍需走脚本一次） |
| 追问者（内部评审读者） | 简单几步：读稿→列对象与分支→查覆盖→提问 | `pipelines/review/probe_draft.py` | ① 调脚本 ② 自己按 `modules/review/probe.py` 的 `PROBE_SYS` 扮演 |
| 审稿人（多维 persona） | 简单几步：单维度清单式检查 | `pipelines/review/review_draft.py`（并发扇出） | ① 调脚本（默认，独立性更强）② 自己按 `review_team.py` 的 persona 逐条审（省配置，但失去独立第三方性质） |
| 硬断言 | 纯机械，无 LLM | `pipelines/review/check_draft.py` | **只能走脚本**（无 LLM，没有替代意义） |

> 这层分工的意义就是「弱模型做简单任务」：**只有撰写值得用强模型**，其余四个角色用便宜模型、甚至你的 agent 顺手做掉都可以。脚本的价值在三件事：**审计链**（`[S#]` 登记 / 证据包 / 轮次预算）+ **并发扇出**（一次跑多个 persona）+ **角色隔离**（审稿人与作者不是同一个脑子）——不在于“这些活有多难”。

### 模式 A：一行开跑

```bash
python pipelines/review/main_agent.py --round 4                        # 跑一轮（调研 → 初稿 → 追问 → 论断 → 定稿 → 审核），round 自动 +1
python pipelines/review/main_agent.py --dry-run --round 4              # 不调 LLM：只校验提示词 / PRD / 工具表 / 轮次状态
python pipelines/review/main_agent.py --stop-before-review --round 4    # 停在审核之前（你先读稿，再决定要不要送审）
python pipelines/review/main_agent.py --print-kickoff --round 4         # 只打印开场上下文（模式 B 用）
```

开跑前会跑 `preflight()` 探活知识库端点（不通直接终止、不烧钱）；执行轨迹落 `data/reviews/thinking/round_NN/_trace_rN.md`，日志落 `data/reviews/logs/`。

### 模式 B：步骤手册（与模式 A 逐步对照）

| # | 步骤 | 模式 A 的工具调用 | **模式 B 你要做的** | 产出（相对 `data/reviews/`） | 前置 / 自检 |
|---|---|---|---|---|---|
| 0 | 规划 | 无（PRD 由人写，kickoff 注入） | 按顶部「执行纪律」建 Todo、向用户澄清、写 `meta/_prd.md` + `meta/_spec.md` | `meta/_prd.md` / `meta/_spec.md` | 没有 PRD 不要往下走 |
| 1 | 开场 | `build_kickoff()` 自动注入 | `python pipelines/review/main_agent.py --print-kickoff --round N`，读入 | — | 同时把 `pipelines/review/main_agent_prompt.md` 读成你的纪律 |
| 2 | 查库 | `kb_query(questions)` | `python pipelines/review/kb_qa.py --q "<英文关键词>" --round N`（一问一条命令，可并行/后台多条） | `research/_kb_rN_K_slug.md` + `research/_kb_refs.json` | 每轮 ≤24 问；数值只能来自这里 |
| 3 | 事实锚点 | `save_key_findings(content)` | 写文件 `thinking/round_NN/key_findings.md` | 同左 | 每条 = 结论 + 数值/方法 + `[S#]` + 对论证的作用 |
| 4 | 初稿 | `write_draft(markdown)` | 写文件 `thinking/round_NN/draft_rN.md` | 同左 | 章节用 `## N. 标题`；含 `## 摘要` 与 `## 已知局限` 两个不编号块；不写参考文献 |
| 5 | 局部修订 | `patch_draft(old, new)` | 直接编辑该文件（精确替换，别整篇重写） | 同左 | 消化追问/审核意见都在这一层做 |
| 6 | 追问 | `run_probe(response)` | `python pipelines/review/probe_draft.py --round N [--response "本轮改了什么"]` | `thinking/round_NN/probe_rN.md`（`_2` 后缀 = 深挖轮） | **每轮 ≥1 次、≤3 次**；跑完必须消化；带 `--response` 进深挖轮 |
| 7 | 硬断言 | `run_check()` | `python pipelines/review/check_draft.py --round N`（`--json` 得机器可读结果） | stdout | **ERROR 必须清零**才能进下一步 |
| 8 | 核证据 | `expand_ref(sids)` | `python pipelines/review/kb_qa.py --expand S10 S17` | stdout | 核对某个数值到底有没有出处 |
| 9 | 文献表 | `list_refs(cited_only)` | `python -c "import re;from modules.review import refs;r=refs.load();t=open('data/reviews/thinking/round_04/draft_r4.md',encoding='utf-8').read();ids=list(dict.fromkeys(re.findall(r'\[([SE]\d+)\]',t)));print(chr(10).join(refs.biblio_lines(r,ids)))"`（换成本轮初稿的路径） | `research/_kb_refs.json` | 题录机读自 `_kb_refs.json`，**不要自编** |
| 10 | 定稿 | `write_final(markdown)` | 写文件 `writing/round_NN/review_draft_rN.md`（叙述化 + 参考文献表，**不得新增事实**） | 同左 | 备选路径：`python pipelines/review/compile_draft.py --round N`（LLM 表达层转写，会先跑硬断言） |
| 11 | 审核 | `run_review()` | `python pipelines/review/review_draft.py --round N` | `review/review_issues_rN.json`（同时归档 `final/final_review_rN.md`、刷新 `final/final_review.md`） | 有 ERROR 会被拒绝；≤2 次；跑完 `round += 1` |
| 12 | 收口 | `finish(summary)` | 写本轮小结（初稿/定稿路径、断言结果、审核结论、下一步）到 `thinking/round_NN/_trace_rN.md` | 同左 | 初稿写了却没跑追问 = 本轮不合格 |

> 模式 B 没有「工具调用轮次上限」的自动拦截（那是模式 A 的 30 轮循环限制）。预算仍由脚本记账（超限直接拒绝），但**"再查一批 / 再深挖一层"的次数要你自己设上限**——建议在 Todo 里写成显式清单，别无限打磨。

### 脚本契约（工具面）

| 脚本 | 干什么 | 读 | 写 | 每轮上限 |
|---|---|---|---|---|
| `pipelines/review/main_agent.py` | 模式 A 主循环；`--print-kickoff` 供模式 B 取开场上下文 | 提示词 / PRD / 状态 | `thinking/round_NN/_trace_rN.md` | — |
| `pipelines/review/kb_qa.py` | 查库问答（唯一信息通道）+ 引用登记 + 按需展开原文 | `papers.db` | `research/_kb_rN_K_*.md`、`research/_kb_refs.json` | 24 问 |
| `pipelines/review/probe_draft.py` | 独立追问者（内部评审读者：对象—覆盖地图 + 追问清单） | 初稿 / key_findings / PRD | `thinking/round_NN/probe_rN.md` | 3 |
| `pipelines/review/check_draft.py` | 硬断言：章节完整性 / `[S#]` 可解析 / 数值可溯源 / 流程自指 / 长句长段 / 摘要重复度 | 初稿（或 `--file` 指定） | stdout（`--json`） | — |
| `pipelines/review/compile_draft.py` | 初稿 → 成稿（表达层转写）+ 证据包；先跑硬断言 | 初稿 | `writing/round_NN/review_draft_rN.md`、`thinking/round_NN/evidence_pack_rN.md` | 2 |
| `pipelines/review/review_draft.py` | 并行审核（多位审稿人 + F_human 槽位）→ 归档 → `round += 1` | 定稿 | `review/review_issues_rN.json`、`final/final_review_rN.md` | 2 |

### 文件契约（唯一真相源：`modules/review/paths.py` + `modules/review/rounds.py`）

```
data/reviews/
├── research/           调研：_kb_r{N}_{K}_{slug}.md（kb_qa 产出） + _kb_refs.json（[S#] 全局登记表）
├── thinking/round_NN/  本地轮：draft_rN.md（初稿·唯一内容源）/ key_findings.md / probe_rN.md
│                       / evidence_pack_rN.md / _trace_rN.md
├── writing/round_NN/   review_draft_rN.md（定稿）
├── review/             review_issues_rN.json（审核意见，每条带 location 章节锚点）
├── human/              human_review_filled.md（人类意见入口，审核时并入 F_human 槽位）
├── reviewers/          用户/自动生成的专属审稿人 *.yaml（运行时合并，不写进代码）
├── final/              final_review.md（当前）+ final_review_rN.md（每轮保留）
├── meta/               _prd.md / _spec.md / _round_state.json（轮次唯一真相源）/ _budget.json（脚本预算）
└── logs/               运行日志
```

状态机三条：

- `_round_state.json` 的 `round` 是轮次唯一真相源；**每跑完一次审核 = 本轮结束，`round += 1`**。
- 产物路径全部带轮次号，**禁止跨轮覆盖**；状态文件缺失时按已有 `thinking/round_NN/` 目录推断当前轮（不默认 1）。
- 脚本预算按轮次隔离（`_budget.json`），超限抛 `BudgetExceeded`——需用户明确授权才能 `--reset-budget`。

### 人人在环（两模式同构）

人类只在 `data/reviews/human/human_review_filled.md` 里写自然语言意见 → 审核脚本把它并入 **F_human** 槽位 → 下一轮的开场上下文会**全文注入上一轮的 `review/review_issues_rN.json`**，智能体逐条消化（这就是"人类审稿意见会不会被看到"那条链）。「必须 / 不能 / 绝不允许」这类**强制约束**若想固定成常驻关卡，手动跑一次 `python pipelines/review/human_router.py`：它把约束转成审稿人 YAML 落到 `reviewers/`，之后每轮自动进审稿团队；疑问/建议类则走调研回答后再决定是否改稿。

**两种用法下人工审核的时机**：用你自己的智能体时，直接跟它对话即可；用内置主智能体（托管）时，把意见写进 `human_review_filled.md`，下次送审自动并入 F_human 槽位。

### 💰 成本透明（跑前必读）

**本管线是重上下文任务**（主智能体累积历史 + kb_qa 全库题录注入 + 全文规范化重写），一次全流程是真实开销。`pipelines/review/main_agent.py` 启动时会自动打印两行预估；精确明细用计算器：

```bash
python tools/cost_calculator.py --papers 50        # 全程 DeepSeek·空闲档：≈¥11（建库≈¥0.14/篇 + 综述≈¥4）
python tools/cost_calculator.py --papers 50 --peak # 高峰档（工作日 9-12/14-18 点）×2
python tools/cost_calculator.py --papers 50 --model pro   # v4-pro：≈¥44；pro+高峰 ≈¥87
python tools/cost_calculator.py --papers 50 --profile current  # 当前 config 混合口径：≈¥1.1
```

关键结论：① 输出 token 是大头（缓存命中价 ¥0.02/M 几乎白送，勿把省钱努力错放在压输入）；② 综述成本与篇数基本无关（kb 检索有截断），篇数只影响建库；③ 把建库/kb_query/审稿/probe/gap_audit 指向本地自建推理端点时，这些步骤开销≈ 0，实付只剩写稿与定稿转写。token 常数与不确定度（±30%）见 `tools/cost_calculator.py` 文件头。

---

## 执行纪律：弱模型防跑偏（启动顺序：Todo → 澄清 → PRD/Spec → 执行）

> 本 skill 要在不同能力档位的模型上都能稳定跑通。实测表明：**agent 能力越弱，越容易跳过规划、拿到任务直接写稿或乱调脚本，最终产物不达标还难以回滚。** 因此把"规划→执行"的纪律固化进 skill，强制任何模型在动手前先过两道闸门——**先建 Todo 清单、再问清用户细节**——然后才写 PRD/Spec、再进建库/写稿。能力强弱都该遵守，弱模型尤其不能省：Todo 是对抗漏步/乱序的第一保险，"问清楚"是对抗凭假设瞎干的唯一办法。

### 启动前必走的两道闸门（先于一切建库 / 写稿 / 代码动作）

**第 1 道 · Todo 清单（最高优先，一接到任务就建）**
- 任何规划、任何澄清、任何代码之前，**先把整件事拆成可勾选清单**（用运行环境的待办/任务能力；无该工具则在 `_spec.md` 顶部维护 `[ ] / [x]` 勾选表）。
- 清单至少覆盖：① 澄清询问 ② 写 PRD/Spec ③ 建库（入库 / 超图提取 / 向量）④ 轮次制撰写（kb_qa → 初稿 → probe → check → 定稿 → review）⑤ 终稿自检 ⑥ 续跑/补跑检查（`tools/doctor/doctor.py status`）。
- 这是对抗弱模型"漏步、乱序、凭记忆推进"的第一保险——**永远先有清单再有动作**。

**第 2 道 · 澄清询问（问清楚用户再动，缺一项不许进 PRD）**
- 弱模型最大的坑是"假设"：没问清就开干，路径 / 主题 / 配置全错，返工成本极高。动手前必须向用户逐项确认，至少覆盖下面四类：
  1. **PDF / 源文件位置**：原始文献在哪（目录 / 文件）、用什么方式入库（单篇 `tools/ingest/ingest_paper.py` 还是批量）。
  2. **文本与中间产物保存路径**：综述终稿写到哪（`data/reviews/final/final_review.md`）、规划文件（`data/reviews/meta/_prd.md` / `_spec.md`）、探索池（`data/reviews/research/_rag_*.md` / `_web_*.md`）放哪个目录、是否与现有 `data/reviews/` 结构冲突（见"中间文件依赖链"）。
  3. **综述主题与细节要求**：题目、研究范围与边界、目标篇幅 / 章节结构、行文风格、引用数量与格式要求、目标读者、有无必须覆盖的文献或观点。
  4. **LLM API 配置事项**：provider（OpenAI / vLLM / 其它）、api_key、base_url、模型名、vLLM 端点与并发数、是否需要本地部署、timeout 与重试策略。
- 把答案回写进后续 PRD 的"输入 / 输出 / 配置"字段与 Spec 的"步骤清单"——**没有这些答案，PRD 不得声称"输入 / 配置已明确"**。

### 三件套（澄清完成后才写，Todo 已在第 1 道建好）

> 顺序：第 1 道建好 Todo → 第 2 道问清用户 → 下面写 PRD/Spec → 然后才进建库/写稿。

1. **PRD（`data/reviews/meta/_prd.md`）** —— 需求文档，先回答"做这件事为了什么、边界在哪"：
   - 目标：本次要解决的具体问题 / 要交付的文档
   - 范围与边界：做什么、**明确不做什么**
   - 输入：哪个库 `papers.db`、哪些探索池 / 中间文件已就绪
   - 输出：产物文件名与存放位置
   - 验收标准：可判定的完成条件（如"终稿无英文小标题、引用文献 ≥ N 篇"）
   - 风险与回滚：可能出错的点、如何回到上一状态
2. **Spec（`data/reviews/meta/_spec.md`）** —— 技术规格，把"怎么做"拆到可执行粒度：
   - 步骤清单：每步调哪个模块 / 脚本、用什么参数、读哪个文件、写哪个文件
   - 依赖顺序：哪步必须在哪步之后
   - 调用样例：关键命令 / 函数签名，直接可抄（优先引用本文件 RAG / 建库命令段）
3. **Todo 清单（在第 1 道已建，这里只填充/对齐 Spec 步骤）** —— 把 Spec 步骤变成可勾选清单：
   - 第 1 道已先建了骨架清单；此处把 Spec 拆出的细步骤挂上去，每开始一步先标 in_progress，完成才标 completed，**未完成不得跳到下一步**
   - 弱模型务必依赖清单而非"凭记忆"推进——它是对抗漏步 / 乱序的唯一保险
   - 若运行环境无 todo 工具，则在 `_spec.md` 顶部维护 `[ ] / [x]` 的 Markdown 勾选表作为等价物

### 强制门禁

- **两道闸门前置**：未通过「Todo 清单 + 澄清询问」两道闸门，**禁止写 PRD 以外的任何动作**；未产出 PRD + Spec，禁止进入写稿、审稿、建库的核心动作。先清单、再问清、后规划、再执行。
- 每轮（Phase 2 一个大轮）结尾，把"本轮实际完成 / 跳过步骤 / 遗留问题"回写 PRD/Spec，作为下一轮输入。
- 人类在 `data/reviews/human/human_review_filled.md` 的意见，下一轮必须进 PRD 的"风险与回滚"或 Spec 的"步骤清单"，不得口头消化。

### 为什么弱模型特别需要这套

弱模型常犯的错：① **不问清就假设**（路径 / 主题 / 配置凭猜，全盘返工）；② 跳过探索池直接靠记忆写稿（数字失真）；③ 不调 `retriever` 直接读 summaries（精度不足）；④ 用 `import_all` 清库（灾难性）；⑤ 接受第三方审稿意见照单全收不二次评估。Todo + 澄清询问 + PRD/Spec 三件套把"先建清单、先问清楚、必须走哪几步、走之前确认什么"显式写出，让弱模型有迹可循、有单可查。

### 附录 A：Todo 清单模板（弱模型可直接照抄）

把下面这份勾选表复制到 `_spec.md` 顶部（无 todo 工具时作为等价勾选表），或导入运行环境的待办能力，**逐项打勾推进，未完成不跳下一步**：

```
[ ] 0. 启动闸门（最高优先，一接任务就做）
  [ ] 0.1 建立本 Todo 清单（第一动作，先于一切）
  [ ] 0.2 向用户澄清 4 类细节 → 写入 _prd.md
        - PDF/源文件位置   - 文本与中间产物保存路径
        - 综述主题与细节要求   - LLM API 配置（provider/key/base_url/模型/并发/timeout）
[ ] 1. 规划文件
  [ ] 1.1 写 _prd.md（目标 / 范围边界 / 输入 / 输出 / 验收标准 / 风险与回滚）
  [ ] 1.2 写 _spec.md（步骤清单 / 依赖顺序 / 可抄命令 + 本勾选表）
[ ] 2. 建库·预处理（Phase 1）
  [ ] 2.1 建 data/ 各中间文件夹 + 初始化 papers.db（schema.init_db）
  [ ] 2.2 提取 PDF → data/extracted/*_raw.md（batch_extract_pdfs.py）
  [ ] 2.3 规范化：metadata + normalized text → data/metadata, data/normalized（batch_normalize.py / 单篇）
  [ ] 2.4 入库 chunks + 向量：批量拆 sections+chunks 用 batch_index.py，单篇可用 tools/ingest/ingest_paper.py；向量由 2.9 统一算
  [ ] 2.5 超图提取 entities/hyperedges：批量入口 batch_extract_all.py（并行 + A-C 2 轮），单篇用 tools/extract/extract_one.py
  [ ] 2.6 A-C 增强（refine，每成功一轮 papers.ac_rounds +1）；**权限：实体 full CRUD，超边仅 ADD+UPDATE、禁 DELETE**（见关键陷阱）
  [ ] 2.7 安全增量入库超图（tools/graph/import_hypergraph.py --paper-id，禁用 import_all）
  [ ] 2.8 跨文献超图（合并实体两步；跨文献超边由问答增量产生，不在此阶段）
        [ ] 2.8.1 实体合并：`tools/graph/cluster_pipeline.py --stage 1`（DBSCAN 分簇）+ `tools/graph/merge_entities.py`（LLM 合并）→ entities_merged（**含 type='paper' 3 条单例**，否则"论文:标题"参与者变孤儿）
        [ ] 2.8.2 实体 ID 双域：两张超边表都存两列——`participants`（私有 entities.id，稳定锚点）+ `participants_merged`（公有 entities_merged.id，派生）。合并跑完由 `merge.py` 自动重算公有列；手工核对用 `doctor.py ids`，重算用 `tools/maintain/sync_id_domains.py --apply`
  [ ] 2.9 向量 embedding + 索引（`pipelines/build/embeds.py`，必须覆盖 chunks + entities_merged + cross_doc_hyperedges）；**须在 2.8 之后跑**，合并实体向量此时才生成
[ ] 3. 续跑 / 补跑检查
  [ ] 3.1 跑 tools/doctor/doctor.py status，补齐 NEED_EXTRACT / NEED_AC
[ ] 4. 撰写（Phase 2·轮次制 V2.3）
  [ ] 4.1 读作业纪律：pipelines/review/main_agent_prompt.md（模式 B 必读；模式 A 由脚本注入）
  [ ] 4.2 取开场上下文：python pipelines/review/main_agent.py --print-kickoff --round N（或模式 A 直接 pipelines/review/main_agent.py --round N）
  [ ] 4.3 调研：pipelines/review/kb_qa.py 并行查库（数值/引用只能来自这里）
  [ ] 4.4 初稿 thinking/round_NN/draft_rN.md → 独立追问 pipelines/review/probe_draft.py → 逐个消化
  [ ] 4.5 硬断言 pipelines/review/check_draft.py：ERROR 清零
  [ ] 4.6 定稿 writing/round_NN/review_draft_rN.md（或 pipelines/review/compile_draft.py）
  [ ] 4.7 送审 pipelines/review/review_draft.py（跑完 = 本轮结束，round += 1）；人类意见写 human/human_review_filled.md
[ ] 5. 收尾
  [ ] 5.1 质量检查：pipelines/review/check_draft.py（机械断言）+ 人工读稿；tools/doctor/doctor.py review（标题/收束/指令腔/公式/结构）
  [ ] 5.2 回写 _prd/_spec：实际完成 / 跳过步骤 / 遗留问题
```

> 弱模型务必依赖这份清单而非"凭记忆"推进：每步开始标 in_progress、完成才标 completed，**跳步即意味着流程跑偏**。

---

## 项目结构

```
litreview/
├── SKILL.md                     # 本文件（两种运行模式的唯一入口）
├── config.yaml                  # providers + steps 配置（脚本侧 LLM；模板见 config.yaml.example）
├── pipelines/review/main_agent_prompt.md  # ★ 主智能体作业纪律（模式 A 注入 / 模式 B 你自己读）
├── pipelines/review/main_agent.py                # ★ 模式 A 入口：内置主智能体循环（--round / --dry-run / --print-kickoff）
├── pipelines/review/kb_qa.py                     # 脚本① 知识库问答（唯一信息通道 + [S#] 登记）
├── pipelines/review/probe_draft.py               # 脚本② 独立追问者（内部评审读者）
├── pipelines/review/check_draft.py               # 脚本③ 硬断言
├── pipelines/review/compile_draft.py             # 脚本④ 初稿→成稿（表达层转写；弱模型备选路径）
├── pipelines/review/review_draft.py              # 脚本⑤ 并行审核 + 轮次推进
├── modules/
│   ├── database/                # 第一阶段·建库与检索
│   │   ├── schema.py / indexer.py / embedder.py / retriever.py
│   │   └── hyper_write.py       # 超图写入的唯一实现
│   ├── hypergraph/  refine.py / summary.py                  # 第一阶段·超图提取
│   ├── normalize/   extractor.py / normalizer.py            # 第一阶段·规范化
│   ├── review/                    # 第二阶段·综述撰写与审稿（路径/状态的唯一真相源）
│   │   ├── paths.py / rounds.py   # ★ 路径常量 / 轮次状态 + 脚本预算（模式 A、B 共用）
│   │   ├── refs.py                # ★ 引用登记：[S#] 全局唯一编号 / 题录 / 证据包
│   │   ├── probe.py               # 追问者实现（内部评审读者）
│   │   ├── writing/    compiler.py                     # 成稿整合（表达层转写）
│   │   ├── critique/   review_team.py                  # 并行审稿人 + F_human + H_curation
│   ├── util/       llm_client.py / jsonllm.py / dedup_gate.py   # 通用件（LLM 调用 / JSON 容错 / 查重闸门）
│   │              batch/  只含函数：batch_extract_pdfs / batch_normalize / batch_extract_all /
│   │                     batch_index / batch_embeds（命令行入口在 pipelines/build/）
├── tools/                       # 辅助工具（通用、参数化，非主流程）
│   ├── ingest/    ingest_paper.py / ingest_text_batch.py / batch_ingest.py / ocr_pdf.py
│   ├── extract/   extract_one.py（单篇） / batch_summary.py
│   ├── graph/     import_hypergraph.py / cluster_pipeline.py / merge_entities.py
│   ├── embed/     embed_missing.py
│   ├── doctor/    doctor.py（health/graph/smoke/status/review/audit）/ audit_normalize.py
│   ├── maintain/  orphans.py（inspect/match/dups/ingest/list/dedupe）/ backfill_metadata.py /
│   │              fetch_missing_pdfs.py
│   └── cost_calculator.py       # 跑前成本估算
└── data/
    ├── database/papers.db       知识库（chunks / entities / hyperedges + 向量）
    ├── reviews/                 ★ 按"环节/职能"分组的规划文件 + 中间文件 + 终稿（布局见 modules/review/paths.py）
    │   ├── research/   _rag_*(RAG) / _web_*(网络) / user_*(用户给) 调研纪要
    │   ├── thinking/   thinking_brief.md / thinking_chain.md / thinking_selected.json
    │   ├── writing/    round_NN/{review_draft.md, changelog.md}（每轮草稿 + 修改历史）
    │   ├── review/     review_team_rN.json / unapplied_rN.md / curation_report.md / main_agent_pr.json
    │   ├── human/      human_review_filled.md / human_writing_input.md / human_review_checklist.md / 归档
    │   ├── final/      final_review.md(当前) + final_review_rN.md(每轮保留) + final_review_prev.md(基线)
    │   ├── reviewers/  用户/自动审稿人 *.yaml（运行时合并）
    │   ├── meta/       _prd.md / _spec.md / _round_state.json / _human_router_spec.md / _style_refs.md
    │   └── logs/       _run_roundN.log
    └── pdfs/ extracted/ normalized/ summaries/ entities/  # 建库中间产物

_archive/                          # 本地归档（不入仓库）：legacy_v1_v21/（V1/V2.1 撰写管线）、
                                   # 一次性脚本、题录清单、旧快照里主库缺的 PDF
```

## 数据库

**格式：单个 SQLite 文件**（`data/database/papers.db`）。

- 全文检索用 SQLite 内置 **FTS5**（外部内容表 + 触发器同步，见 `schema.py`）。
- 语义向量（embedding）以 **BLOB** 形式直接存在 `chunks` / `entities` / `hyperedges` 等表的 `vector` 列里——混合检索（BM25 + 向量 z-score 融合）在 `retriever.py` 内完成，无需额外向量库。
- 换库零成本：其他用户只要把 `data/database/papers.db` 换成自己的库（表结构一致即可），或删掉后由建库流程重建空库，skill 逻辑完全不变。

位置：`data/database/papers.db`

| 表 | 说明 |
|:---|:-----|
| papers | 论文元数据（title/authors/year/journal/doi/file_path）+ 提取进度（entity_count/hyperedge_count/ac_rounds，见 Phase 1「进度追踪」） |
| sections | 树结构章节（Overview+L2+L3+L4，parent_id 连接） |
| chunks | 文本分块 + embedding 向量（BLOB） |
| entities | 单篇超图实体 + 向量（BLOB） |
| hyperedges | 单篇超边：`participants`=私有 entities.id（锚点）+ `participants_merged`=公有 entities_merged.id（派生，检索用）+ 向量 |
| entities_merged | 合并后实体 + 向量（BLOB） |
| cross_doc_hyperedges | 跨文献超边（问答归档时增量产生）+ 同样两列双域 + `qa_msg_id` 溯源 + 向量 |

FTS5 表使用 `tokenize='unicode61'` 分词器，英文词汇完整匹配。连字符术语（如 `NMC-0.5`）由 `_build_fts_query()` 自动包装为短语查询处理。chunks 表有独立的 `chunks_fts` 内联内容索引。

## RAG 检索接口（核心！每次查数据必须调这个）

**综述撰写过程中，每次需要论文的具体数据时，必须调用 retriever.py 的函数，而非直接读 summaries/*.json。** 摘要文件只有概要，不包含精确数值。

> **⚠️ 查询语言约束（重要）：检索查询（各 `search_*` 的 `query` 参数）必须使用「英文关键词」。** 当前 FTS5 使用 `unicode61` 分词器、不切分中文，中文查询的 BM25 词法分数为 0、只能靠向量语义匹配，召回与排序会劣化。中文叙述请先改写成英文术语/短语（如 "传输损耗"→`power loss`、"路由算法"→`routing algorithm`）再查。根治方案是把 FTS 切到 `trigram` 分词器并重建索引（见关键陷阱，规模化中文语料前建议实施）。

```python
from modules.database.retriever import *

# ── 混合检索（推荐：FTS5 BM25 + embedding） ──

# 统一搜索：chunks + hyperedges（edges 默认 top_k=10）
result = search("LSTM wind power forecasting", top_k_chunks=10, top_k_edges=10)
# → {"chunks": [...], "hyperedges": [...]}

# 只搜 chunks
chunks = search_chunks("VMD decomposition wind power", top_k=10)
# → [{chunk_id, paper_id, paper_title, section_title, content, score, vec_score, bm25_score}, ...]

# 只搜超边（默认 top_k=10，便于跨文献超边在 99 条单篇超边中不被挤掉）
edges = search_hyperedges("prediction accuracy RMSE MAE", top_k=10)
# → [{id, scope, participants, participant_names, description, keywords, strength, source_papers, score, vec_score, bm25_score}]

# ── 基础检索 ──

# 获取论文列表
papers = list_papers()
# → [{id, title, authors, year, journal}, ...]

# 获取论文详细信息（含 sections 列表 + summary + entities）
detail = get_paper_details(paper_id=5)

# FTS5 全文搜索（快速定位）
results = search_sections("GCN LSTM spatial temporal", limit=5)
# → [{paper_id, paper_title, section_title, snippet(高亮), ...}]

# ── 精读段落 ──

sections = read_section(paper_id=12)             # 全部内容
section = read_section(paper_id=12, section_id=32)  # 指定段落

# ── 树导航 ──

tree = get_section_tree(paper_id=5)
children = get_section_children(pid=5, sid=22)
path = get_section_path(pid=5, sid=28)
```

**连字符处理：** `search_chunks("GWO-DAGRU feature")` 自动将带连字符的术语包装为短语查询；同时会**滤掉停用词与 <3 字符的短词**（不过滤时一个 34 词长查询会命中 85.8% 的 chunk，BM25 形同噪声）。

**默认返回数量：** `search_chunks` / `search` 的 chunks 默认 `top_k=10`；`search_hyperedges` / `search` 的 edges 默认 `top_k=10`。可按查询需要显式调大/调小。

**针对“库里有却检索不到”：**

```python
# 1) 多子查询合并检索：复合问题拆成原子子查询，各算名次后 RRF 合并（比单条长查询准得多）
rows = search_chunks_multi(["crop phenology time series monitoring",
                            "interannual yield variability of crop residue"], top_k=12)

# 2) 论文级去重：同一篇论文最多留 2 条 chunk（避免一篇的 40 个 chunk 霸屏）
rows = search_chunks("straw return SOC", top_k=12, max_per_paper=2)

# 3) 缺口核实：“库里到底有没有关于 X 的文献”——论文级 max-pool 召回 + FTS 命中统计
g = gap_check("minimum crop residue retention rate soil carbon threshold", top_papers=25)
# → {topic, papers_total, fts_chunk_hits, papers:[{paper_id,title,year,journal,doi,
#      vector_rank,bm25_rank,best_section,snippet}]}
```

**融合方式变更：** 由 z-score 改为 **RRF（名次融合，`RRF_K=60`）**。实测 z-score 会把向量排第 4 的正确文献冲到第 138；RRF 对“稀疏 BM25 + 稠密向量”鲁棒。`weight` 参数保留但不再使用。
**回归验证：** 6 篇历史漏检论文（[110]/[212]/[218]/[171]/[221]/[96]）用 `gap_check` 逐个主题测，**6/6 进入 top-30（5 篇排第 1）**。

## litread Web 平台（webapp/，http://127.0.0.1:8790）

启动：`cd webapp && ../.venv/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8790`（Linux/macOS 用 `../.venv/bin/python`）；前端 `webapp/frontend`（Vue3+Vite，改完 `npm run build`，dist 由 SPA 路由托管）。只读 DB + 后端惰性导入重模块（kb_agent/paper_qa/librarian）。

| 能力 | 端点 / 实现 |
|---|---|
| 卡片列表 + 多维筛选 | `GET /api/papers`（facets/AND-OR/排序；item 带 `abstract` = summaries narrative 截 500 字） |
| 分析仪表盘 | `GET /api/insights` → `/insights` 页（echarts；图表初始化必须在 loading 结束后） |
| **图书管理员智能体** | `POST /api/agent/chat`（SSE）+ `GET/DELETE /api/agent/threads[/{id}]` |
| 单篇问答（旧，保留未接前端） | `POST /api/papers/{pid}/chat`（paper_qa）、`POST /api/ask`（kb_agent） |
| **中英对照 PDF** | `GET/POST /api/papers/{pid}/bilingual`（状态/触发）、`GET .../bilingual/pdf`（Range 回传） |

**图书管理员（webapp/librarian.py）**——全局侧边栏抽屉 `AgentPanel.vue`（悬浮钮任何页呼出，不跳页）：
- 工具：`search_papers`（300 篇暴力打分，完整串>整词>中文2-gram 比例）/ `read_paper`（复用 paper_qa `_fulltext`，带 question 按 2-gram 打分取相关段落）/ `get_stats`
- 工作区：会话级 paper_ids（store `agentStore`，论文页可一键加入），系统提示常驻清单
- 问答库：`qa_threads`/`qa_messages`/`qa_fts`；新问题 FTS 检索历史相似问答注入 system（常见问题越用命中越高）；SSE 事件 `meta/tool/delta/done/error`，done.content 幂等替换流式累积
- LLM：复用 `config.yaml` 的 `paper_qa` step（qwen-plus），流式 tool_calls 分片同 paper_qa
- **跨文献超边的唯一来源是问答（现行）**：超边不按"单篇/跨文献"分两类生产，建库阶段只产单篇局部超边；用户提问后，会话归档时后台线程让 LLM 从问答回答里抽跨文献超边 → `cross_doc_hyperedges`（`qa_msg_id` 记来源，台账 `qa_edge_jobs` 幂等，0 条也记防重复烧 LLM）。约束 ≥3 参与者且 ≥2 篇来源（单篇深读回答正常入 0 条）；入库后即时 embedding（ollama 不可用则 NULL 等 `batch_embeds` 补，检索只取 vector NOT NULL）。
  早前的两条批量路径（`hypergraph/cross_doc.py` 一次性整库产出、`batch_cross_doc.py` 两阶段按合并实体逐组产出 + `import_cross_doc.py` 入库）已下线：产出与成本不成正比，实测库里 0 条来自管线。

**三个必须注意的点**：① fts5 默认 unicode61 把中文整段切一个 token，中文查询全部漏 → `_seg()` 自分词（中文2-gram+英文整词）存取同构；② 分词索引必须**应用层双写**（SQL 触发器调不了 Python 分词），且改版时要 `DROP TRIGGER IF EXISTS` 旧触发器——否则它先占 rowid、应用层再插报 `IntegrityError: constraint failed`；③ 验证 SSE/中文数据别用 Windows 侧 Python 读 curl 输出（GBK 解码 UTF-8 会把 500 字撑成 765、打印报错），用 `sys.stdin.buffer.read().decode('utf-8')`。

**中英对照（复用 zotero-pdf2zh）**——PdfPane 工具栏「原文｜对照」切换 + 「生成对照」按钮（按需翻译，读到哪篇译哪篇，不预跑全库）：
- **引擎**：外部可选依赖 `pdf2zh_next`（独立 venv）。查找顺序：`config.yaml` 的 `paths.pdf2zh_python` → 仓库内 `.venv-pdf2zh` → `paths.pdf2zh_dirs` 下的 venv；都没配则中英对照按钮不可用。CLI 直调，无需常驻服务；默认免费翻译通道，en→zh-CN，一篇约 6 分钟。上游：https://github.com/Byaidu/PDFMathTranslate
- **后端**（`webapp/server.py` `_run_bilingual`）：POST 起 daemon 线程 → 拷 `data/translated/{pid}.src.pdf`（ASCII 工作副本，避开中文/长文件名）→ exe 产出 `{pid}.src.no_watermark.zh-CN.dual.pdf` → `replace` 成 `{pid}.zh.pdf` 缓存（二次访问零成本，持久化纯中文 mono），中间 mono/工作副本自动清，`{pid}.log` 留排障；pdf2zh 子进程参数走 `_winarg()`（保留作跨平台兼容，Windows 下 no-op）
- **前端**：`bilChecked` 后才渲染控件，2.5s 轮询 running，生成完成自动切到对照版；pid 切换重置状态
- **实测**：烟测 5m41s 出 8 页 dual，第 2 页 1009 中文字符+英文原文并排

## 工作流入口 0：库里没有这篇论文

本系统不承担文献检索与下载。**文献获取由用户自己完成**（自己找、自己下），拿到 PDF 后按下面的 Phase 1 入库即可。

写综述时**不要凭空编写引文**：每条引用都必须能在本库中查到，并经 `kb_qa` 登记为 `[S#]` 编号。查不到的，写"库中未见"，不许编。

## Phase 1: 建库管线

> 同样适用顶部"执行纪律"：动手批量建库前先写 PRD/Spec 并建 Todo 清单，尤其防止误用 `import_all` 清库（见关键陷阱）。

单个论文的处理路径：

```
PDF → pymupdf提取 → 规范化 → 叙事式摘要
    → 超图实体提取（A-C优化，2轮）
    → sections 拆分（indexer）
    → chunks 入库 + embedding 批量计算
    → 实体合并 + 跨文档超边提取
```

### 主要脚本

| 脚本 | 功能 |
|------|------|
| `pipelines/build/extract_hypergraph.py` → `modules/util/batch/batch_extract_all.py` | 全量超图提取（实体+超边，并行A-C优化） |
| `pipelines/build/index.py` → `modules/util/batch/batch_index.py` | normalized → sections + chunks |
| `pipelines/build/embeds.py` → `modules/util/batch/batch_embeds.py` | 批量计算 embeddings |
| `tools/ingest/ingest_paper.py` | 单篇入库：PDF → chunks + 向量（`--stem`/`--meta` 可选） |
| `tools/extract/extract_one.py` | 单篇超图提取（STEM 或 md 路径） |
| `tools/graph/import_hypergraph.py` | 单篇安全增量超图入库（`--paper-id`） |
| `tools/maintain/sync_id_domains.py` | 重算超边公有 id 列（默认 dry-run；合并后补跑 / 排查悬空） |

### 建库命令参考

```bash
# 超图提取（A-C 2轮优化，并行）
cd /path/to/litreview
python3 pipelines/build/extract_hypergraph.py

# 拆分论文章节 + 分块
python3 pipelines/build/index.py

# 批量计算 embedding（chunks + entities_merged + cross_doc_hyperedges）
python3 pipelines/build/embeds.py

# ── 实体合并 ──

# 1) 分簇：DBSCAN
python3 tools/graph/cluster_pipeline.py --stage 1

# 2) LLM 合并，产出 entities_merged
python3 tools/graph/merge_entities.py

# 3) 重算向量（entities_merged 更新后补）
python3 pipelines/build/embeds.py
```

### 超图提取进度追踪（断点续跑 + 检查日志）

超图提取最易出错（vLLM 并行不稳定），但各 LLM 调用之间不强依赖、可续跑。为支持断点续跑，在 `papers` 表加了三列作为"检查管理日志"：

| 列 | 含义 | 初始 |
|----|------|------|
| `entity_count` | 初始提取的实体数 | 0 |
| `hyperedge_count` | 初始提取的超边数 | 0 |
| `ac_rounds` | A-C 增强已完成轮数（每成功一轮 +1） | 0 |

状态语义：初始提取成功 → 实体/超边数变非 0、`ac_rounds` 仍 0；每成功一轮 A-C → `ac_rounds +1`。`process_one_paper`（被 `batch_extract_all.py` 与 `tools/extract/extract_one.py` 共用）**按这三列决定步骤**，不再靠"json 是否存在"来跳过——这样 A-C 中途崩溃不会被误判为已完成，下一轮可只补 A-C、不必重提。

- 检查哪些论文需要补跑：`python tools/doctor/doctor.py status`（输出 OK / NEED_EXTRACT / NEED_AC 清单；`--target-ac` 改目标轮数）。
- 存量旧库首次使用：先 `python tools/doctor/doctor.py status --backfill --mark-ac-done` 从 `data/entities/*.json` 回填计数（非破坏性，把历史已跑完的论文标为达标）。
- 新库无需手动回填：`schema.py` 的 `SCHEMA_SQL` 已含三列，新论文入库即带 0 值。

### 关键陷阱

- FTS5 表使用 `tokenize='unicode61'` 分词器，英文词汇完整匹配。连字符术语（如 `GWO-DAGRU`）由 `_build_fts_query()` 自动包装为短语查询处理
- `chunks_fts` 必须用内联内容模式（`INSERT INTO ... SELECT`），不能用外部内容表模式
- `papers.db` 损坏修复：删除 `.db-wal` 和 `.db-shm` 后重建
- Embedding 批量计算：注意 batch_size 和 API 限频
- **单篇入库用 `tools/graph/import_hypergraph.py`，绝不用 `import_hyper.import_all()`**：后者 `os.remove(DB_PATH)` 全量重建，会清掉已有 papers/sections/chunks
- **超图提取路径已固定为 `litreview/data/`**：`batch_extract_all.py` 的 `ENTITIES_DIR/SUMMARIES_DIR/NORM_DIR` 现指向根 `data/`（旧版曾错指 `modules/util/batch/data/` 导致 json 写错位置、批量入口失效）；`tools/extract/extract_one.py` 与 `batch_extract_all.py` 共用 `process_one_paper`，落盘位置一致。断点续跑以 `papers` 表的 `entity_count/hyperedge_count/ac_rounds` 为准，不以 json 是否存在为准。
- **实体 ID 双域（唯一规则，别自己映射）**：`entities`（私有，按论文）与 `entities_merged`（公有，合并后）各自自增，**同一数字指两个不同实体**（如 `entities.id=14652` 是 wheat，`entities_merged.id=14652` 是 RayExtract）。因此两张超边表各存两列：
  - `participants` = 私有 `entities.id`，**原始锚点**：抽取时就知道，合并重跑也不会变；
  - `participants_merged` = 公有 `entities_merged.id`，**派生列**：合并时按 `_o2m` 重算，检索/语义用。
  写入走 `modules/database/id_domain.py`（`to_merged` / `to_private` / `refresh_table`）；合并结束 `merge.py` 自动刷新两张表；读取时公有列优先，为空或悬空（公有实体被重建）自动退回私有列现算（`retriever._merged_or_fallback`）。对外展示带前缀：`e:14729`（私有）/ `m:588`（公有）——搬运数字时务必带前缀。
  `entities_merged` 包含 `type='paper'` 的论文实体（由 `merge.py` 末尾单例纳入），否则"论文: 标题"类参与者会变孤儿。

- **公有实体身份：并查集式「只挂接、不删除」**。公有 `id` 永不复用——合并两个已存在的公有实体时，**id 小的（创建更早）当根**，另一个标 `status='merged_into'` + `canonical_id` 指向根，根的 `source_ids` 吃掉并集；根的 `name`/`description` 可原地更新（内容可变，身份不变）。三种状态：`active`（在用）/ `merged_into`（已被并入别的根）/ `superseded`（成员全部消失，只留身份记录）。`generation` 记录第几次合并写下的。
  - 解析：`id_domain.resolve(conn, mid)` 沿 `canonical_id` 走到根（`canonical_map()` 做了传递闭包）；`to_merged()` 返回的已经是根 id。
  - 新论文带来的同名私有实体：没有成员交集时按**规范名**匹配已有根，挂过去而不是另建一行。
  - 每次合并收尾会自动：① 同名多根只留最老的一个（其余 `merged_into`，成员并集写回根）② 成员全部失效的行标 `superseded`。
  - 体检：`python tools/doctor/doctor.py ids`（看状态分布 / 链断裂 / canonical 指向不存在）。
- **检索查询必须用英文关键词**：FTS5 当前 `unicode61` 不切分中文，中文查询 BM25=0 仅靠向量；详见「RAG 检索接口」顶部约束。根治需切 `trigram` 分词器并重建 FTS 索引（待实施，规模化中文语料前建议做）。

### 超图 CRUD 权限策略（微型智能体数据整理）

A-C 精炼（`refine.py`）与 Phase1 校验（`summary.py`）都给模型一套受限的 CRUD 操作权限，**统一规则**：

| 对象 | 允许操作 | 说明 |
|------|----------|------|
| 实体（entities） | **增 / 删 / 改**（full CRUD） | 漏抽的概念可补 ADD；错误实体可 DELETE；描述/类型可 UPDATE |
| 超边（hyperedges） | **增 / 改**（ADD + UPDATE） | **禁止 DELETE**——超边承载已抽取的观测事实，删了即丢信息 |

- 修复"孤儿参与者"（超边引用了不存在的实体名）时：**优先把该名称 ADD 为一条新实体**（full CRUD 允许），而不是改名或删超边；只有确属拼写/别名变体时才改为已有实体名。
- 权限已由代码强制：`refine.py` 与 `summary.py` 的 `_apply_crud` 都不处理 `delete_hyperedges`；`summary.py` 的校验提示词也已移除 `delete_hyperedges` 选项。
- 历史坑：早期 `refine.py` 的 fix prompt 误写 "Do NOT add new entities"，导致孤儿参与者无法修复、A-C 卡死 3 次；现已改为 "Entities support FULL CRUD; Hyperedges ADD+UPDATE only, NEVER delete"。

---
