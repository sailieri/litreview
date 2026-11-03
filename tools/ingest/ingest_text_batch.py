# -*- coding: utf-8 -*-
"""仅文本入库 / 单篇重建索引（不需要 PDF）。

用途：
  1) 批量孤儿文本——normalized 文本与 metadata 齐全，但 papers 里没有记录；
     它们没有 PDF，ingest_paper.py 走不通。
  2) 个别论文入库不完整（文本先入库、之后源文件又被更新），需要按现有 normalized 重建。

流程：papers 元数据 → sections（含 parent 层级）→ chunks → chunks_fts → 向量 → ingest_status。

用法：
  cd <仓库根>
  LITREVIEW_DATA=<仓库根>/data \
  LITREVIEW_EMBED_URL=<embed_endpoint> \
  python3 tools/ingest/ingest_text_batch.py --orphans --dry
  ... 去掉 --dry 正式跑；--stem a,b,c 指定单篇（可重建已有记录）
"""
import os, sys, json, time, argparse
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules.database.schema import get_db, init_db
from modules.database.indexer import parse_sections, _insert_chunks
from modules.database.embedder import compute_embedding, vector_to_blob
from modules.ingest_status import set_stage
from modules.util.dedup_gate import check_duplicate, format_report

from modules.paths import data_root, extracted_dir, metadata_dir, normalized_dir  # noqa: E402
DATA = data_root()
NORM = normalized_dir()
META = metadata_dir()
EXT = extracted_dir()
LITLIB = Path(os.environ['LITREVIEW_LITLIB']) if os.environ.get('LITREVIEW_LITLIB') else None


def find_orphans(conn):
    rows = conn.execute("SELECT id, file_path FROM papers").fetchall()
    known = set()
    for _pid, fp in rows:
        if fp:
            known.add(os.path.splitext(os.path.basename(str(fp).replace('\\', '/')))[0].replace('_raw', ''))
    lit = {p.stem for p in LITLIB.glob('*.md')} if LITLIB and LITLIB.exists() else set()
    out = []
    for p in sorted(NORM.glob('*.md')):
        if p.stem in known or p.stem in lit:
            continue
        out.append(p.stem)
    return out


def find_pid(conn, stem):
    rows = conn.execute("SELECT id, file_path FROM papers").fetchall()
    for pid, fp in rows:
        if fp and os.path.splitext(os.path.basename(str(fp).replace('\\', '/')))[0].replace('_raw', '') == stem:
            return pid
    return None


