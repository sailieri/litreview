"""
Standalone Actor-Critic quality loop for hypergraph extraction.

Reads an existing extraction result (entities + hyperedges), runs a quality
check via Critic (QA generation) → Actor (CRUD repair) loop, and saves the
improved result.

Can be run at ANY time after initial extraction — not coupled to the extraction pipeline.
"""

import argparse, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ..util.llm_client import load_config, call_llm, extract_json, get_step_config, normalize_field_aliases


# ── Critic prompt ──

CRITIC_PROMPT = """You are a quality inspector AND a question-asker for a hypergraph extraction system.
You will examine a research paper and the hypergraph (entities + hyperedges) extracted
from it. The lists below include FULL descriptions — read every description carefully
against the paper.

## Paper Full Text
{paper_text}

## Current Entities (with full descriptions)
{entity_list}

## Current Hyperedges (with FULL descriptions)
{hyperedge_list}

## Task 1 — Grade existing hyperedges/entities
Report issues in three severity levels. Levels matter: L0/L1 block convergence, L2 does not.

### L0 — Broken content (blocks convergence)
The description is not a usable factual statement about the paper. Examples:
- Meta-commentary instead of facts: "This hyperedge is vague...", "Details are in <other edge>",
  "Merged into...", "Removed from consideration", "Should specify X"
- A critique OF the hyperedge itself rather than a statement about the paper's content
- Empty or placeholder descriptions

### L1 — Important coverage gap (blocks convergence)
Key information from the paper that the hypergraph does NOT capture:
- Key methods, datasets, or study designs central to the paper
- Important numerical results (accuracy, RMSE, R², thresholds, counts, intervals)
  present in the paper but absent from hyperedges
- Validation/comparison outcomes stated in the paper but missing
In `reason`, state the missing facts precisely (with values and units) so the Actor
can write the fix without searching the paper again.

### L2 — Minor issues (do NOT block convergence)
Slightly imprecise wording, missing secondary detail, debatable entity type, style.

## Task 2 — Probe coverage with questions
Generate Q&A pairs for questions the paper DEFINITIVELY answers. For each question,
decide whether the CURRENT hypergraph can answer it:
- If it CAN answer → pass, no action.
- If it CANNOT answer, but the paper definitively answers it → report an L1 coverage-gap
  issue. Set `question` to that question (the Actor will test their own edges against it —
  DO NOT include the answer here). In `reason`, specify exactly WHICH information is
  missing (values / units / conditions / relation) that blocks answering. Distinguish the
  failure mode in `type`:
    - "missing_hyperedge": the info is in the paper but NO hyperedge captured it
      → the Actor should ADD a new hyperedge.
    - "wrong_hyperedge": a hyperedge exists but records it incorrectly
      → the Actor should UPDATE that hyperedge.

{indicator_block}
Rules:
- Check EVERY hyperedge description against the paper. Meta-commentary (L0) is a known
  failure mode — look for it explicitly in every description.
- Only report issues actually supported by the paper text.
- `target` must be the exact hyperedge title (or entity name) the issue refers to. For
  missing_hyperedge, `target` may be a short topic descriptor for the missing edge.
- L1 issues raised via Task 2 MUST include `question` (the probing question, NO answer).
- If everything important is covered and no description is broken, output an empty issues list.

Output ONLY a JSON object:
{{"issues": [{{"level": "L0"|"L1"|"L2",
              "type": "meta_commentary"|"bad_hyperedge"|"bad_entity"|"wrong_type"|"missing_coverage"|"missing_hyperedge"|"wrong_hyperedge",
              "target": "...", "reason": "...", "question": "..."}}]}}
"""

# ── Task 3: domain indicator audit (optional, injected via {indicator_block}) ──

