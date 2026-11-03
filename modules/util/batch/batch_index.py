"""
批量索引：拆分 normalized .md 论文 → sections + chunks 入库
不重复插入 papers（已存在的跳过），只追加 sections 和 chunks
"""
import os, sys, re, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.database.schema import get_db, init_db, DB_PATH
from modules.database.indexer import parse_sections, _insert_chunks
from modules.database.embedder import compute_embedding, vector_to_blob, chunk_text
from modules.paths import normalized_dir

def match_paper_id(conn, md_path):
    """匹配 normalized 文件到 papers 表 ID。
    从文件的第一行 `# Title` 提取英文标题，与 papers 表做模糊匹配。
    """
    with open(md_path, encoding="utf-8") as f:
        first_line = f.readline().strip()
    
    # 提取标题（去掉 # 标记）
    title = first_line.replace("# ", "").strip()
    if not title:
        return None
    
    # 模糊匹配：计算词重叠度
    all_papers = conn.execute("SELECT id, title FROM papers").fetchall()
    best_id = None
    best_score = 0
    
    title_words = set(title.lower().split())
    
    for row in all_papers:
        paper_words = set(row["title"].lower().split())
        common = title_words & paper_words
        if len(common) > best_score:
            best_score = len(common)
            best_id = row["id"]
    
    # 需要至少 5 个共同词才算匹配
    if best_score >= 5:
        return best_id
    return None

def index_all_papers(normalized_dir, compute_embeds=True):
    conn = get_db()
    init_db(conn)

    md_files = sorted(glob.glob(os.path.join(normalized_dir, "*.md")))
    print(f"找到 {len(md_files)} 个 normalized 文件")

    total_sections = 0
    total_chunks = 0

    for md_path in md_files:
        fname = os.path.basename(md_path)

        # 匹配 paper_id
        paper_id = match_paper_id(conn, md_path)
        if not paper_id:
            print(f"  ⚠️  [{fname}] 无法匹配 paper → 跳过")
            continue

        # 检查是否已有 sections
        existing = conn.execute(
            "SELECT COUNT(*) as cnt FROM sections WHERE paper_id=?", (paper_id,)
        ).fetchone()["cnt"]
        if existing > 0:
            print(f"  ⏭️  [{fname}] paper #{paper_id}: 已有 {existing} sections → 跳过")
            continue

        # 读取 normalized 文本
        with open(md_path, encoding="utf-8") as f:
            text = f.read()

        # 解析 sections
        raw_sections = parse_sections(text)

        # 合并 Overview sections
        merged_sections = []
        overview_parts = []
        body_start = None
        for i, sec in enumerate(raw_sections):
            title_lower = sec["title"].strip().lower()
            is_body = (sec["level"] >= 2 and (
                title_lower[0].isdigit() or
                title_lower.startswith("introduction") or
                title_lower.startswith("materials") or
                title_lower.startswith("methods") or
                title_lower.startswith("results") or
                title_lower.startswith("discussion") or
                title_lower.startswith("conclusion") or
                title_lower.startswith("experiment") or
                title_lower.startswith("data")
            ))
            if not is_body and body_start is None:
                if sec["content"].strip():
                    overview_parts.append(sec["content"].strip())
            else:
                if body_start is None:
                    overview_text = "\n\n".join(overview_parts) if overview_parts else ""
                    merged_sections.append({
                        "title": "Overview",
                        "level": 1,
                        "parent_id_ref": None,
                        "content": overview_text,
                    })
                    body_start = i
                merged_sections.append(sec)

        if body_start is None:
            overview_text = "\n\n".join(overview_parts) if overview_parts else ""
            merged_sections = [{
                "title": "Overview",
                "level": 1,
                "parent_id_ref": None,
                "content": overview_text,
            }]

        # 插入 sections 和 chunks
        section_id_map = {}
        paper_sections = 0
        paper_chunks = 0
        for i, sec in enumerate(merged_sections):
            parent_id = section_id_map.get(sec["parent_id_ref"]) if sec.get("parent_id_ref") is not None else None

            cur = conn.execute(
                "INSERT INTO sections (paper_id, title, level, parent_id, content) VALUES (?, ?, ?, ?, ?)",
                (paper_id, sec["title"], sec["level"], parent_id, sec["content"]),
            )
            section_id = cur.lastrowid
            section_id_map[i] = section_id
            paper_sections += 1

            # 创建 chunks
            chunks = chunk_text(sec["content"])
            for ci, chunk_content in enumerate(chunks):
                cur2 = conn.execute(
                    "INSERT INTO chunks (section_id, paper_id, content, chunk_index) VALUES (?, ?, ?, ?)",
                    (section_id, paper_id, chunk_content, ci),
                )
                # chunks_fts 是**无触发器的独立 FTS5 表**，必须显式内联写入
                # （rowid = chunks.id，检索端靠 `JOIN chunks c ON c.id=f.rowid`）。
                # 漏写会导致走 batch_index 入库的 chunk 完全搜不到。
                conn.execute("INSERT OR REPLACE INTO chunks_fts(rowid, content) VALUES (?, ?)",
                             (cur2.lastrowid, chunk_content))
                paper_chunks += 1

        conn.commit()
        total_sections += paper_sections
        total_chunks += paper_chunks
        print(f"  ✅ [{fname}] paper #{paper_id}: {paper_sections} sections, {paper_chunks} chunks")

    conn.close()
    print(f"\n总计: {len(md_files)} 篇论文, {total_sections} sections, {total_chunks} chunks")
    return total_chunks

# 命令行入口见 pipelines/build/index.py
