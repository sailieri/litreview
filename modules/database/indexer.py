"""
Indexer: Process normalized papers → populate database.

Parses markdown headings generically into a tree structure.
No hardcoded section types — all headings are stored as-is.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.database.schema import get_db, init_db, DB_PATH
from modules.database.embedder import chunk_text, compute_embedding, vector_to_blob
from modules.paths import summaries_dir, normalized_dir
from modules.util.dedup_gate import (DuplicateError, audit_forced, check_duplicate,
                                     format_report, stem_of as _stem_of_path)


def _insert_chunks(conn, paper_id: int, section_id: int, text: str):
    """Split section text into chunks and insert into DB.
    Computes embedding synchronously for each chunk.
    """
    chunks = chunk_text(text)
    for i, chunk_text_content in enumerate(chunks):
        conn.execute(
            "INSERT INTO chunks (section_id, paper_id, content, chunk_index) VALUES (?, ?, ?, ?)",
            (section_id, paper_id, chunk_text_content, i),
        )


def _compute_embeddings(conn):
    """Compute embeddings for all chunks that don't have them yet."""
    rows = conn.execute("SELECT id, content FROM chunks WHERE vector IS NULL").fetchall()
    if not rows:
        return

    print(f"  Computing {len(rows)} embeddings...")
    for i, (cid, content) in enumerate(rows):
        vec = compute_embedding(content)
        if vec:
            conn.execute("UPDATE chunks SET vector=? WHERE id=?", (vector_to_blob(vec), cid))
        if (i + 1) % 5 == 0:
            conn.commit()
    conn.commit()


def parse_sections(markdown_text: str) -> list[dict]:
    """Parse markdown into a tree of sections based on heading levels.

    Rules:
      # heading  → level=1, parent=NULL
      ## heading → level=2, parent=most recent level<2
      ### etc.
    
    Returns:
        [{"title": "1. Introduction", "level": 2, "parent_id_ref": null|idx, "content": "..."}, ...]
        parent_id_ref is the index (in this list) of the parent section, or None.
    """
    heading_re = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
    
    # Find all heading positions
    matches = list(heading_re.finditer(markdown_text))
    if not matches:
        # No headings → single section
        return [{"title": "Full Text", "level": 1, "parent_id_ref": None, "content": markdown_text.strip()}]
    
    sections = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        
        # Content starts after this heading and ends at next heading
        start = m.end() + 1
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(markdown_text)
        content = markdown_text[start:end].strip()
        
        # Find parent: most recent section with lower level
        parent_ref = None
        for j in range(len(sections) - 1, -1, -1):
            if sections[j]["level"] < level:
                parent_ref = j
                break
        
        sections.append({
            "title": title,
            "level": level,
            "parent_id_ref": parent_ref,
            "content": content,
        })
    
    return sections


def normalize_section_node(node: dict, section_id: int, conn) -> dict:
    """Augment a section dict with its tree context (path, children, depth)."""
    # Get path to root
    path_parts = [node["title"]]
    cur = node
    while cur.get("parent_id_ref") is not None:
        # Look up parent from our parsed data — skip for now, 
        # this is for the output display
        break
    
    node["section_path"] = " / ".join(reversed(path_parts))
    return node