INDICATOR_AUDIT_TASK = """## Task 3 — Domain indicator audit (MANDATORY)
This paper collection defines domain-specific priority indicators (below). Audit EACH
one against the paper text (look for aliases/synonyms and equivalent wordings too) AND
the current hypergraph, then classify it as exactly one of:

- "covered": at least one hyperedge (or metric entity) already records the reported
  value(s)/findings → skip silently, no issue.
- "paper_missing": the paper does not report this indicator anywhere → skip silently,
  no issue. NEVER fabricate values for it — absence in the paper is NOT a defect.
- "hypergraph_missing": the paper DOES report it, but no hyperedge captures it
  → output an issue:
  {{"level": "L1", "type": "missing_hyperedge", "target": "<indicator name>",
    "reason": "Indicator '<name>' (aliases: ...) is reported in the paper (<which values,
    units, conditions, section>) but no hyperedge captures it. Fix: ADD a hyperedge
    recording the reported value(s) with units and conditions.",
    "question": "What <name> value(s) does this paper report?"}}

Priority indicators:
{indicator_list}

Report the complete audit of ALL indicators in the "indicator_audit" field of your
JSON output (a status for EVERY indicator; "note" = the reported value(s) with units,
or "not found in paper"). Do NOT skip any indicator in the audit.

JSON output shape for this task (add alongside "issues"):
"indicator_audit": [{{"name": "...", "status": "covered"|"paper_missing"|"hypergraph_missing", "note": "..."}}]
"""


def _build_indicator_block(indicators: list[dict]) -> str:
    """Render the Task-3 audit block; empty string when no indicator list is set
    (the Critic prompt then carries no Task-3 block)."""
    if not indicators:
        return ""
    lines = []
    for ind in indicators:
        bits = [ind.get("name", "?")]
        if ind.get("aliases"):
            bits.append(f"aliases: {', '.join(ind['aliases'])}")
        if ind.get("unit"):
            bits.append(f"unit: {ind['unit']}")
        lines.append("- " + "; ".join(bits))
    return INDICATOR_AUDIT_TASK.replace("{indicator_list}", "\n".join(lines))


def _load_indicators(path) -> list[dict]:
    """Load domain indicator list from YAML (returns [] on any failure, never raises)."""
    try:
        import yaml
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        inds = data.get("indicators")
        return inds if isinstance(inds, list) else []
    except Exception as e:
        print(f"  [AC] Warning: failed to load indicators from {path}: {e}")
        return []


# ── Actor prompt ──

