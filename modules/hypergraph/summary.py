"""
Paper pipeline: multi-turn LLM conversation for one paper.

Turn 1: Write narrative summary (500-800 words, 4 sections)
Turn 2: Extract hypergraph entities (JSON)
Turn 3: Extract per-paper hyperedges (JSON) — optional
Turn 4+: CRUD validation loop — optional

Saves:
  data/summaries/<paper>.json        — narrative summary
  data/entities/<paper>.json         — extracted entities + hyperedges
"""

import argparse, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ..util.llm_client import load_config, call_llm, extract_json, get_step_config, normalize_field_aliases

SUMMARY_PROMPT = """你是一位科研助手。以下是**一篇论文的规范化全文**。请用**叙事式**写一篇论文摘要，500字左右。

要求包含以下四部分，各部分自然地融入叙事中，不要用分点罗列：

1. 研究背景与目标：为什么做这个研究？要解决什么问题？
2. 数据与方法：用了什么数据？做了什么分析？
3. 核心发现：最重要的结果是什么？写必要的关键数值（如最佳精度、核心对比结论），不用罗列全部数据——细粒度数值后续通过RAG检索
4. 结论与方法论判断：作者的核心结论是什么？这个方法有什么适用范围或局限性？

**写作纪律：**
- 语言为中文，自然段落叙述，逻辑流畅
- 不要写展望段，不要重复结论
- 不要用"总的来说""综上所述"等套话
- 关键数值要写（如最佳精度、核心对比），不需要全部数据——细粒度数值走RAG检索
- 重点：让AI读完后能回答"这篇论文做了什么、用了什么方法、结论是什么"

论文全文：
{paper_text}"""


ENTITIES_PROMPT = """Based on the same paper above, extract all important technical entities as JSON.

Entities are reusable concept anchors. Types: material, method, metric, concept, mechanism, process, instrument, dataset.
Exclude: author names, institution names, reference citations, journal names, funding information.

CRITICAL RULES for descriptions:
- description must explain what the entity IS, not what value it has
- ❌ BAD: "MCBF average biomass" → "approximately 172 Mg/ha"  ← 这是数值，不是定义
- ✅ GOOD: "MCBF average biomass" → "Mean aboveground biomass of the Mountainous Coniferous Broadleaf Forest type"
- ❌ BAD: "GEDI" → "a spaceborne LiDAR with 25m footprints"  ← footprint size 是数值
- ✅ GOOD: "GEDI" → "NASA's spaceborne LiDAR instrument aboard the ISS"

Numerical values, performance metrics, experimental results MUST go into hyperedges, NOT entity descriptions.

Type guidance:
- Material samples: type "material" (e.g. NMC-0.5, hard carbon)
- Instruments, sensors, satellites: type "instrument" (e.g. GEDI, ALS, SEM)
- Datasets, data products: type "dataset" (e.g. GEDI L4A, MODIS)
- Methods, algorithms, models: type "method"
- Metrics, indicators: type "metric"
- Abstract concepts: type "concept"
- Mechanisms: type "mechanism"
- Processes: type "process"

Field-name discipline: use EXACTLY "name", "type", "description" — never abbreviate "description" as "desc" or any other key.

Output ONLY a JSON object with an "items" array:
{"items": [{"name": "...", "type": "...", "description": "..."}]}"""


HYPEREDGES_PROMPT = """Now, based on the entities you just extracted, create per-paper hyperedges.

A hyperedge links MULTIPLE entities to describe one complete observation from this paper.
Each hyperedge has:
- title: a short unique identifier (e.g. "GEDI vs ALS RMSE comparison")
- participants: list of entity names (from the entities you already extracted)
- description: a coherent narrative paragraph with ALL numerical findings

Rules:
- Group related metrics about the SAME experiment into ONE hyperedge
- Each experiment/condition gets its own hyperedge
- A hyperedge typically has 2-6 participants
- The first participant is usually the main subject
- Field names are EXACT: "title", "participants", "description" — never write "desc" or any abbreviation for "description"

Example:
{"hyperedges": [{"title": "GEDI L4A validation", "participants": ["GEDI L4A", "ALS", "RMSE"], "description": "GEDI L4A AGBD validated against ALS reference shows RMSE of 25.3 Mg/ha."}]}

Output ONLY a JSON object: {"hyperedges": [{"title": "...", "participants": ["..."], "description": "..."}]}"""


