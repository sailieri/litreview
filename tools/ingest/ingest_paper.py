"""单篇论文入库（通用）：PDF → extracted → normalized → papers+sections+chunks → chunks_fts → embeddings。

可选项：用 --meta 传入已知精确元数据 JSON（避免依赖 LLM 抽取，保证 papers 表引文准确）。
入库前自动对 papers.db 做一次时间戳备份（.bak_pre_ingest_<stem>_<ts>）。

用法：
  python tools/ingest/ingest_paper.py path/to/paper.pdf
  python tools/ingest/ingest_paper.py paper.pdf --stem My2024_Keyword --meta meta.json
"""
import os, sys, json, shutil, time, argparse
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.normalize.extractor import extract_pdf
from modules.normalize.normalizer import normalize_text
from modules.database.indexer import index_paper
from modules.database.schema import backup_db  # noqa: E402
from modules.database.schema import get_db, init_db, DB_PATH
from modules.database.embedder import compute_embedding, vector_to_blob
from modules.ingest_status import set_stage
from modules.util.dedup_gate import (DuplicateError, audit_forced, check_duplicate,
                                     first_doi, format_report, heuristic_title)


def clean_raw(raw: str) -> str:
    """去掉 pymupdf 的 --- Page N --- 标记，其余保留。"""
    lines = []
    for ln in raw.splitlines():
        if ln.strip().startswith("--- Page ") and ln.strip().endswith("---"):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="PDF 文件路径")
    ap.add_argument("--stem", default=None,
                    help="入库 STEM 名（默认取 PDF 文件名去扩展名）")
    ap.add_argument("--meta", default=None,
                    help="已知元数据 JSON 路径（可选；含 title/authors/year/journal/doi）")
    ap.add_argument("--force", action="store_true",
                    help="查重命中时仍继续导入（须已人工核对非重复；会写审计）")
    ap.add_argument("--no-backup", action="store_true",
                    help="跳过整库备份（批量导入时由外层统一备份一次，避免 N 份 280MB 副本）")
    ap.add_argument("--no-llm", action="store_true",
                    help="LLM 不可达：跳过 LLM 规范化，直接用清洗原文")
    ap.add_argument("--no-embed", action="store_true",
                    help="embedding 不可达：跳过向量计算，后续可补齐")
    args = ap.parse_args()

    src_pdf = Path(args.pdf).resolve()
    if not src_pdf.exists():
        print(f"[ERR] 找不到 PDF: {src_pdf}")
        return
    stem = args.stem or src_pdf.stem

    from modules.paths import extracted_dir, metadata_dir, normalized_dir, pdfs_dir
    PDF_DIR = pdfs_dir()
    EXT_DIR = extracted_dir()
    NORM_DIR = normalized_dir()
    META_DIR = metadata_dir()

    known_meta = {}
    if args.meta:
        known_meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))

    t0 = time.time()
    # 注意：DB 备份放在查重闸门之后、真正写库之前——被拦截的尝试不必先拷一份整库

    # 1. 复制 PDF
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    dst_pdf = PDF_DIR / f"{stem}.pdf"
    if not dst_pdf.exists():
        shutil.copy2(src_pdf, dst_pdf)
        print(f"[pdf] 复制到 {dst_pdf}")
    else:
        print(f"[pdf] 已存在，跳过复制: {dst_pdf}")

    # 2. 提取
    EXT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = EXT_DIR / f"{stem}_raw.md"
    if not raw_path.exists():
        res = extract_pdf(str(dst_pdf), str(EXT_DIR))
        raw_text = res["full_text"]
        print(f"[extract] {len(raw_text)} 字符 → {raw_path}")
    else:
        raw_text = raw_path.read_text(encoding="utf-8")
        print(f"[extract] 已存在，读取 {len(raw_text)} 字符")

    # ── 查重闸门·元数据线（在第一次 LLM 花钱之前，免费）──────────
    def _pre_gate(title, doi, authors=""):
        hits = check_duplicate(title=title, doi=doi, stem=stem, authors=authors,
                               raw_text=raw_text, exclude_stem=stem)
        if not hits:
            return
        cur = {"title": title or "", "doi": doi or "", "stem": stem}
        ctx = f"ingest_paper · 提取前(stem={stem})"
        if args.force:
            audit_forced(ctx, cur, hits, reason="force 放行 ingest_paper")
            print(format_report(hits, ctx + " [force 已放行]", cur))
            return
        print(format_report(hits, ctx, cur))
        sys.exit(2)

    if known_meta.get("title") or known_meta.get("doi"):
        _pre_gate(known_meta.get("title"), known_meta.get("doi"),
                  known_meta.get("authors", ""))       # 有已知元数据：用精确值查
    else:
        _pre_gate(heuristic_title(raw_text), first_doi(raw_text))  # 无：启发式+首页 DOI

    # 3. 规范化（LLM 优先，失败回退清洗原文）
    NORM_DIR.mkdir(parents=True, exist_ok=True)
    norm_path = NORM_DIR / f"{stem}.md"
    if not norm_path.exists():
        body = None
        if args.no_llm:
            print("[normalize] --no-llm：跳过 LLM，使用清洗原文")
            body = clean_raw(raw_text)
            norm_path.write_text(body, encoding="utf-8")
        else:
            try:
                from modules.util.llm_client import LLMClient
                client = LLMClient(step="normalize")
                body = normalize_text(raw_text, client=client)
                print(f"[normalize] LLM 成功，{len(body)} 字符")
            except Exception as e:
                print(f"[normalize] LLM 失败({e})，回退到清洗原文")
                body = clean_raw(raw_text)
            norm_path.write_text(body, encoding="utf-8")
    else:
        body = norm_path.read_text(encoding="utf-8")
        print(f"[normalize] 已存在，读取 {len(body)} 字符")

    # 4. 元数据（LLM 抽取；--meta 给的已知值优先覆盖）
    # 此前只做规范化不抽元数据 → 标题回退成文件名、年份为空，参考文献题录全废。
    META_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = META_DIR / f"{stem}.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    if known_meta:
        meta = {**meta, **known_meta}
        print(f"[metadata] 使用已知元数据：{known_meta.get('title', stem)}")
    if not meta.get("title") and not args.no_llm:
        try:
            from modules.normalize.normalizer import extract_metadata
            from modules.util.llm_client import LLMClient
            meta = extract_metadata(raw_text, client=LLMClient(step="metadata"),
                                    stem=stem, force=args.force)
            if known_meta:
                meta = {**meta, **known_meta}
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            print(f"[metadata] 抽取完成：{meta.get('title', '?')[:60]} "
                  f"({meta.get('year', '年份未知')})")
        except Exception as e:  # noqa: BLE001  抽取失败不阻断入库
            print(f"[metadata] 抽取失败({e})，标题将回退为文件名；可事后跑 "
                  f"tools/maintain/backfill_metadata.py 补齐")
    elif meta.get("title"):
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # 5. 索引入库（papers + sections + chunks；数据库线查重闸门在 index_paper 内）
    if not args.no_backup:
        bak = backup_db(f"ingest_{stem}")
        print(f"[backup] {bak}" if bak else "[backup] 跳过（库还不存在，首次建库）")
    summary = {"metadata": meta}
    try:
        paper_id = index_paper(stem, body, summary=summary, file_path=str(dst_pdf),
                               force=args.force)
    except DuplicateError as e:
        print(str(e))
        sys.exit(2)
    print(f"[index] paper_id={paper_id}")

    # 6. 补 chunks_fts（内联 FTS5，必须显式插入 rowid=chunk id）
    conn = get_db()
    init_db(conn)
    new_chunks = conn.execute(
        "SELECT id, content FROM chunks WHERE paper_id=?", (paper_id,)
    ).fetchall()
    inserted = 0
    for cid, content in new_chunks:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO chunks_fts(rowid, content) VALUES (?, ?)",
                (cid, content),
            )
            inserted += 1
        except Exception as e:
            print(f"  [fts] chunk {cid} 插入失败: {e}")
    conn.commit()
    print(f"[fts] chunks_fts 插入 {inserted}/{len(new_chunks)}")

    # 7. 向量化新 chunks（只处理本论文的 NULL 向量）
    to_embed = conn.execute(
        "SELECT id, content FROM chunks WHERE paper_id=? AND vector IS NULL",
        (paper_id,)
    ).fetchall()
    emb_ok = 0
    if args.no_embed:
        print(f"[embed] --no-embed：跳过向量计算（{len(to_embed)} 个 chunk 待补）")
    else:
        for cid, content in to_embed:
            vec = compute_embedding(content)
            if vec:
                conn.execute("UPDATE chunks SET vector=? WHERE id=?",
                             (vector_to_blob(vec), cid))
                emb_ok += 1
        conn.commit()
        print(f"[embed] 向量化 {emb_ok}/{len(to_embed)}")

    # 记录入库状态（ingest_status）
    # 注意：paper_id 到第 5 步才产生，故在此一次性回填前面已完成的阶段。
    try:
        set_stage(paper_id, conn=conn, extracted=True, normalized=True,
                  metadata_done=True, indexed=True)
        print(f"[status] ingest_status 已更新 (paper_id={paper_id})")
    except Exception as e:  # noqa: BLE001
        print(f"[status] ingest_status 写入失败（不阻断入库）: {e}")

    conn.close()

    print(f"\n完成。耗时 {time.time()-t0:.1f}s。paper_id={paper_id}，"
          f"新增 chunks={len(new_chunks)}。")


if __name__ == "__main__":
    main()