ACTOR_PROMPT = """You are the extraction agent. The Critic has inspected the hypergraph
(entities + hyperedges) against the paper and reported issues in three severity levels:
L0 = broken content (e.g. your description is meta-commentary, not facts — this BLOCKS),
L1 = important coverage gaps (BLOCKS), L2 = minor (optional to fix).

## Current Entities
{entity_list}

## Current Hyperedges
{hyperedge_list}

## Critic's Issues (fix L0 and L1 first)
{issues_text}

## Instructions
Fix each issue using the appropriate CRUD operation:
- "meta_commentary" (L0) → update_hyperedges: REPLACE the description with a factual
  statement from the paper (values, units, conditions). NEVER write "This hyperedge is
  vague/redundant..." or "Details are in ..." — those are critique notes, not content.
  If the paper truly lacks the detail, write what the paper DOES say about the topic.
- "bad_entity"/"wrong_type" → update_entities (or delete_entities if truly wrong)
- "missing_coverage" (L1) → ADD new hyperedges (and new entities if participants
  don't exist yet) capturing the missing facts stated in the issue's reason.
- "bad_hyperedge" → update_hyperedges

When an L1 issue includes a `question`, NO standard answer is provided. Test your OWN
current hyperedges against the question: try to answer it using the entities/hyperedges
you see. If you can answer it, that info is already covered (no action needed). If you
cannot, the hypergraph lacks that info — act on the `type`:
- "missing_hyperedge" → ADD a new hyperedge (and any new entities required) capturing
  the missing facts stated in the issue's `reason`.
- "wrong_hyperedge" → UPDATE the existing hyperedge to record the correct facts from
  the paper.

You have FULL CRUD permissions on entities, and ADD/UPDATE on hyperedges:
- "entities": [...] -> ADD new entities
- "update_entities": [...] -> UPDATE existing entities (by name)
- "delete_entities": [{{"name": "..."}}] -> DELETE entities
- "hyperedges": [...] -> ADD new hyperedges (each with title, participants, description — EXACT key names; never "desc")
- "update_hyperedges": [...] -> UPDATE existing hyperedges (by title; field key is always "description", never "desc")

CRITICAL RULES:
1. NEW hyperedges MUST have ALL participants exist as entities. If a participant does not exist yet, ADD it as a new entity FIRST.
2. Hyperedge descriptions must contain ONLY factual data from the paper. NO commentary, NO notes, NO reasoning, NO meta-commentary. Example of FORBIDDEN description: "This hyperedge is vague. It should specify the slope threshold (<30°)." — if that is a Critic note, your job is to write the FIXED version instead, e.g. "GEDI footprints on terrain slopes steeper than 30° are filtered out by quality flags before L4A AGBD prediction."
3. Include ALL relevant numerical values with units in descriptions.
4. Split oversized hyperedges: if one hyperedge covers too many distinct observations, split into multiple.
5. Do NOT bake conditions/values into entity names (e.g. wrong: "CO2 adsorption at 25C", right: "CO2 adsorption capacity"). Use different hyperedges for different conditions.
6. CHECK all existing entities: if any entity is MISSING a description (empty or placeholder), UPDATE it with a proper description from the paper.
7. ENTITY vs HYPEREDGE: entities define WHAT things ARE (concept anchors). Hyperedges carry the numerical DATA. Entity descriptions must NOT contain specific values — those go in hyperedge descriptions.
8. NO DELETE hyperedges: You may ADD and UPDATE hyperedges, but NEVER delete them. Entities CAN still be deleted if incorrect.
9. Field names are EXACT: use "description" for both entities and hyperedges — NEVER "desc" or any other abbreviation. Also exact: "title", "participants", "name", "type".

Respond with a JSON object:
{{
  "entities": [...],
  "update_entities": [...],
  "delete_entities": [...],
  "hyperedges": [...],
  "update_hyperedges": [...]
}}

All fields optional. Include only fields with changes. Omit "entities" if no new entities needed.
"""


# ── Helper functions ──

def extract_json(raw: str) -> dict:
    """Extract JSON from LLM output (strip markdown fences) + 字段名别名归一。"""
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    return normalize_field_aliases(json.loads(raw))


def _parse_critic_issues(raw: str) -> list[dict]:
    """Extract graded issues from critic response: {"issues": [{level, type, target, reason}]}."""
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    data = json.loads(raw)
    issues = data.get("issues")
    if not isinstance(issues, list):
        raise TypeError("'issues' is not a list")
    # 兼容：模型可能仍输出 qa_pairs 顶级字段，忽略
    return [i for i in issues if isinstance(i, dict)]


def _parse_indicator_audit(raw: str) -> list[dict] | None:
    """Extract the optional 'indicator_audit' field from critic response.

    Returns None on absence or parse failure — audit is observational only,
    so a missing field must never break the A-C loop.
    """
    try:
        data = extract_json(raw)
    except Exception:
        return None
    audit = data.get("indicator_audit")
    return audit if isinstance(audit, list) else None


def _parse_qa_pairs(raw: str) -> tuple[list[dict], list[dict]]:
    """Extract QA pairs and issues from a critic response that carries qa_pairs."""
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    data = json.loads(raw)
    qa = data.get("qa_pairs", [])
    issues = data.get("issues", [])
    return qa, issues


def _build_critic_prompt(paper_text: str, entities: list[dict], hyperedges: list[dict],
                         indicators: list[dict] | None = None) -> str:
    """Build critic prompt: paper text + entities/hyperedges WITH FULL descriptions.

    Critic 每轮只基于 Actor 修改后的最新 entities/hyperedges 重审，不回喂
    上轮 issue 清单（避免复述而非重审）。indicators 非空时注入 Task 3 领域
    指标审计块。
    """
    e_full = _entity_list_full(entities)
    he_full = _hyperedge_list_full(hyperedges)
    return (CRITIC_PROMPT
            .replace("{paper_text}", paper_text)
            .replace("{entity_list}", e_full)
            .replace("{hyperedge_list}", he_full)
            .replace("{indicator_block}", _build_indicator_block(indicators or [])))


