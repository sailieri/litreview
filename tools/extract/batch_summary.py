# -*- coding: utf-8 -*-
"""批量生成论文叙事摘要（不含超图提取）。

对应 modules/hypergraph/summary.py::run_summary —— 该函数是解耦的摘要生成器
（Turn1 ONLY，不产 entities/hyperedges），走 config.yaml 的 summary step（本地部署的推理服务）。

用法：
  LITREVIEW_DATA=/path/to/data \
  python3 tools/extract/batch_summary.py [--workers 4] [--limit N]
产物：data/summaries/<stem>.json  {"narrative": "..."}，并置 ingest_status.summarized=1
"""
import os, sys, json, time, argparse, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules.paths import normalized_dir, summaries_dir, metadata_dir
from modules.hypergraph.summary import run_summary
from modules.database.schema import get_db

NORM = normalized_dir()
SUM = summaries_dir()
META = metadata_dir()
SUM.mkdir(parents=True, exist_ok=True)


def paper_id_map(conn):
    """stem -> paper_id 映射（一次建好，避免每篇都全表遍历）。"""
    m = {}
    for pid, fp in conn.execute("SELECT id, file_path FROM papers"):
        if not fp:
            continue
        base = str(fp).replace('\\', '/').rsplit('/', 1)[-1]
        for suf in ('.md', '.pdf'):
            if base.endswith(suf):
                base = base[:-len(suf)]
        m.setdefault(base, pid)
    return m


def paper_id_for_stem(conn, stem):
    return paper_id_map(conn).get(stem)


def do_one(stem):
    """返回 (stem, status, 耗时, narrative)。

    注意：skip 分支也必须把 narrative 带回主线程统一回填 DB，
    否则摘要只落在 json 文件里，papers.summary 永远为空。
    """
    out = SUM / f'{stem}.json'
    if out.exists():
        try:
            d = json.loads(out.read_text(encoding='utf-8'))
            txt = (d.get('narrative') or '').strip()
            if txt:
                return (stem, 'skip', 0, txt)
        except Exception:
            pass
    meta = None
    mp = META / f'{stem}.json'
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding='utf-8'))
        except Exception:
            meta = None
    t0 = time.time()
    r = run_summary(str(NORM / f'{stem}.md'), metadata=meta)
    narrative = (r or {}).get('narrative', '')
    if not narrative.strip():
        return (stem, 'empty', time.time() - t0, '')
    out.write_text(json.dumps({'narrative': narrative}, ensure_ascii=False, indent=2), encoding='utf-8')
    return (stem, 'ok', time.time() - t0, narrative)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--stem', default='', help='只处理指定 stem（单篇入库用）')
    a = ap.parse_args()

    if a.stem:
        stems = [a.stem]
    else:
        stems = sorted(p.stem for p in NORM.glob('*.md'))
    if a.limit:
        stems = stems[:a.limit]
    print(f'共 {len(stems)} 篇，workers={a.workers}', flush=True)

    conn = get_db()
    pid_of = paper_id_map(conn)
    t0 = time.time(); ok = skip = err = filled = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(do_one, s): s for s in stems}
        for i, fu in enumerate(as_completed(futs), 1):
            stem = futs[fu]
            try:
                stem, st, dt, narrative = fu.result()
            except Exception as e:
                st, dt, narrative = f'err-{type(e).__name__}', 0, ''
                print('   ', traceback.format_exc(limit=2)[-200:], flush=True)
            pid = pid_of.get(stem)
            if st in ('ok', 'skip') and narrative:
                # 注意：叙事摘要必须回填 papers.summary（只填空值，可重复执行）
                if pid:
                    try:
                        cur = conn.execute(
                            "UPDATE papers SET summary=? WHERE id=? "
                            "AND (summary IS NULL OR TRIM(summary)='')", (narrative, pid))
                        filled += cur.rowcount
                        conn.execute("INSERT OR IGNORE INTO ingest_status (paper_id) VALUES (?)", (pid,))
                        conn.execute("UPDATE ingest_status SET summarized=1, "
                                     "updated_at=datetime('now','localtime') WHERE paper_id=?", (pid,))
                        conn.commit()
                    except Exception:
                        conn.rollback()
            if st == 'ok':
                ok += 1
            elif st == 'skip':
                skip += 1
            else:
                err += 1
            print(f'[{i}/{len(stems)}] {st:14s} {dt:6.1f}s | {stem[:56]} | {ok}ok/{skip}skip/{err}err | db+{filled} | {time.time()-t0:.0f}s', flush=True)
    print(f'\n=== 完成 ok={ok} skip={skip} err={err} 回填DB={filled} 用时 {time.time()-t0:.0f}s ===', flush=True)


if __name__ == '__main__':
    main()