# ── CRUD validation helpers ──

def extract_json(raw: str) -> dict:
    """Extract JSON from LLM output (strip markdown fences) + 字段名别名归一。"""
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    return normalize_field_aliases(json.loads(raw))


def _validate_hyperedges(entities: list[dict], hyperedges: list[dict]) -> list[dict]:
    """Check which hyperedge participant names don't exist in entities."""
    entity_names = {e["name"].strip().lower() for e in entities}
    mismatches = []
    for he in hyperedges:
        for p in he.get("participants", []):
            if p.strip().lower() not in entity_names:
                mismatches.append({
                    "name": p,
                    "hyperedge_title": he.get("title", "(no title)"),
                    "hyperedge_participants": he["participants"],
                })
    return mismatches


def _validate_entities(entities: list[dict]) -> list[dict]:
    """Check if any entities are missing required fields (name, type, description)."""
    issues = []
    for i, e in enumerate(entities):
        missing = []
        if not e.get("name", "").strip():
            missing.append("name")
        if not e.get("type", "").strip():
            missing.append("type")
        if not e.get("description", "").strip():
            missing.append("description")
        if missing:
            issues.append({
                "index": i,
                "name": e.get("name", "(empty)"),
                "missing": missing,
            })
    return issues


def _validate_hyperedge_fields(hyperedges: list[dict]) -> list[dict]:
    """语法检测第二层防护：检查超边必填字段（title/description）缺失。
    缺失即点名回喂模型重写，并在反馈里提醒用规范键名（description 而非 desc）。"""
    issues = []
    for i, he in enumerate(hyperedges):
        missing = []
        if not str(he.get("title") or "").strip():
            missing.append("title")
        if not str(he.get("description") or "").strip():
            missing.append("description")
        if missing:
            issues.append({
                "index": i,
                "title": he.get("title") or "(no title)",
                "missing": missing,
            })
    return issues


def _get_paper_context(paper_text: str, name: str, window: int = 2) -> str:
    """Get surrounding lines from paper text for a given entity name."""
    lines = paper_text.split("\n")
    name_lower = name.lower()
    hits = []
    for i, line in enumerate(lines):
        if name_lower in line.lower():
            start = max(0, i - window)
            end = min(len(lines), i + window + 1)
            snippet = "\n".join(f"  L{j+1}|{lines[j][:200]}" for j in range(start, end))
            hits.append(snippet)
            if len(hits) >= 3:
                break
    return "\n---\n".join(hits) if hits else "(not found in visible text)"


def _entity_list_preview(entities: list[dict]) -> str:
    lines = []
    for i, e in enumerate(entities):
        lines.append(f"  {i}. [{e.get('type','?')}] {e['name']}")
    return "\n".join(lines)


def _hyperedge_list_preview(hyperedges: list[dict]) -> str:
    lines = []
    for i, he in enumerate(hyperedges):
        title = he.get("title", "(no title)")
        parts = he.get("participants", [])
        lines.append(f"  {i}. '{title}' -> {parts}")
    return "\n".join(lines)


def _build_validation_prompt(missing_list, paper_context, entity_list, hyperedge_list):
    template = r'''The automatic validation found issues in the extracted entities and hyperedges. You have CRUD permissions.

## Issues detected
The following entity names appear in hyperedges but do not match any extracted entity:
__MISSING_LIST__

## Paper context (surrounding lines):
__PAPER_CONTEXT__

## Current entity list:
__ENTITY_LIST__

## Current hyperedge list:
__HYPEREDGE_LIST__

## CRUD Instructions
Your response can include any combination of these fields:

### Entities
- "entities": [...] -- ADD new entities
- "update_entities": [{"name": "...", "type": "...", "description": "..."}] -- UPDATE existing entities (matched by name)
- "delete_entities": [{"name": "..."}] -- DELETE entities by name

### Hyperedges
- "hyperedges": [...] -- ADD new hyperedges (each needs title, participants, description — EXACT key names; never "desc")
- "update_hyperedges": [{"title": "...", "participants": ["..."], "description": "..."}] -- UPDATE existing (matched by title; key is always "description", never "desc")

NOTE: Hyperedges support ADD and UPDATE only — they MUST NOT be deleted (preserve
extracted information). Entities support full CRUD (add/update/delete).

Rules:
- For each missing entity: decide whether to ADD it, or UPDATE the hyperedge to use an existing entity name
- If an existing entity has wrong type or poor description, UPDATE it
- Omit any field that has no changes

Output ONLY a valid JSON object.'''
    return (template
        .replace("__MISSING_LIST__", missing_list)
        .replace("__PAPER_CONTEXT__", paper_context)
        .replace("__ENTITY_LIST__", entity_list)
        .replace("__HYPEREDGE_LIST__", hyperedge_list))


