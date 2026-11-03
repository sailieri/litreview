# -*- coding: utf-8 -*-
"""体检 data/pdfs 里「有文件但没有论文行引用」的孤儿 PDF。

对每个孤儿：
  - 提取首页标题（首页面积最大的字号块）
  - 查库：是否与某篇已有论文标题重复（→ 可能是被查重闸门挡下的重复件）
  - 查 metadata/normalized/extracted 是否还有文本产物（→ 入库过但行被删）
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.paths import data_root, db_path, pdfs_dir  # noqa: E402

PDFS = pdfs_dir()

try:
    import pymupdf
except Exception:
    pymupdf = None


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def stem_of(fp: str) -> str:
    base = (fp or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".md", ".pdf"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


def title_of(path: Path) -> str:
    if pymupdf is None:
        return ""
    try:
        doc = pymupdf.open(str(path))
        page = doc[0]
        blocks = page.get_text("dict")["blocks"]
        best, best_size = "", 0
        for b in blocks:
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    t = sp.get("text", "").strip()
                    if len(t) > 8 and sp.get("size", 0) > best_size:
                        best, best_size = t, sp["size"]
        doc.close()
        return best
    except Exception:
        return ""


def main():
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id,title,doi,file_path FROM papers").fetchall()
    conn.close()
    stems = {stem_of(r["file_path"] or ""): r for r in rows}
    norm_titles = [(norm(r["title"]), r["id"], r["title"]) for r in rows]

    files = sorted(PDFS.glob("*.pdf"))
    orphans = [f for f in files if f.stem not in stems]
    print(f"data/pdfs 共 {len(files)} 个 PDF；其中 {len(orphans)} 个没有论文行引用\n")
    for f in orphans:
        t = title_of(f)
        key = norm(t)
        dup = None
        if key:
            for nt, pid, tt in norm_titles:
                if nt and (nt[:50] == key[:50] or key[:50] in nt or nt[:50] in key):
                    dup = (pid, tt)
                    break
        has = []
        for sub in ("metadata", "normalized", "extracted"):
            if (data_root() / sub / f"{f.stem}.md").exists() or (data_root() / sub / f"{f.stem}.json").exists():
                has.append(sub)
        size = f.stat().st_size // 1024
        print(f"[{f.name[:52]}] {size}KB")
        print(f"   首页标题: {t[:80]}")
        print(f"   文本产物: {has or '无'}")
        print(f"   库内重复: {dup if dup else '无匹配'}")
        print()


if __name__ == "__main__":
    main()
