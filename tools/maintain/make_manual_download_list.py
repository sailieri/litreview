# -*- coding: utf-8 -*-
"""把「补不回来的 PDF」导成人工下载清单（CSV + Markdown）。

用法：python tools/maintain/orphans.py list
输出：data/missing_pdfs_manual_<YYYYMMDD>.csv / .md（日期后缀由脚本内的文件名常量决定）
"""
import csv
import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.config import section as _cfg_section  # noqa: E402
from modules.paths import data_root, db_path, pdfs_dir  # noqa: E402

SRC = _cfg_section("sources")
DATA = data_root()                 # 跟随 config.yaml data_root，不再写死 data/
DB = db_path()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def stem_of(fp: str) -> str:
    base = (fp or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".md", ".pdf"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id,title,doi,journal,year,file_path FROM papers ORDER BY id").fetchall()
    conn.close()
    out = []
    for r in rows:
        stem = stem_of(r["file_path"] or "")
        if (DATA / "pdfs" / f"{stem}.pdf").exists():
            continue
        doi = (r["doi"] or "").strip()
        title = (r["title"] or "").strip()
        out.append({
            "id": r["id"], "title": title, "journal": r["journal"] or "",
            "year": r["year"] or "", "doi": doi,
            "target_filename": stem + ".pdf",
            "doi_url": ("%s/%s" % (SRC.get("doi", ""), doi)) if doi else "",
            "arxiv_search": SRC.get("arxiv_search", "")
                            + urllib.parse.quote(title),
            "google_scholar": SRC.get("google_scholar", "")
                              + urllib.parse.quote('"' + title + '"'),
        })
    csv_p = DATA / "missing_pdfs_manual.csv"
    with csv_p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    L = ["# 待人工下载的 PDF 清单", "",
         "存到：`%s/<target_filename>`（文件名必须完全一致，前端按这个名找）" % pdfs_dir(),
         "下完校验：`python tools/maintain/fetch_missing_pdfs.py --dry` 看还缺哪篇；或起 webapp 看列表页 PDF 圆点。",
         "", "| id | 期刊 | 年 | 标题 | DOI | 目标文件名 | 检索入口 |",
         "|---|---|---|---|---|---|---|"]
    for r in out:
        links = []
        if r["doi_url"]:
            links.append("[DOI](" + r["doi_url"] + ")")
        links.append("[arXiv](" + r["arxiv_search"] + ")")
        links.append("[Scholar](" + r["google_scholar"] + ")")
        L.append("| " + " | ".join([
            str(r["id"]), r["journal"], str(r["year"]),
            r["title"].replace("|", "/"), r["doi"] or "-",
            "`" + r["target_filename"] + "`", " ".join(links)]) + " |")
    md_p = DATA / "missing_pdfs_manual.md"
    md_p.write_text("\n".join(L), encoding="utf-8")
    print(f"[ok] 缺 {len(out)} 篇 → {csv_p} / {md_p}")


if __name__ == "__main__":
    main()
