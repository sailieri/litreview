# -*- coding: utf-8 -*-
"""批量把外部已整理好的论文并入主库（源头目录由 LITREVIEW_SRC 或 --src 指定）。

前提：pdfs/extracted/normalized/metadata 已拷进主库对应目录。
本脚本逐篇调 tools/ingest/ingest_paper.py（复用已存在文件，不做规范化/不花 LLM），
由 ingest_paper 负责 papers+sections+chunks+fts+向量+ingest_status。

用法：
  cd <仓库根>
  LITREVIEW_DATA=<仓库根>/data python3 tools/ingest/batch_ingest.py --dry
  LITREVIEW_DATA=<仓库根>/data python3 tools/ingest/batch_ingest.py

--dry：只跑查重闸门，列出会命中重复的篇目，不写库。
"""
import os, sys, json, subprocess, argparse, time
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SRC = Path(os.environ.get('LITREVIEW_SRC') or (ROOT / 'data'))
from modules.paths import data_root, extracted_dir  # noqa: E402
DATA = data_root()


def stems():
    pdfs = {p.stem for p in (SRC / 'pdfs').glob('*.pdf')}
    metas = {p.stem for p in (SRC / 'metadata').glob('*.json')}
    norms = {p.stem for p in (SRC / 'normalized').glob('*.md')}
    # 三处都有才算可导
    return sorted(pdfs & metas & norms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()

    from modules.util.dedup_gate import check_duplicate, format_report

    ss = stems()
    if a.limit:
        ss = ss[:a.limit]
    print(f'候选 {len(ss)} 篇  dry={a.dry}', flush=True)

    ok = skip = err = 0
    t0 = time.time()
    for i, stem in enumerate(ss, 1):
        meta_p = SRC / 'metadata' / f'{stem}.json'
        pdf_p = SRC / 'pdfs' / f'{stem}.pdf'
        meta = json.loads(meta_p.read_text(encoding='utf-8'))
        raw_p = extracted_dir() / f'{stem}_raw.md'
        raw = raw_p.read_text(encoding='utf-8', errors='replace') if raw_p.exists() else ''

        hits = check_duplicate(title=meta.get('title', ''), doi=meta.get('doi', ''),
                               stem=stem, authors=meta.get('authors', ''),
                               raw_text=raw, exclude_stem=stem)
        if hits:
            skip += 1
            print(f'[{i}/{len(ss)}] DUP  {stem}  -> {format_report(hits, "dry", {"title": meta.get("title","")})[:300]}', flush=True)
            continue
        if a.dry:
            ok += 1
            print(f'[{i}/{len(ss)}] OK   {stem}', flush=True)
            continue

        cmd = [sys.executable, str(ROOT / 'tools' / 'ingest' / 'ingest_paper.py'), str(pdf_p),
               '--stem', stem, '--meta', str(meta_p), '--no-backup']
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if r.returncode == 0:
            ok += 1
            tail = [l for l in r.stdout.splitlines() if l.startswith('[index]') or l.startswith('[embed]') or l.startswith('[status]')]
            print(f'[{i}/{len(ss)}] ok   {stem} | {" ; ".join(tail)[:160]} | {ok}ok/{skip}skip/{err}err | {time.time()-t0:.0f}s', flush=True)
        elif r.returncode == 2:
            skip += 1
            print(f'[{i}/{len(ss)}] DUP2 {stem} | {ok}ok/{skip}skip/{err}err | {time.time()-t0:.0f}s', flush=True)
        else:
            err += 1
            print(f'[{i}/{len(ss)}] err  {stem} rc={r.returncode} | {r.stderr.strip()[-300:]}', flush=True)

    print(f'\n=== 完成 ok={ok} skip={skip} err={err} 用时 {time.time()-t0:.0f}s ===', flush=True)


if __name__ == '__main__':
    main()