def index_paper(paper_title: str, normalized_text: str, 
                summary: dict | None = None,
                file_path: str = "",
                force: bool = False) -> int:
    """Index a paper into the database.

    查重闸门：命中且 force=False 抛
    DuplicateError，什么都不写；force=True 放行但打印报告并写审计（须先人工核对）。

    Args:
        paper_title: Paper title or filename.
        normalized_text: Cleaned markdown text.
        summary: Optional JSON summary dict.
        file_path: Path to the source file.
        force: 跳过拦截（误判时用，会写审计 data/dedup_forced.jsonl）。

    Returns:
        paper_id in the database.
    """
    conn = get_db()
    init_db(conn)

    # 论文元数据取自 summary，两种结构都支持：
    # 扁平（元数据在顶层）/ 嵌套（元数据在 "metadata" 子字典）
    if summary:
        meta = summary.get("metadata", summary)  # 有 metadata 子字典则取嵌套，否则按扁平读
        title = meta.get("title") or paper_title
        authors = meta.get("authors", "")
        year = meta.get("year")
        journal = meta.get("journal", "")
        doi = meta.get("doi", "")
        core_idea = meta.get("core_idea", "")
    else:
        title = paper_title
        authors = ""
        year = None
        journal = ""
        doi = ""
        core_idea = ""

    # ── 查重闸门（写入前校验）───────────────────────────────
    stem = _stem_of_path(file_path)
    # summary 缺失时 title 就是文件名，不作比对键（防两个文件名互撞误拦）
    gate_title = title if title != paper_title else None
    _ctx = f"数据库线 index_paper(stem={stem})"
    _cur = {"title": gate_title or paper_title, "doi": doi, "stem": stem}
    _hits = check_duplicate(title=gate_title, doi=doi, stem=stem, authors=authors,
                            exclude_stem=stem)
    if _hits:
        if force:
            audit_forced(_ctx, _cur, _hits, reason="force 放行 index_paper")
            print(format_report(_hits, _ctx + " [force 已放行]", _cur))
        else:
            raise DuplicateError(_hits, _ctx, _cur)

    cur = conn.execute(
        """INSERT INTO papers (title, authors, year, journal, doi, file_path)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (title, authors, year, journal, doi, file_path),
    )
    paper_id = cur.lastrowid

    # Parse sections into tree
    raw_sections = parse_sections(normalized_text)

    # Merge overview sections (title + abstract + keywords) into one
    merged_sections = []
    overview_parts = []
    body_start = None
    for i, sec in enumerate(raw_sections):
        title = sec["title"].strip().lower()
        # Body sections: start with a digit (e.g. "1. Introduction", "2. Materials")
        # or match common body patterns
        is_body = (sec["level"] >= 2 and (
            title[0].isdigit() or
            title.startswith("introduction") or
            title.startswith("materials") or
            title.startswith("methods") or
            title.startswith("results") or
            title.startswith("discussion") or
            title.startswith("conclusion") or
            title.startswith("experiment") or
            title.startswith("data")
        ))
        if not is_body and body_start is None:
            # Still in overview
            if sec["content"].strip():
                overview_parts.append(sec["content"].strip())
        else:
            if body_start is None:
                # First body section found — insert overview before it
                overview_text = "\n\n".join(overview_parts) if overview_parts else ""
                merged_sections.append({
                    "title": "Overview",
                    "level": 1,
                    "parent_id_ref": None,
                    "content": overview_text,
                })
                body_start = i
            merged_sections.append(sec)

    # If no body section found (everything is overview)
    if body_start is None:
        overview_text = "\n\n".join(overview_parts) if overview_parts else ""
        merged_sections = [{
            "title": "Overview",
            "level": 1,
            "parent_id_ref": None,
            "content": overview_text,
        }]

    raw_sections = merged_sections
    
    # Insert sections, tracking SQL ids for parent references
    section_id_map = {}  # parse index → SQL id
    for i, sec in enumerate(raw_sections):
        # parent 按合并后的列表重算：parent_id_ref 是 parse 阶段基于原始列表的索引，
        # 插入 Overview 段后两份列表错位，直接拿它当下标会取到错误 id。
        # 只认恰好 level-1 的父级；跳级缺层时置空，由前端按 level 回挂。
        parent_id = None
        for j in range(i - 1, -1, -1):
            if raw_sections[j]["level"] == sec["level"] - 1:
                parent_id = section_id_map[j]
                break
            if raw_sections[j]["level"] < sec["level"] - 1:
                break
        cur = conn.execute(
            "INSERT INTO sections (paper_id, title, level, parent_id, content) VALUES (?, ?, ?, ?, ?)",
            (paper_id, sec["title"], sec["level"], parent_id, sec["content"]),
        )
        section_id = cur.lastrowid
        section_id_map[i] = section_id

        # Create chunks for this section (embedding basis)
        _insert_chunks(conn, paper_id, section_id, sec["content"])

    conn.commit()
    # 向量化不在写入时同步执行，由建库流程（build.embeds）批量补齐

    conn.commit()
    conn.close()
    
    return paper_id


def index_all(normalized_dir: str, force: bool = False) -> list[int]:
    """Index all normalized .md files with their companion summaries.

    查重闸门：对非空库重跑会被逐篇拦截（幂等保护，避免整目录重复插入）。
    首次建空库不受影响；确需重跑加 force=True / --force。
    """
    from ..util.llm_client import LLMClient
    
    paper_ids = []
    n_dup = 0
    md_files = sorted(Path(normalized_dir).glob("*.md"))
    print(f"Found {len(md_files)} markdown files in {normalized_dir}")

    for md_path in md_files:
        fname = md_path.name
        stem = md_path.stem

        # Load normalized text
        with open(str(md_path), encoding="utf-8") as f:
            text = f.read()

        # Try to find companion JSON summary
        summary = None
        for sp in [
            summaries_dir() / f"{stem}.json",
            summaries_dir() / f"{stem.replace('_deepseek','')}.json",
        ]:
            if sp.exists():
                with open(sp, encoding="utf-8") as fh:
                    try:
                        summary = json.load(fh)
                    except json.JSONDecodeError:
                        pass
                break

        print(f"\n[{fname}]")
        try:
            pid = index_paper(stem, text, summary=summary, file_path=str(md_path),
                              force=force)
        except DuplicateError as e:
            n_dup += 1
            print(str(e))
            continue
        
        # Get tree info for display
        conn = get_db()
        sections = conn.execute(
            "SELECT id, title, level, parent_id, length(content) as c FROM sections WHERE paper_id=? ORDER BY id",
            (pid,)
        ).fetchall()
        conn.close()
        
        print(f"  → Paper #{pid}: {len(sections)} sections in tree")
        for s in sections:
            indent = "  " * (s["level"] - 1)
            parent = f" (child of #{s['parent_id']})" if s["parent_id"] else ""
            print(f"    {indent}[L{s['level']}] {s['title'][:60]}{parent}")
        
        paper_ids.append(pid)

    print(f"\nDone! Indexed {len(paper_ids)} papers" +
          (f"，查重拦截 {n_dup} 篇（确认误判可加 --force）" if n_dup else "") + ".")
    return paper_ids


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Index papers into knowledge base (tree structure)")
    parser.add_argument("--dir", "-d", default=str(normalized_dir()),
                        help="Directory containing normalized .md files")
    parser.add_argument("--force", action="store_true",
                        help="查重命中时仍继续导入（须已人工核对非重复；会写审计）")
    args = parser.parse_args()
    index_all(args.dir, force=args.force)
