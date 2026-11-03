"""为 data/normalized 下缺少 metadata 的论文补齐元数据提取（从 extracted 原文，LLM 单次调用/篇）。

说明：元数据提取用 extracted 原文（含作者/期刊/DOI）——normalized 文本在规范化时
已剥离作者信息，无法提供 authors/journal/doi。

用法：
  python tools/maintain/backfill_metadata.py                # 全量（跳过已存在）
  python tools/maintain/backfill_metadata.py --stems "A,B"  # 指定篇
  python tools/maintain/backfill_metadata.py --limit 1      # 前 N 篇调试
"""
import json
import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.normalize.normalizer import extract_metadata
from modules.util.dedup_gate import DuplicateError
from modules.util.llm_client import LLMClient

from modules.paths import extracted_dir, metadata_dir, normalized_dir  # noqa: E402
NORM_DIR = normalized_dir()
EXT_DIR = extracted_dir()
META_DIR = metadata_dir()


def process_one(md_path: Path, client, force: bool = False) -> tuple[str, str, float, str]:
    stem = Path(md_path).stem
    meta_path = META_DIR / f"{stem}.json"
    if meta_path.exists():
        return stem, "skip", 0.0, ""
    # 优先 extracted 原文（含作者/期刊/DOI），缺失则回退 normalized
    src = EXT_DIR / f"{stem}_raw.md"
    if not src.exists():
        src = md_path
    text = src.read_text(encoding="utf-8", errors="replace")
    t0 = time.time()
    try:
        meta = extract_metadata(text, client=client, stem=stem, force=force)
    except DuplicateError as e:  # 查重拦截：不写 json，报告交给 main 打印
        return stem, "dup", time.time() - t0, str(e)
    except Exception as e:
        return stem, f"EXC {e}", time.time() - t0, ""
    dt = time.time() - t0
    if not meta.get("title") or not meta.get("year"):
        return stem, f"LOW_QUALITY title={meta.get('title')!r} year={meta.get('year')}", dt, ""
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return stem, "ok", dt, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stems", default="")
    ap.add_argument("--force", action="store_true",
                    help="查重命中时仍继续提取（须已人工核对非重复；会写审计）")
    args = ap.parse_args()

    META_DIR.mkdir(parents=True, exist_ok=True)
    papers = sorted(NORM_DIR.glob("*.md"))
    if args.stems:
        wanted = set(args.stems.split(","))
        papers = [p for p in papers if Path(p).stem in wanted]
    if args.limit:
        papers = papers[: args.limit]

    todo = [p for p in papers if not (META_DIR / f"{Path(p).stem}.json").exists()]
    print(f"共 {len(papers)} 篇, 已有 {len(papers) - len(todo)}, 待提取 {len(todo)}, workers={args.workers}")

    client = LLMClient(step="normalize")
    ok = fail = dup = 0
    t_all = time.time()
    for i in range(0, len(todo), args.workers):
        batch = todo[i : i + args.workers]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_one, p, client, args.force): p for p in batch}
            for fut in as_completed(futs):
                stem, status, dt, detail = fut.result()
                if status == "ok":
                    ok += 1
                    print(f"  OK {stem[:60]} ({dt:.0f}s)")
                elif status == "skip":
                    pass
                elif status == "dup":
                    dup += 1
                    print(f"\n[查重拦截] {stem[:60]}\n{detail}\n")
                else:
                    fail += 1
                    print(f"  !! {stem[:60]}: {status}")
        print(f"  [进度] {min(i + args.workers, len(todo))}/{len(todo)} "
              f"ok={ok} fail={fail} 拦截={dup}")

    print(f"完成: ok={ok} fail={fail} 查重拦截={dup} 总耗时 {time.time() - t_all:.0f}s")
    if dup:
        print("  上述拦截若确认为误判：加 --force 重跑（或单篇用 ingest_paper --force）")
    print(f"输出: {META_DIR}")


if __name__ == "__main__":
    main()
