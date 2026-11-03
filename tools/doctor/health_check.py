#!/usr/bin/env python3
"""全库健康检查：逐篇核对内容量、超图（实体/超边）、摘要、向量、元数据，
并检查悬空引用（超边指向不存在的论文/实体）。"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.paths import db_path, pdfs_dir  # noqa: E402

c = sqlite3.connect(str(db_path()))
c.row_factory = sqlite3.Row

papers = c.execute(
    "SELECT id,title,year,doi,journal,file_path FROM papers").fetchall()
print(f"论文总数 {len(papers)}")

# 每篇的 chunks / 字符数
chunk_stat = {}
for r in c.execute(
        "SELECT paper_id, COUNT(*) n, SUM(LENGTH(content)) chars "
        "FROM chunks GROUP BY paper_id"):
    chunk_stat[r["paper_id"]] = (r["n"], r["chars"] or 0)

# 每篇的实体数（排除 paper 自身实体）
ent_stat = {}
for r in c.execute(
        "SELECT paper_id, COUNT(*) n FROM entities "
        "WHERE type != 'paper' GROUP BY paper_id"):
    ent_stat[r["paper_id"]] = r["n"]

# 超边归属（source_papers 是 JSON 数组）
he_stat = {p["id"]: 0 for p in papers}
he_bad = []
for r in c.execute("SELECT id, source_papers FROM hyperedges"):
    try:
        ids = json.loads(r["source_papers"] or "[]")
    except Exception:
        he_bad.append(r["id"])
        continue
    for pid in ids:
        if pid in he_stat:
            he_stat[pid] += 1
        else:
            he_bad.append((r["id"], pid))

sum_ok = set()
try:
    for r in c.execute(
            "SELECT paper_id FROM ingest_status WHERE summarized=1"):
        sum_ok.add(r["paper_id"])
except Exception as e:  # noqa: BLE001
    print("[warn] ingest_status 读取失败:", e)

content_bad, ent_missing, he_missing, meta_bad, sum_missing = [], [], [], [], []
pdf_missing = []


def _stem_of(file_path: str) -> str:
    """papers.file_path 可能是 .md 或 .pdf 路径，取文件名去后缀作为 stem。"""
    base = (file_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".md", ".pdf"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


PDF_DIR = pdfs_dir()

for p in papers:
    pid, title = p["id"], p["title"] or ""
    n, chars = chunk_stat.get(pid, (0, 0))
    ne = ent_stat.get(pid, 0)
    nh = he_stat.get(pid, 0)

    if n < 3 or chars < 3000:
        content_bad.append((pid, title[:50], n, chars))
    if ne == 0:
        ent_missing.append((pid, title[:50], n))
    elif nh == 0:
        he_missing.append((pid, title[:50], ne))
    if title.lower().endswith((".pdf", ".md", ".txt")) or len(title) < 6:
        meta_bad.append((pid, title[:60], "标题异常"))
    elif not p["year"]:
        meta_bad.append((pid, title[:50], "缺年份"))
    if pid not in sum_ok:
        sum_missing.append((pid, title[:50]))
    # PDF 原文缺失（库里只剩文本、拿不到 PDF 的篇目）
    stem = _stem_of(p["file_path"] or "")
    if not (PDF_DIR / f"{stem}.pdf").exists():
        pdf_missing.append((pid, title[:50], stem))


def show(name, rows, limit=20):
    print(f"\n=== {name}: {len(rows)} ===")
    for r in rows[:limit]:
        print("  ", r)
    if len(rows) > limit:
        print(f"   ... 另有 {len(rows) - limit} 条")


print("\n--- 汇总 ---")
print(f"  无实体（超图未提取）: {len(ent_missing)}")
print(f"  有实体但无超边      : {len(he_missing)}")
print(f"  内容过少(<3chunk)   : {len(content_bad)}")
print(f"  元数据异常           : {len(meta_bad)}")
print(f"  缺摘要               : {len(sum_missing)}")
print(f"  缺 PDF 原文          : {len(pdf_missing)}")

show("无实体", ent_missing)
show("有实体但无超边", he_missing)
show("内容过少", content_bad)
show("元数据异常", meta_bad, 10)
show("缺摘要", sum_missing, 12)
show("缺 PDF 原文", pdf_missing, 40)

print(f"\n=== 悬空超边引用: {len(he_bad)} ===")
for b in he_bad[:10]:
    print("  ", b)

# 实体悬空：participants 指向不存在的实体
ent_ids = {r[0] for r in c.execute("SELECT id FROM entities")}
dangling = 0
for r in c.execute("SELECT id, participants FROM hyperedges"):
    try:
        parts = json.loads(r["participants"] or "[]")
    except Exception:
        continue
    if parts and not set(parts) <= ent_ids:
        dangling += 1
print(f"\n=== 参与者含不存在实体的超边: {dangling} ===")
