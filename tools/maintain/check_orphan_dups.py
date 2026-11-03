# -*- coding: utf-8 -*-
"""孤儿 PDF 查重体检：逐个判定「与库内某篇重复 / 是新文献」。

判定依据（从强到弱）：
  1) metadata 里的 DOI == 库内某篇的 DOI          → 强重复
  2) PDF 首页文本里的 DOI == 库内某篇的 DOI        → 强重复
  3) 标题（metadata 或 PDF 首页）归一化后完全相等   → 强重复
  4) 标题归一化后前缀 50 字符相同                  → 疑似重复
  5) difflib 相似度 ≥ 0.85                        → 疑似重复
  6) 以上都不中                                    → 新文献（未入库）

用法：.venv/Scripts/python.exe tools/maintain/orphans.py dups [--json]
"""
import difflib
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.paths import data_root, db_path, metadata_dir, normalized_dir, pdfs_dir  # noqa: E402

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


def pdf_head(path: Path):
    """返回 (首页标题, 前两页文本)"""
    if pymupdf is None:
        return "", ""
    try:
        doc = pymupdf.open(str(path))
        page = doc[0]
        best, bs = "", 0
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    t = sp.get("text", "").strip()
                    if len(t) > 8 and sp.get("size", 0) > bs:
                        best, bs = t, sp["size"]
        text = "".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
        doc.close()
        return best, text
    except Exception:
        return "", ""


def meta_of(stem: str):
    p = metadata_dir() / f"{stem}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d.get("title", "") or "", (d.get("doi", "") or "")
        except Exception:
            return "", ""
    return "", ""


def main():
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id,title,doi,year,journal,file_path FROM papers").fetchall()
    conn.close()
    papers = []
    for r in rows:
        papers.append({"id": r["id"], "title": r["title"] or "", "doi": (r["doi"] or "").strip(),
                       "norm": norm(r["title"])})
    stems = {stem_of(r["file_path"] or ""): r["id"] for r in rows}

    orphans = sorted(f for f in PDFS.glob("*.pdf") if f.stem not in stems)
    out = []
    for f in orphans:
        stem = f.stem
        m_title, m_doi = meta_of(stem)
        p_title, p_text = pdf_head(f)
        title = m_title or p_title
        doi = m_doi
        if not doi:
            mm = re.findall(r"10\.\d{4,9}/[^\s\"'<>,;)]+", p_text)
            doi = mm[0].rstrip(".") if mm else ""

        verdict, match, how = "新文献（未入库）", None, ""
        # 1/2 DOI
        if doi:
            for pp in papers:
                if pp["doi"] and pp["doi"].lower() == doi.lower():
                    verdict, match, how = "重复", pp["id"], f"DOI 相同 ({doi})"
                    break
        # 3/4 标题
        if match is None:
            nt = norm(title)
            for pp in papers:
                if pp["norm"] and pp["norm"] == nt:
                    verdict, match, how = "重复", pp["id"], "标题完全相同"
                    break
            if match is None and len(nt) >= 20:
                for pp in papers:
                    if pp["norm"][:50] == nt[:50] and pp["norm"][:50]:
                        verdict, match, how = "疑似重复", pp["id"], "标题前50字符相同"
                        break
        # 5 difflib
        if match is None and len(norm(title)) >= 20:
            best = max(papers, key=lambda pp: difflib.SequenceMatcher(
                None, norm(title)[:120], pp["norm"][:120]).ratio())
            r = difflib.SequenceMatcher(None, norm(title)[:120], best["norm"][:120]).ratio()
            if r >= 0.85:
                verdict, match, how = "疑似重复", best["id"], f"标题相似度 {r:.2f}"
        out.append({"file": f.name, "title": title, "doi": doi, "verdict": verdict,
                    "match_id": match, "how": how,
                    "size_mb": round(f.stat().st_size / 1048576, 1),
                    "has_norm": (normalized_dir() / f"{stem}.md").exists(),
                    "has_meta": (metadata_dir() / f"{stem}.json").exists()})

    dup = [o for o in out if o["verdict"] == "重复"]
    sus = [o for o in out if o["verdict"] == "疑似重复"]
    new = [o for o in out if o["verdict"].startswith("新文献")]
    print(f"孤儿 PDF {len(out)} 个 → 重复 {len(dup)} / 疑似重复 {len(sus)} / 新文献 {len(new)}\n")
    for tag, lst in (("重复", dup), ("疑似重复", sus), ("新文献", new)):
        print(f"===== {tag} =====")
        for o in lst:
            print(f"  {o['file'][:58]}")
            print(f"    标题: {o['title'][:76]}")
            print(f"    DOI : {o['doi'] or '-'}   {o['size_mb']}MB  "
                  f"norm={'Y' if o['has_norm'] else 'N'} meta={'Y' if o['has_meta'] else 'N'}")
            if o["match_id"]:
                mp = next(p for p in papers if p["id"] == o["match_id"])
                print(f"    → 命中库内 id={o['match_id']}  {o['how']}")
                print(f"      库内标题: {mp['title'][:76]}")
        print()
    if "--json" in sys.argv:
        (data_root() / "orphan_dup_report.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