def _apply_crud(entities: list[dict], hyperedges: list[dict],
                supplemental: dict) -> tuple[list[dict], list[dict]]:
    """Apply CRUD operations from LLM supplemental JSON to current state."""
    entities = list(entities)
    hyperedges = list(hyperedges)
    entity_name_map = {e["name"].strip().lower(): e for e in entities}
    he_title_map = {he.get("title", "").strip().lower(): he for he in hyperedges if he.get("title")}

    for ne in supplemental.get("entities", []):
        key = ne["name"].strip().lower()
        if key not in entity_name_map:
            entities.append(ne)
            entity_name_map[key] = ne
            print(f"    + Entity: '{ne['name']}' ({ne.get('type', '?')})")

    for ue in supplemental.get("update_entities", []):
        key = ue["name"].strip().lower()
        if key in entity_name_map:
            old = entity_name_map[key]
            idx = entities.index(old)
            merged = dict(old)
            for k, v in ue.items():
                if v is not None and k != "name":
                    merged[k] = v
            entities[idx] = merged
            entity_name_map[key] = merged
            print(f"    ~ Entity updated: '{ue['name']}'")

    for de in supplemental.get("delete_entities", []):
        name = de.get("name", de) if isinstance(de, dict) else de
        key = name.strip().lower()
        if key in entity_name_map:
            old = entity_name_map[key]
            entities.remove(old)
            del entity_name_map[key]
            print(f"    - Entity deleted: '{name}'")

    for nhe in supplemental.get("hyperedges", []):
        title = nhe.get("title", "").strip().lower()
        if title and title not in he_title_map:
            hyperedges.append(nhe)
            he_title_map[title] = nhe
            print(f"    + Hyperedge: '{nhe.get('title', '(no title)')}'")
        elif title:
            print(f"    ~ Hyperedge '{nhe.get('title')}' already exists, skipped")
        else:
            hyperedges.append(nhe)

    for uhe in supplemental.get("update_hyperedges", []):
        title = uhe.get("title", "").strip().lower()
        if title in he_title_map:
            old = he_title_map[title]
            idx = hyperedges.index(old)
            merged = dict(old)
            for k, v in uhe.items():
                if v is not None:
                    merged[k] = v
            hyperedges[idx] = merged
            he_title_map[title] = merged
            print(f"    ~ Hyperedge updated: '{uhe.get('title')}'")

    # Hyperedges: NO DELETE (preserve extracted information). Entities support
    # full CRUD above; hyperedges are add/update only.
    return entities, hyperedges


def _build_paper_info(metadata: dict | None) -> str:
    """从 metadata 构造论文信息前缀（title/authors/year/keywords）。"""
    if not metadata:
        return ""
    return (f"Title: {metadata.get('title', '')}\n"
            f"Authors: {metadata.get('authors', '')}\n"
            f"Year: {metadata.get('year', '')}\n"
            f"Keywords: {', '.join(metadata.get('keywords', []))}\n\n")


def run_summary(normalized_path: str, metadata: dict | None = None,
                step: str = "summary", dry_run: bool = False) -> dict:
    """Turn1 ONLY: 叙事摘要。独立于超图提取（不产 entities/hyperedges）。

    解耦设计：summary 归入基础 RAG 提取阶段，与超图提取无关。
    summary 本身不依赖超图，超图提取也不依赖 summary——normalized 全文已提供上下文。
    """
    with open(normalized_path, encoding="utf-8") as f:
        paper_text = f.read()
    if dry_run:
        return {"narrative": "(dry-run, no LLM)"}
    cfg = load_config()
    extract_cfg = get_step_config(step, cfg)
    summary_prompt = SUMMARY_PROMPT.format(paper_text=_build_paper_info(metadata) + paper_text)
    narrative = call_llm(
        [{"role": "user", "content": summary_prompt}],
        extract_cfg, max_tokens=1024)
    return {"narrative": narrative}


