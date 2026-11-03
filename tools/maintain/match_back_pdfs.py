# -*- coding: utf-8 -*-
"""核对 data/back 里用户手动下的 PDF 与缺失清单的匹配情况。

按优先级匹配：
  1) 文件名 == 目标 stem
  2) 文件名含 DOI
  3) PDF 首页文本含该篇 DOI
  4) PDF 首页文本含标题关键词（归一化后前 60 字符子串）

输出：匹配表 + 建议拷贝映射（back/<file> → data/pdfs/<stem>.pdf）
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.paths import data_root, pdfs_dir  # noqa: E402
DATA = data_root()
BACK = DATA / "back"
PDFS = pdfs_dir()
sys.path.insert(0, str(ROOT))
from modules.paths import db_path  # noqa: E402

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", (s or "").lower())


def stem_of(fp: str) -> str:
    base = (fp or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".md", ".pdf"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


def head_text(path: Path) -> str:
    if fitz is None:
        return ""
    try:
        doc = fitz.open(str(path))
        t = "".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
        doc.close()
        return t
    except Exception:
        return ""


def main():
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id,title,doi,file_path FROM papers ORDER BY id").fetchall()
    conn.close()
    missing = []
    for r in rows:
        st = stem_of(r["file_path"] or "")
        if not (PDFS / f"{st}.pdf").exists():
            missing.append({"id": r["id"], "stem": st, "title": r["title"],
                            "doi": (r["doi"] or "").strip()})
    print(f"缺失清单 {len(missing)} 篇；back 目录 {len(list(BACK.glob('*.pdf')))} 个文件\n")

    files = sorted(BACK.glob("*.pdf"))
    heads = {}
    for f in files:
        heads[f.name] = head_text(f)

    used, mapping, unmatched_files = set(), [], []
    for m in missing:
        hit = None
        how = ""
        # 1) 文件名精确等于 stem
        for f in files:
            if f.stem == m["stem"]:
                hit, how = f, "文件名精确"
                break
        # 2) 文件名含 DOI
        if not hit and m["doi"]:
            d = m["doi"].lower()
            for f in files:
                if d in f.name.lower():
                    hit, how = f, "文件名含DOI"
                    break
        # 3) 正文含 DOI
        if not hit and m["doi"]:
            d = m["doi"].lower()
            for f in files:
                if m["doi"].lower() in heads.get(f.name, "").lower():
                    hit, how = f, "正文含DOI"
                    break
        # 4) 标题关键词
        if not hit:
            key = norm(m["title"])[:45]
            if len(key) > 12:
                for f in files:
                    if key and key in norm(heads.get(f.name, "")):
                        hit, how = f, "正文标题匹配"
                        break
        if hit:
            used.add(hit.name)
            mapping.append((m, hit, how))
        else:
            m["_miss"] = True

    print("=== 匹配上的（可补回）===")
    for m, f, how in mapping:
        print(f"  id={m['id']:>4} | {how:<12} | {f.name[:58]:<58} → {m['stem'][:45]}.pdf")
    print(f"\n匹配 {len(mapping)} / {len(missing)} 篇")

    still = [m for m in missing if m.get("_miss")]
    print(f"\n=== 仍然缺失 {len(still)} 篇 ===")
    for m in still:
        print(f"  id={m['id']:>4} | {m['journal'] if 'journal' in m else ''} | {m['title'][:62]} | {m['doi']}")

    print(f"\n=== back 里未被匹配的文件 {len(files)-len(used)} 个（可能是新文献或重复）===")
    for f in files:
        if f.name not in used:
            print("  ", f.name)

    if "--apply" in sys.argv:
        import shutil
        n = 0
        for m, f, how in mapping:
            dst = PDFS / f"{m['stem']}.pdf"
            if dst.exists():
                print(f"  [skip] {dst.name} 已存在")
                continue
            shutil.copy2(str(f), str(dst))
            n += 1
            print(f"  [copy] {f.name} → {dst.name}")
        print(f"\n[apply] 已复制 {n} 个 PDF 进 data/pdfs/")


if __name__ == "__main__":
    main()