def index_one(conn, stem, dry=False, force=False):
    norm_p = NORM / f'{stem}.md'
    text = norm_p.read_text(encoding='utf-8', errors='replace')
    meta = {}
    mp = META / f'{stem}.json'
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding='utf-8'))
        except Exception:
            meta = {}
    raw_exists = (EXT / f'{stem}_raw.md').exists()
    pdf_p = DATA / 'pdfs' / f'{stem}.pdf'
    # file_path 优先指向 PDF（有则用），否则指向 normalized 文本
    src_path = str(pdf_p) if pdf_p.exists() else str(norm_p)

    pid = find_pid(conn, stem)
    # 查重：已有记录是自己，不拦；新增才查
    if pid is None and not force:
        hits = check_duplicate(title=meta.get('title', '') or stem, doi=meta.get('doi', ''),
                               stem=stem, authors=meta.get('authors', ''), exclude_stem=stem)
        if hits:
            return ('dup', None, format_report(hits, 'ingest_text', {'title': meta.get('title', '')}))

    n_chars = len(text)
    if dry:
        return ('dry', pid, f'{n_chars} 字符, meta={"Y" if meta else "-"}, raw={"Y" if raw_exists else "-"}')

    # 1) papers 行
    if pid is None:
        cur = conn.execute(
            """INSERT INTO papers (title, authors, year, journal, doi, file_path)
               VALUES (?,?,?,?,?,?)""",
            (meta.get('title') or stem, meta.get('authors', '') or '', meta.get('year'),
             meta.get('journal', '') or '', meta.get('doi', '') or '', src_path))
        pid = cur.lastrowid
    else:
        conn.execute(
            """UPDATE papers SET title=coalesce(nullif(?,''),title), authors=coalesce(nullif(?,''),authors),
                   year=coalesce(?,year), journal=coalesce(nullif(?,''),journal),
                   doi=coalesce(nullif(?,''),doi), file_path=? WHERE id=?""",
            (meta.get('title', '') or '', meta.get('authors', '') or '', meta.get('year'),
             meta.get('journal', '') or '', meta.get('doi', '') or '', src_path, pid))
        # 清旧（chunks_fts 用 rowid 索引，先取出）
        old = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE paper_id=?", (pid,))]
        for cid in old:
            conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
        conn.execute("DELETE FROM chunks WHERE paper_id=?", (pid,))
        conn.execute("DELETE FROM sections WHERE paper_id=?", (pid,))

    # 2) sections + chunks
    secs = parse_sections(text)
    id_map = {}
    for i, sec in enumerate(secs):
        parent = None
        for j in range(i - 1, -1, -1):
            if secs[j]['level'] == sec['level'] - 1:
                parent = id_map[j]
                break
            if secs[j]['level'] < sec['level'] - 1:
                break
        cur = conn.execute(
            "INSERT INTO sections (paper_id, title, level, parent_id, content) VALUES (?,?,?,?,?)",
            (pid, sec['title'], sec['level'], parent, sec['content']))
        id_map[i] = cur.lastrowid
        _insert_chunks(conn, pid, cur.lastrowid, sec['content'])
    conn.commit()

    # 3) chunks_fts
    rows = conn.execute("SELECT id, content FROM chunks WHERE paper_id=?", (pid,)).fetchall()
    for cid, content in rows:
        conn.execute("INSERT OR REPLACE INTO chunks_fts(rowid, content) VALUES (?,?)", (cid, content))
    conn.commit()

    # 4) 向量
    todo = conn.execute("SELECT id, content FROM chunks WHERE paper_id=? AND vector IS NULL", (pid,)).fetchall()
    emb = 0
    for cid, content in todo:
        vec = compute_embedding(content)
        if vec:
            conn.execute("UPDATE chunks SET vector=? WHERE id=?", (vector_to_blob(vec), cid))
            emb += 1
    conn.commit()

    # 5) 状态
    try:
        set_stage(pid, conn=conn, extracted=raw_exists, normalized=True,
                  metadata_done=bool(meta), indexed=True)
        conn.commit()
    except Exception:
        pass

    return ('ok', pid, f'sections={len(secs)} chunks={len(rows)} emb={emb}/{len(todo)}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--orphans', action='store_true', help='自动扫描未入库的孤儿文本')
    ap.add_argument('--stem', default='', help='逗号分隔的 stem 列表（可重建已有记录）')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--force', action='store_true', help='查重命中时仍入库（用于同批重复对的择一保留）')
    a = ap.parse_args()

    conn = get_db(); init_db(conn)
    if a.stem:
        stems = [s.strip() for s in a.stem.split(',') if s.strip()]
    elif a.orphans:
        stems = find_orphans(conn)
    else:
        print('需要 --orphans 或 --stem'); return
    if a.limit:
        stems = stems[:a.limit]
    print(f'待处理 {len(stems)} 篇  dry={a.dry}', flush=True)

    ok = dup = err = 0
    t0 = time.time()
    for i, stem in enumerate(stems, 1):
        try:
            st, pid, info = index_one(conn, stem, dry=a.dry, force=a.force)
        except Exception as e:
            st, pid, info = 'err', None, f'{type(e).__name__}: {e}'
        if st == 'ok': ok += 1
        elif st == 'dup': dup += 1
        elif st == 'err': err += 1
        print(f'[{i}/{len(stems)}] {st:4s} pid={pid} | {stem[:58]} | {str(info)[:150]} | {ok}ok/{dup}dup/{err}err | {time.time()-t0:.0f}s', flush=True)
    print(f'\n=== 完成 ok={ok} dup={dup} err={err} 用时 {time.time()-t0:.0f}s ===', flush=True)


if __name__ == '__main__':
    main()