def _build_actor_prompt(entities: list[dict], hyperedges: list[dict],
                        issues: list[dict]) -> str:
    """Build actor prompt: current state + critic issues (L0/L1/L2)."""
    entity_preview = _entity_list_full(entities)
    he_preview = _hyperedge_list_full(hyperedges)

    # L0/L1 first, then L2 — actor prioritizes blockers
    order = {"L0": 0, "L1": 1, "L2": 2}
    sorted_issues = sorted(issues, key=lambda i: order.get(i.get("level", "L2"), 3))
    issues_lines = []
    for i, iss in enumerate(sorted_issues):
        q = f" | question: {iss.get('question','')}" if iss.get("question") else ""
        issues_lines.append(
            f"  Issue#{i} [{iss.get('level','L2')}|{iss.get('type','?')}] "
            f"target: {iss.get('target','?')}{q} — {iss.get('reason','')}"
        )
    issues_text = "\n".join(issues_lines) if issues_lines else "(no issues reported)"

    return (ACTOR_PROMPT
            .replace("{entity_list}", entity_preview)
            .replace("{hyperedge_list}", he_preview)
            .replace("{issues_text}", issues_text))


# ── Helper functions ──

def _entity_list_full(entities: list[dict]) -> str:
    """Entities WITH full descriptions (Critic must see everything)."""
    lines = []
    for i, e in enumerate(entities):
        lines.append(f"  {i}. [{e.get('type','?')}] {e['name']}\n     desc: {e.get('description','')}")
    return "\n".join(lines)


def _hyperedge_list_full(hyperedges: list[dict]) -> str:
    """Hyperedges WITH FULL descriptions — no truncation (Critic's core requirement)."""
    lines = []
    for i, he in enumerate(hyperedges):
        title = he.get("title", "(no title)")
        parts = he.get("participants", [])
        desc = he.get("description", "")
        lines.append(f"  {i}. '{title}' -> {parts}\n     desc: {desc}")
    return "\n".join(lines)


def _apply_crud(entities: list[dict], hyperedges: list[dict],
                supplemental: dict) -> tuple[list[dict], list[dict]]:
    """Apply CRUD operations from LLM supplemental JSON to current state.

    Entities: full CRUD allowed (add/update/delete).
    Hyperedges: add/update only (delete is blocked to preserve information).
    """
    entities = list(entities)
    hyperedges = list(hyperedges)
    # 防御: 过滤缺 name 的实体（LLM 偶发输出畸形条目）
    entities = [e for e in entities if isinstance(e, dict) and e.get("name")]
    entity_name_map = {e["name"].strip().lower(): e for e in entities}
    he_title_map = {he.get("title", "").strip().lower(): he for he in hyperedges if he.get("title")}

    for ne in supplemental.get("entities", []):
        if not isinstance(ne, dict) or not ne.get("name"):
            print(f"    [!] 跳过缺 name 的 entity: {str(ne)[:80]}")
            continue
        key = ne["name"].strip().lower()
        if key not in entity_name_map:
            entities.append(ne)
            entity_name_map[key] = ne
            print(f"    + Entity: '{ne['name']}' ({ne.get('type', '?')})")

    for ue in supplemental.get("update_entities", []):
        if not isinstance(ue, dict) or not ue.get("name"):
            print(f"    [!] 跳过缺 name 的 update_entity: {str(ue)[:80]}")
            continue
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

    # Hyperedges: NO DELETE (preserve information, silently ignore delete_hyperedges)
    # for dhe in supplemental.get("delete_hyperedges", []):
    #     title = dhe.get("title", dhe) if isinstance(dhe, dict) else dhe
    #     key = title.strip().lower()
    #     if key in he_title_map:
    #         old = he_title_map[key]
    #         hyperedges.remove(old)
    #         del he_title_map[key]
    #         print(f"    - Hyperedge deleted: '{title}'")

    return entities, hyperedges