def run_extract_hyper(normalized_path: str, metadata: dict | None = None,
                      max_validation_rounds: int = 3, step: str = "extract_hyper",
                      dry_run: bool = False) -> dict:
    """Turn2+3+CRUD: entities + hyperedges + validation。独立于 summary。

    解耦设计：直接用规范化全文作为上下文（不依赖 Turn1 narrative），
    因为 normalized 论文已提供完整信息。返回
    {"items": entities, "hyperedges": hyperedges, "metadata": ...}。
    """
    with open(normalized_path, encoding="utf-8") as f:
        paper_text = f.read()
    if dry_run:
        return {"items": [], "hyperedges": [], "metadata": metadata or {}}
    cfg = load_config()
    extract_cfg = get_step_config(step, cfg)
    paper_ctx = _build_paper_info(metadata) + paper_text

    # ── Turn 2: extract entities ──
    print("  [Turn 2] Extracting entities...")
    raw_entities = call_llm(
        [{"role": "user", "content": paper_ctx + "\n\n" + ENTITIES_PROMPT}],
        extract_cfg, max_tokens=8192)
    try:
        entities = extract_json(raw_entities).get("items", [])
    except (json.JSONDecodeError, KeyError):
        entities = []
        print(f"  [Turn 2] JSON parse failed, raw={raw_entities[:200]}")
    print(f"  [Turn 2] {len(entities)} entities extracted")

    t2_messages = [
        {"role": "user", "content": paper_ctx + "\n\n" + ENTITIES_PROMPT},
        {"role": "assistant", "content": raw_entities},
    ]

    # ── Turn 3: extract per-paper hyperedges ──
    print("  [Turn 3] Extracting per-paper hyperedges...")
    raw_hyperedges = call_llm(
        t2_messages + [{"role": "user", "content": HYPEREDGES_PROMPT}],
        extract_cfg, max_tokens=12000)
    try:
        hyperedges = extract_json(raw_hyperedges).get("hyperedges", [])
    except (json.JSONDecodeError, KeyError):
        hyperedges = []
        print(f"  [Turn 3] JSON parse failed, raw={raw_hyperedges[:200]}")
    print(f"  [Turn 3] {len(hyperedges)} hyperedges extracted")

    # ── Validation + CRUD loop: Turn 4+ ──
    for round_idx in range(max_validation_rounds):
        mismatches = _validate_hyperedges(entities, hyperedges)
        entity_issues = _validate_entities(entities)
        he_field_issues = _validate_hyperedge_fields(hyperedges)
        if not mismatches and not entity_issues and not he_field_issues:
            print(f"  [Validation] OK All {len(hyperedges)} hyperedges self-consistent, "
                  f"all {len(entities)} entities complete")
            break
        print(f"  [Validation] {len(mismatches)} participant mismatches, "
              f"{len(entity_issues)} entity field issues, "
              f"{len(he_field_issues)} hyperedge field issues (round {round_idx + 1})")
        all_issues = []
        seen_names = set()
        unique_mismatches = []
        for m in mismatches:
            key = m["name"].strip().lower()
            if key not in seen_names:
                seen_names.add(key)
                unique_mismatches.append(m)
        missing_lines = []
        context_lines = []
        for m in unique_mismatches:
            missing_lines.append(
                f"  - '{m['name']}' (hyperedge '{m['hyperedge_title']}': "
                f"participants={m['hyperedge_participants']})")
            ctx = _get_paper_context(paper_text, m["name"])
            context_lines.append(f"--- '{m['name']}' ---\n{ctx}")
        if unique_mismatches:
            all_issues.append(
                "## Missing Participants\n"
                "The following entity names appear in hyperedges but are not in the entity list:"
                "\n" + "\n".join(missing_lines) +
                "\n\n## Paper Context for Missing Participants\n" + "\n\n".join(context_lines))
        if entity_issues:
            issue_lines = []
            for ei in entity_issues:
                issue_lines.append(
                    f"  [{ei['index']}] '{ei['name']}' missing: {', '.join(ei['missing'])}")
            all_issues.append(
                "## Incomplete Entities\n"
                "The following entities are missing required fields (name/type/description). "
                "For each missing description, use update_entities to add a proper description from the paper:\n"
                + "\n".join(issue_lines))
        if he_field_issues:
            hf_lines = [
                f"  [{hfi['index']}] '{hfi['title']}' missing: {', '.join(hfi['missing'])}"
                for hfi in he_field_issues]
            all_issues.append(
                "## Hyperedges Missing Required Fields\n"
                "The following hyperedges are missing required fields. Fix them with "
                'update_hyperedges. Field names are EXACT: "title", "participants", '
                '"description" — use "description", NEVER "desc":\n'
                + "\n".join(hf_lines))
        combined_issues = "\n\n".join(all_issues) if all_issues else "No issues found."
        feedback_prompt = _build_validation_prompt(
            missing_list=combined_issues,
            paper_context="\n\n".join(context_lines) if context_lines else "(no context)",
            entity_list=_entity_list_preview(entities),
            hyperedge_list=_hyperedge_list_preview(hyperedges))
        print(f"  [Turn {4 + round_idx}] CRUD round: {len(unique_mismatches)} issues")
        crud_raw = call_llm(
            t2_messages + [
                {"role": "user", "content": HYPEREDGES_PROMPT},
                {"role": "assistant", "content": raw_hyperedges},
                {"role": "user", "content": feedback_prompt},
            ], extract_cfg, max_tokens=4096)
        try:
            supplemental = extract_json(crud_raw)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  [Turn {4 + round_idx}] JSON parse failed: {e}")
            continue
        entities, hyperedges = _apply_crud(entities, hyperedges, supplemental)
        if not any(k in supplemental for k in
                   ["entities", "update_entities", "delete_entities",
                    "hyperedges", "update_hyperedges"]):
            print("  [Validation] No CRUD operations provided, stopping loop")
            break
    else:
        print(f"  [Validation] Reached max {max_validation_rounds} rounds")

    final_mismatches = _validate_hyperedges(entities, hyperedges)
    final_entity_issues = _validate_entities(entities)
    final_he_field_issues = _validate_hyperedge_fields(hyperedges)
    if final_mismatches or final_entity_issues or final_he_field_issues:
        msg = []
        if final_mismatches:
            msg.append(f"{len(final_mismatches)} participant mismatches")
        if final_entity_issues:
            msg.append(f"{len(final_entity_issues)} entities missing fields")
        if final_he_field_issues:
            msg.append(f"{len(final_he_field_issues)} hyperedges missing fields")
        print(f"  [Validation] Final issues: {', '.join(msg)}")

    return {"items": entities, "hyperedges": hyperedges, "metadata": metadata or {}}


