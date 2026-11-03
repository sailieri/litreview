# -*- coding: utf-8 -*-
"""批量入库「孤儿 PDF」（data/pdfs 里有文件、papers 表里没行的那些新文献）。

流程（逐篇按 extract → normalize → index → 向量）：
  1) extract_pdf → data/extracted/<stem>_raw.md
  2) extract_metadata（LLM，失败回退正则抽 DOI + 首页标题）→ data/metadata/<stem>.json
  3) 调 tools/ingest/ingest_paper.py（--meta 传入已知元数据）→ papers 行 + sections + chunks + FTS + 向量
  4) 可选 --summary / --hyper：跑叙事摘要与超图提取

用法：
  python tools/maintain/orphans.py ingest --list            # 列出待入库的孤儿
  python tools/maintain/orphans.py ingest --only <stem>      # 单篇
  python tools/maintain/orphans.py ingest --all              # 全部
  python tools/maintain/orphans.py ingest --all --summary --hyper
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from modules.paths import pdfs_dir, normalized_dir, metadata_dir, db_path, extracted_dir  # noqa: E402


def known_stems():
    import sqlite3
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    stems = set()
    for r in conn.execute("SELECT file_path FROM papers"):
        fp = (r["file_path"] or "").replace("\\", "/").rsplit("/", 1)[-1]
        for suf in (".md", ".pdf"):
            if fp.endswith(suf):
                fp = fp[: -len(suf)]
        stems.add(fp)
    conn.close()
    return stems


def orphans():
    ks = known_stems()
    return sorted(p for p in pdfs_dir().glob("*.pdf") if p.stem not in ks)


def fallback_meta(raw: str):
    m = re.findall(r"10\.\d{4,9}/[^\s\"'<>,;)]+", raw[:6000])
    doi = m[0].rstrip(".") if m else ""
    lines = [ln.strip() for ln in raw.splitlines() if len(ln.strip()) > 20]
    title = lines[0][:200] if lines else ""
    year = ""
    ym = re.findall(r"\b(19|20)\d{2}\b", raw[:3000])
    if ym:
        year = re.findall(r"\b(?:19|20)\d{2}\b", raw[:3000])[0]
    return {"title": title, "authors": "", "year": year, "journal": "", "doi": doi}


def build_meta(pdf: Path, stem: str):
    from modules.normalize.extractor import extract_pdf
    from modules.normalize.normalizer import extract_metadata
    from modules.util.llm_client import LLMClient

    ext = extracted_dir()
    ext.mkdir(parents=True, exist_ok=True)
    raw_path = ext / f"{stem}_raw.md"
    if raw_path.exists():
        raw = raw_path.read_text(encoding="utf-8", errors="replace")
    else:
        res = extract_pdf(str(pdf), str(ext))
        raw = res["full_text"]
    meta = {}
    try:
        client = LLMClient(step="metadata")
        meta = extract_metadata(raw, client=client) or {}
        print(f"    [meta-LLM] ok: {str(meta.get('title'))[:60]}")
    except Exception as e:
        print(f"    [meta-LLM] 失败({type(e).__name__}:{e})，用正则兜底")
        meta = {}
    if not meta.get("title") or not meta.get("doi"):
        fb = fallback_meta(raw)
        for k, v in fb.items():
            if not meta.get(k):
                meta[k] = v
    meta.setdefault("authors", "")
    meta.setdefault("year", "")
    meta.setdefault("journal", "")
    meta.setdefault("doi", "")
    mp = metadata_dir() / f"{stem}.json"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--hyper", action="store_true")
    args = ap.parse_args()

    orph = orphans()
    if args.only:
        orph = [p for p in orph if p.stem == args.only]
    print(f"[plan] 孤儿 PDF {len(orph)} 篇")
    for p in orph:
        print(f"   - {p.name}")

    if args.list or not (args.all or args.only):
        return

    for pdf in orph:
        stem = pdf.stem
        t0 = time.time()
        print(f"\n=== 入库 {pdf.name} ===")
        meta = build_meta(pdf, stem)
        print(f"    [meta] {meta.get('title')} | {meta.get('year')} | {meta.get('journal')} | {meta.get('doi')}")
        cmd = [sys.executable, str(ROOT / "tools" / "ingest" / "ingest_paper.py"), str(pdf),
               "--meta", str(metadata_dir() / f"{stem}.json"), "--no-backup"]
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        for ln in out.splitlines():
            if any(k in ln for k in ("[pdf]", "[extract]", "[metadata]", "[index]",
                                     "[fts]", "[embed]", "[status]", "[backup]", "查重", "ERR")):
                print("   ", ln)
        if r.returncode != 0:
            print(f"    [FAIL] ingest_paper 退出码 {r.returncode}")
            continue
        print(f"    [done] {time.time()-t0:.0f}s")

    if args.summary:
        print("\n=== 批量摘要 ===")
        subprocess.run([sys.executable, str(ROOT / "tools" / "extract" / "batch_summary.py")],
                       cwd=str(ROOT))
    if args.hyper:
        print("\n=== 超图提取 ===")
        for pdf in orph:
            stem = pdf.stem
            cmd = [sys.executable, str(ROOT / "tools" / "extract" / "extract_one.py"), stem]
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (r.stderr or "")
            print(f"--- {stem}")
            for ln in out.splitlines()[-12:]:
                print("   ", ln)


if __name__ == "__main__":
    main()
