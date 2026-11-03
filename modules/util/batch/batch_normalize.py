"""批量规范化：原始文本 → 规范 markdown + 元数据（走 step=normalize 的 provider）。

只提供函数，不提供命令行；入口在 `pipelines/build/normalize.py`。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules.paths import extracted_dir, metadata_dir, normalized_dir   # noqa: E402


def run(limit: int | None = None) -> dict:
    """处理 data/extracted 下所有 `*_raw.md`，缺 normalized 或 metadata 的才跑。

    返回 {"total", "todo", "ok", "seconds"}。
    """
    from modules.normalize.normalizer import normalize_paper
    from modules.util.llm_client import LLMClient

    ex_dir, norm_dir, meta_dir = extracted_dir(), normalized_dir(), metadata_dir()
    norm_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    client = LLMClient(step="normalize")

    raw_files = sorted(ex_dir.glob("*_raw.md"))
    todo = []
    for rf in raw_files:
        stem = rf.stem.replace("_raw", "")
        if not (norm_dir / f"{stem}.md").exists() or not (meta_dir / f"{stem}.json").exists():
            todo.append((rf, stem))
    if limit:
        todo = todo[:limit]

    print(f"共 {len(raw_files)} 篇，待规范化: {len(todo)} 篇\n")

    ok = 0
    total_time = 0.0
    for i, (raw_path, stem) in enumerate(todo, 1):
        raw_text = raw_path.read_text(encoding="utf-8")
        print(f"  [{i}/{len(todo)}] {stem[:50]}...", end=" ", flush=True)
        t0 = time.time()
        try:
            meta, body = normalize_paper(raw_text, client=client)
            (meta_dir / f"{stem}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            (norm_dir / f"{stem}.md").write_text(body, encoding="utf-8")
            elapsed = time.time() - t0
            total_time += elapsed
            ok += 1
            print(f"✅ {meta.get('title', '?')[:40]} ({elapsed:.0f}s, {len(body)}ch)")
        except Exception as e:  # noqa: BLE001  单篇失败不阻断整批
            print(f"❌ {e} ({time.time()-t0:.0f}s)")

    print(f"\n完成: {ok}/{len(todo)} 成功，总耗时 {total_time:.0f}s ({total_time/60:.1f}min)")
    print(f"输出: {norm_dir} / {meta_dir}")
    return {"total": len(raw_files), "todo": len(todo), "ok": ok, "seconds": round(total_time)}