def run_actor_critic_loop(paper_text: str,
                          entities: list[dict], hyperedges: list[dict],
                          cfg: dict, max_rounds: int = 4,
                          paper_stem: str | None = None,
                          indicators: list[dict] | None = None) -> tuple[list[dict], list[dict], int]:
    """Actor-Critic quality loop: Critic holds the termination right.

    Each round:
      1. Critic reads paper + FULL entities/hyperedges (descriptions included)
         -> issues graded L0 (broken content) / L1 (coverage gaps) / L2 (minor)
      2. Convergence: at least one Actor repair has run AND no L0/L1 issues remain
         -> stop (L2 leftovers are tolerated)
      3. Actor fixes issues via CRUD (L0/L1 prioritized), validated for structural
         consistency (entity completeness + participant references), retries up to 3x
    """
    import json, os
    log_path = None
    if paper_stem:
        log_path = Path(__file__).resolve().parent.parent.parent / "data" / "ac_log.jsonl"

    actor_ran = False           # 收敛前提：Actor 至少执行过一次修复
    round_results = []
    rounds_done = 0

    for ac_round in range(max_rounds):
        # ── Critic：全文审读，输出分级 issues ──
        critic_prompt = _build_critic_prompt(paper_text, entities, hyperedges, indicators)
        print(f"  [AC R{ac_round+1}] Critic inspecting (full descriptions)...")

        issues = None
        for cr_attempt in range(3):
            critic_response = call_llm(
                [{"role": "user", "content": critic_prompt}],
                cfg, max_tokens=16384
            )
            try:
                issues = _parse_critic_issues(critic_response)
                break
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f"  [AC R{ac_round+1}] Critic JSON parse failed (attempt "
                      f"{cr_attempt+1}/3): {e}")
        else:
            round_results.append({"round": ac_round + 1, "status": "critic_fail",
                                  "error": "persistent JSON parse failure"})
            break

        if issues is None:
            break

        # ── Task 3 指标审计结果（仅观测记录，不影响收敛判定；
        #    hypergraph_missing 已作为 L1 issue 进入 Actor 流程）──
        audit = _parse_indicator_audit(critic_response)
        audit_summary = None
        if indicators:
            if audit is None:
                audit_summary = {"total": len(indicators), "error": "critic did not return indicator_audit"}
                print(f"  [AC R{ac_round+1}] ⚠️ Indicator audit missing from critic response")
            else:
                st = {"covered": 0, "paper_missing": 0, "hypergraph_missing": 0}
                for a in audit:
                    s = a.get("status", "?")
                    st[s] = st.get(s, 0) + 1
                audit_summary = {"total": len(indicators), **st}
                print(f"  [AC R{ac_round+1}] Indicator audit: "
                      f"covered={st['covered']} paper_missing={st['paper_missing']} "
                      f"hypergraph_missing={st['hypergraph_missing']}")

        by_level = {"L0": [], "L1": [], "L2": []}
        for iss in issues:
            by_level.setdefault(iss.get("level", "L2"), []).append(iss)
        n0, n1, n2 = len(by_level["L0"]), len(by_level["L1"]), len(by_level["L2"])
        print(f"  [AC R{ac_round+1}] Critic: L0={n0} L1={n1} L2={n2}")

        # ── 收敛判定：Actor 修过至少一次，且无 L0/L1 ──
        if actor_ran and n0 == 0 and n1 == 0:
            print(f"  [AC] Converged after {rounds_done} actor run(s): "
                  f"no L0/L1 issues ({n2} L2 tolerated)")
            round_results.append({"round": ac_round + 1, "status": "converged",
                                  "L0": n0, "L1": n1, "L2": n2})
            break

        if not issues:
            # 首轮就零意见：说明既有超图已经很干净，也允许收敛（但记录 actor 未跑）
            print(f"  [AC R{ac_round+1}] Critic found no issues at all, converged")
            round_results.append({"round": ac_round + 1, "status": "converged_clean",
                                  "L0": 0, "L1": 0, "L2": 0})
            break

        # ── Actor 修复（L0/L1 优先），结构校验不过重试 ──
        round_ok = False
        missing_desc, missing_name, he_issues, he_desc_missing = [], [], [], []
        for actor_try in range(3):
            actor_prompt = _build_actor_prompt(entities, hyperedges, issues)
            print(f"  [AC R{ac_round+1}] Actor repairing (try {actor_try+1})...")

            actor_response = call_llm(
                [{"role": "user", "content": actor_prompt}],
                cfg, max_tokens=16384
            )
            try:
                crud = extract_json(actor_response)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"  [AC R{ac_round+1}] Actor JSON parse failed: {e}")
                break
            if not isinstance(crud, dict):
                print(f"  [AC R{ac_round+1}] Actor returned non-object JSON "
                      f"(type={type(crud).__name__}), skipping CRUD")
                crud = {}

            crud.pop("qa_coverage", None)  # 忽略模型可能附带的 qa_coverage 字段
            has_changes = any(k in crud for k in
                              ["entities", "update_entities", "delete_entities",
                               "hyperedges", "update_hyperedges"])
            if has_changes:
                entities, hyperedges = _apply_crud(entities, hyperedges, crud)
                actor_ran = True

            # ── 结构校验（骨架级：实体完整性 + 参与者引用一致）──
            missing_desc = [e for e in entities if not (e.get("description") or "").strip()]
            missing_name = [e for e in entities if not (e.get("name") or "").strip()]
            entity_ok = len(missing_desc) == 0 and len(missing_name) == 0

            entity_names = {e["name"].strip().lower() for e in entities}
            he_issues = []
            for he in hyperedges:
                for p in he.get("participants", []):
                    if isinstance(p, str) and p.strip().lower() not in entity_names:
                        he_issues.append(p)
            he_desc_missing = [he.get("title") or "(no title)" for he in hyperedges
                               if not str(he.get("description") or "").strip()]

            print(f"  [AC R{ac_round+1}] Actor done"
                  f"{' +CRUD' if has_changes else ''}"
                  f"{' entity:ok' if entity_ok else f' entity:⚠️{len(missing_desc)}缺描述+{len(missing_name)}缺名'}"
                  f"{' he:ok' if not he_issues and not he_desc_missing else f' he:⚠️{len(he_issues)}参与者不匹配+{len(he_desc_missing)}缺描述'}")

            all_ok = entity_ok and not he_issues and not he_desc_missing
            if all_ok:
                round_ok = True
                break

            # 结构校验不过 → 精准点名让 Actor 重试
            fix_lines = []
            if missing_desc:
                fix_lines.append("Add/fix description for: " + ", ".join(e["name"] for e in missing_desc[:8]))
            if missing_name:
                fix_lines.append("Fix name (currently empty): " + ", ".join(e.get("name", "(?)") for e in missing_name[:5]))
            if he_issues:
                fix_lines.append(
                    "Orphan participants (appear in a hyperedge but no matching entity): "
                    + ", ".join(he_issues[:8])
                    + ". You MUST add each as a NEW entity in your 'entities' output "
                    "(full entity CRUD is allowed) — give it a proper type and a real "
                    "definition from the paper. Only rename the hyperedge participant "
                    "to an existing entity when it is purely a typo/variant."
                )
            if he_desc_missing:
                fix_lines.append(
                    "Hyperedges missing 'description' (EXACT key, never 'desc'): "
                    + ", ".join(he_desc_missing[:6])
                    + " — rewrite each with a factual description from the paper "
                    "using update_hyperedges."
                )
            issues = [{"level": "L0", "type": "bad_entity", "target": "(structural)",
                       "reason": "; ".join(fix_lines)}]
        else:
            print(f"  [AC R{ac_round+1}] Actor could not fix all structural issues after 3 attempts")

        # ── Backstop：孤儿参与者自动补 stub 实体（骨架自洽兜底）──
        entity_names = {e["name"].strip().lower() for e in entities}
        auto_added = []
        for he in hyperedges:
            for p in he.get("participants", []):
                if isinstance(p, str) and p.strip() and p.strip().lower() not in entity_names:
                    entities.append({
                        "name": p.strip(),
                        "type": "concept",
                        "description": "(auto-added stub to satisfy hyperedge participant reference; verify and enrich)",
                    })
                    entity_names.add(p.strip().lower())
                    auto_added.append(p.strip())
        if auto_added:
            print(f"  [AC R{ac_round+1}] Backstop auto-added {len(auto_added)} stub entity(ies): "
                  + ", ".join(auto_added[:8]))

        rounds_done += 1
        round_results.append({
            "round": ac_round + 1,
            "status": "ok" if round_ok else "partial",
            "entities": len(entities),
            "hyperedges": len(hyperedges),
            "critic": {"L0": n0, "L1": n1, "L2": n2},
            "indicator_audit": audit_summary,
        })

        # Save log after each round
        if log_path and paper_stem:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_entry = {"paper": paper_stem, "round": ac_round + 1,
                         "status": "ok" if round_ok else "partial",
                         "entities": len(entities), "hyperedges": len(hyperedges),
                         "critic": {"L0": n0, "L1": n1, "L2": n2},
                         "indicator_audit": audit_summary}
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return entities, hyperedges, rounds_done


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Actor-Critic quality loop for hypergraph extraction")
    parser.add_argument("--normalized", "-n", required=True,
                        help="Path to normalized paper .md file")
    parser.add_argument("--entities", "-e", required=True,
                        help="Path to entities JSON file (input)")
    parser.add_argument("--output", "-o", required=True,
                        help="Path to save improved entities JSON (output)")
    parser.add_argument("--max-rounds", type=int, default=4,
                        help="Max Actor-Critic rounds (default: 4)")
    parser.add_argument("--indicators", default="auto",
                        help="Domain indicator YAML for Task 3 audit "
                             "(default: auto = data_root/indicators.yaml if present; "
                             "pass an explicit path or 'off' to disable)")
    args = parser.parse_args()

    # Load paper text
    with open(args.normalized, encoding="utf-8") as f:
        paper_text = f.read()

    # Load existing extraction
    with open(args.entities, encoding="utf-8") as f:
        data = json.load(f)

    entities = data.get("items", [])
    hyperedges = data.get("hyperedges", [])
    metadata = data.get("metadata")

    print(f"Loaded: {len(entities)} entities, {len(hyperedges)} hyperedges")
    print(f"Starting Actor-Critic loop ({args.max_rounds} max rounds)...\n")

    # Load config (use extraction section if available, else llm)
    cfg = load_config()
    ac_cfg = get_step_config("refine", cfg)

    # Resolve domain indicators (Task 3 audit): explicit path > data_root/indicators.yaml > off
    indicators = None
    ind_arg = (args.indicators or "").strip()
    if ind_arg.lower() == "off":
        ind_arg = ""
    elif ind_arg.lower() in ("", "auto"):
        from modules.paths import data_root
        cand = data_root() / "indicators.yaml"
        ind_arg = str(cand) if cand.exists() else ""
    if ind_arg:
        if Path(ind_arg).exists():
            indicators = _load_indicators(ind_arg)
            print(f"Domain indicators: {len(indicators)} loaded from {ind_arg}")
        else:
            print(f"Domain indicators: file not found ({ind_arg}), audit disabled")
    else:
        print("Domain indicators: none (Task 3 audit disabled)")

    entities, hyperedges, rounds_done = run_actor_critic_loop(
        paper_text=paper_text,
        entities=entities,
        hyperedges=hyperedges,
        cfg=ac_cfg,
        max_rounds=args.max_rounds,
        indicators=indicators,
    )

    # Save output
    output = {"items": entities, "hyperedges": hyperedges}
    if metadata:
        output["metadata"] = metadata

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nDone! Saved: {args.output}")
    print(f"  {len(entities)} entities, {len(hyperedges)} hyperedges")
    print(f"  A-C rounds completed: {rounds_done}")