def run_pipeline(normalized_path: str, metadata: dict | None = None,
                 client=None, max_validation_rounds: int = 3,
                 extract_hyperedges: bool = True, step: str = "extract",
                 dry_run: bool = False) -> tuple[dict, dict]:
    """Multi-turn extraction with optional hyperedges + CRUD validation.

    Args:
        normalized_path: path to normalized .md file
        metadata: optional metadata dict (title/authors/year/keywords)
        client: kept for backward-compat; if provided AND dry_run=True, skip LLM.
                Normally pass client=None so LLM runs via step config.
        max_validation_rounds: CRUD validation rounds
        extract_hyperedges: whether to extract per-paper hyperedges
        step: which LLM step config to use (default "extract", keep compat)
        dry_run: if True, skip all LLM calls (debug)

    Returns:
        (summary_dict, entities_dict)
        entities_dict contains {"items": [...], "hyperedges": [...]}
    """
    with open(normalized_path, encoding="utf-8") as f:
        paper_text = f.read()

    if dry_run:
        return (
            {"narrative": "(dry-run, no LLM)"},
            {"items": [], "hyperedges": [], "metadata": metadata or {}}
        )

    cfg = load_config()
    # Use step-based config
    extract_cfg = get_step_config(step, cfg)

    # Build base paper info string from metadata
    paper_info = ""
    if metadata:
        paper_info = f"Title: {metadata.get('title', '')}\nAuthors: {metadata.get('authors', '')}\nYear: {metadata.get('year', '')}\nKeywords: {', '.join(metadata.get('keywords', []))}\n\n"

    # ── Turn 1: narrative summary ──
    summary_prompt = SUMMARY_PROMPT.format(paper_text=paper_text)
    print("  [Turn 1] Writing narrative summary...")
    narrative = call_llm(
        [{"role": "user", "content": summary_prompt}],
        extract_cfg, max_tokens=1024
    )
    print(f"  [Turn 1] {len(narrative)} chars")

    base_messages = [
        {"role": "user", "content": summary_prompt},
        {"role": "assistant", "content": narrative},
    ]

    # ── Turn 2: extract entities ──
    print("  [Turn 2] Extracting entities...")
    raw_entities = call_llm(
        base_messages + [{"role": "user", "content": ENTITIES_PROMPT}],
        extract_cfg, max_tokens=8192
    )
    try:
        entities_data = extract_json(raw_entities)
        entities = entities_data.get("items", [])
    except (json.JSONDecodeError, KeyError):
        entities = []
        print(f"  [Turn 2] JSON parse failed, raw={raw_entities[:200]}")
    print(f"  [Turn 2] {len(entities)} entities extracted")

    # ── Turn 3: extract per-paper hyperedges (optional) ──
    hyperedges = []
    crud_messages = []
    if extract_hyperedges:
        print("  [Turn 3] Extracting per-paper hyperedges...")
        raw_hyperedges = call_llm(
            base_messages + [
                {"role": "user", "content": ENTITIES_PROMPT},
                {"role": "assistant", "content": raw_entities},
                {"role": "user", "content": HYPEREDGES_PROMPT},
            ],
            extract_cfg, max_tokens=12000
        )
        try:
            he_data = extract_json(raw_hyperedges)
            hyperedges = he_data.get("hyperedges", [])
        except (json.JSONDecodeError, KeyError):
            hyperedges = []
            print(f"  [Turn 3] JSON parse failed, raw={raw_hyperedges[:200]}")
        print(f"  [Turn 3] {len(hyperedges)} hyperedges extracted")

        # ── Validation + CRUD loop: Turn 4+ ──
        for round_idx in range(max_validation_rounds):
            # Check both hyperedge consistency and entity field completeness
            mismatches = _validate_hyperedges(entities, hyperedges)
            entity_issues = _validate_entities(entities)
            he_field_issues = _validate_hyperedge_fields(hyperedges)

            if not mismatches and not entity_issues and not he_field_issues:
                print(f"  [Validation] OK All {len(hyperedges)} hyperedges self-consistent, "
                      f"all {len(entities)} entities complete")
                break

            print(f"  [Validation] {len(mismatches)} participant mismatches, "
                  f"{len(entity_issues)} entity field issues, "
                  f"{len(he_field_issues)} hyperedge field issues (round {round_idx + 1})")

            # Build combined feedback
            all_issues = []

            # Part 1: Hyperedge participant mismatches
            seen_names = set()
            unique_mismatches = []
            for m in mismatches:
                key = m["name"].strip().lower()
                if key not in seen_names:
                    seen_names.add(key)
                    unique_mismatches.append(m)

            missing_lines = []
            context_lines = []
            for m in unique_mismatches:
                missing_lines.append(
                    f"  - '{m['name']}' (hyperedge '{m['hyperedge_title']}': "
                    f"participants={m['hyperedge_participants']})"
                )
                ctx = _get_paper_context(paper_text, m["name"])
                context_lines.append(f"--- '{m['name']}' ---\n{ctx}")

            if unique_mismatches:
                all_issues.append(
                    "## Missing Participants\n"
                    "The following entity names appear in hyperedges but are not in the entity list:"
                    "\n" + "\n".join(missing_lines) +
                    "\n\n## Paper Context for Missing Participants\n" + "\n\n".join(context_lines)
                )

            # Part 2: Entity field issues
            if entity_issues:
                issue_lines = []
                for ei in entity_issues:
                    issue_lines.append(
                        f"  [{ei['index']}] '{ei['name']}' missing: {', '.join(ei['missing'])}"
                    )
                all_issues.append(
                    "## Incomplete Entities\n"
                    "The following entities are missing required fields (name/type/description). "
                    "For each missing description, use update_entities to add a proper description from the paper:\n"
                    + "\n".join(issue_lines)
                )

            if he_field_issues:
                hf_lines = [
                    f"  [{hfi['index']}] '{hfi['title']}' missing: {', '.join(hfi['missing'])}"
                    for hfi in he_field_issues]
                all_issues.append(
                    "## Hyperedges Missing Required Fields\n"
                    "The following hyperedges are missing required fields. Fix them with "
                    'update_hyperedges. Field names are EXACT: "title", "participants", '
                    '"description" — use "description", NEVER "desc":\n'
                    + "\n".join(hf_lines))

            # Build combined feedback prompt
            combined_issues = "\n\n".join(all_issues) if all_issues else "No issues found."
            feedback_prompt = _build_validation_prompt(
                missing_list=combined_issues,
                paper_context="\n\n".join(context_lines) if context_lines else "(no context)",
                entity_list=_entity_list_preview(entities),
                hyperedge_list=_hyperedge_list_preview(hyperedges),
            )

            print(f"  [Turn {4 + round_idx}] CRUD round: {len(unique_mismatches)} issues")
            crud_raw = call_llm(
                base_messages + [
                    {"role": "user", "content": ENTITIES_PROMPT},
                    {"role": "assistant", "content": raw_entities},
                    {"role": "user", "content": HYPEREDGES_PROMPT},
                    {"role": "assistant", "content": raw_hyperedges},
                    {"role": "user", "content": feedback_prompt},
                ],
                extract_cfg, max_tokens=4096
            )

            crud_messages += [
                {"role": "user", "content": feedback_prompt},
                {"role": "assistant", "content": crud_raw},
            ]

            try:
                supplemental = extract_json(crud_raw)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"  [Turn {4 + round_idx}] JSON parse failed: {e}")
                continue

            entities, hyperedges = _apply_crud(entities, hyperedges, supplemental)

            if not any(k in supplemental for k in
                       ["entities", "update_entities", "delete_entities",
                        "hyperedges", "update_hyperedges"]):
                print("  [Validation] No CRUD operations provided, stopping loop")
                break
        else:
            print(f"  [Validation] Reached max {max_validation_rounds} rounds")

        final_mismatches = _validate_hyperedges(entities, hyperedges)
        final_entity_issues = _validate_entities(entities)
        if final_mismatches or final_entity_issues:
            msg = []
            if final_mismatches:
                msg.append(f"{len(final_mismatches)} participant mismatches")
            if final_entity_issues:
                msg.append(f"{len(final_entity_issues)} entities missing fields")
            raise ValueError(f"CRUD 验证未通过: {'; '.join(msg)}")
        print(f"  [Final] OK Self-consistent: {len(entities)} entities, {len(hyperedges)} hyperedges")

    return (
        {"narrative": narrative},
        {"items": entities, "hyperedges": hyperedges, "metadata": metadata or {}},
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summary + entity + hyperedge extraction")
    parser.add_argument("--normalized", "-n", required=True, help="Path to normalized .md file")
    parser.add_argument("--metadata", "-m", help="Path to metadata .json file (optional)")
    parser.add_argument("--summaries-dir", default=None, help="Output dir for summaries")
    parser.add_argument("--entities-dir", default=None, help="Output dir for entities")
    parser.add_argument("--max-validation-rounds", type=int, default=3,
                        help="Max CRUD validation rounds")
    parser.add_argument("--no-hyperedges", action="store_true",
                        help="Skip per-paper hyperedge extraction")
    args = parser.parse_args()

    data_dir = Path(__file__).resolve().parent.parent.parent / "data"
    summaries_dir = Path(args.summaries_dir) if args.summaries_dir else data_dir / "summaries"
    entities_dir = Path(args.entities_dir) if args.entities_dir else data_dir / "entities"

    stem = Path(args.normalized).stem

    metadata = None
    if args.metadata:
        with open(args.metadata, encoding="utf-8") as f:
            metadata = json.load(f)

    summary, result = run_pipeline(
        args.normalized, metadata=metadata,
        max_validation_rounds=args.max_validation_rounds,
        extract_hyperedges=not args.no_hyperedges,
    )

    summaries_dir.mkdir(parents=True, exist_ok=True)
    with open(summaries_dir / f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"Summary: {summaries_dir / f'{stem}.json'}")

    entities_dir.mkdir(parents=True, exist_ok=True)
    with open(entities_dir / f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"Entities: {entities_dir / f'{stem}.json'} ({len(result.get('items', []))} items, {len(result.get('hyperedges', []))} hyperedges)")
