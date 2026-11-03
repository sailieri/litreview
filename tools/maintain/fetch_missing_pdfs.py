# -*- coding: utf-8 -*-
"""补回 data/pdfs 里缺失的 PDF。

按优先级尝试三条 OA 通道：
  1) Semantic Scholar  openAccessPdf（DOI 或标题检索）
  2) arXiv API         标题精确/模糊检索 → arxiv.org/pdf/<id>
  3) Unpaywall         API v2（需要 email 参数）

下到 data/pdfs/<stem>.pdf，校验 %PDF 文件头；失败记 log，不覆盖已有文件。

用法：
  python tools/maintain/fetch_missing_pdfs.py                 # 只补当前缺的
  python tools/maintain/fetch_missing_pdfs.py --dry           # 只查不下载
  python tools/maintain/fetch_missing_pdfs.py --ids 527,469   # 指定 id
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.config import section as _cfg_section  # noqa: E402

SRC = _cfg_section("sources")
S2 = str(SRC.get("semanticscholar") or "").rstrip("/")
ARXIV_API = str(SRC.get("arxiv_api") or "")
ARXIV_PDF = str(SRC.get("arxiv_pdf") or "").rstrip("/")
UNPAYWALL = str(SRC.get("unpaywall") or "").rstrip("/")
OPENALEX = str(SRC.get("openalex") or "").rstrip("/")
MAILTO = SRC.get("mailto", "")
from modules.paths import data_root, db_path, pdfs_dir  # noqa: E402
PDF_DIR = pdfs_dir()
DB = db_path()
LOG = data_root() / "fetch_missing_pdfs.log"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S2_KEY = os.environ.get("S2_API_KEY", "")

# SSL：系统证书链对部分出版方 CDN（Elsevier/Springer）校验不通过 → 显式用 certifi
try:
    import certifi
    import ssl
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = None
SSL_CTX_INSECURE = None


def _ssl_insecure():
    global SSL_CTX_INSECURE
    if SSL_CTX_INSECURE is None:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        SSL_CTX_INSECURE = ctx
    return SSL_CTX_INSECURE


def http_get(url, timeout=30, headers=None):
    h = {"User-Agent": UA, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    # 禁用系统代理（环境级代理会劫持直连请求）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if SSL_CTX is not None:
        opener.add_handler(urllib.request.HTTPSHandler(context=SSL_CTX))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, ssl.SSLError):
            # 证书链失败 → 放宽校验重试一次（只对已从 OA 索引拿到的直链）
            opener2 = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=_ssl_insecure()))
            with opener2.open(req, timeout=timeout) as r:
                return r.read()
        raise


def http_get_json(url, timeout=30, retries=4):
    for i in range(retries):
        try:
            return json.loads(http_get(url, timeout).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < retries - 1:
                time.sleep(6 * (i + 1))
                continue
            raise
        except Exception as e:
            if i == retries - 1:
                raise e
            time.sleep(3 * (i + 1))
    return None


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF" or data[:1024].find(b"%PDF") >= 0


def stem_of(file_path: str) -> str:
    base = (file_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".md", ".pdf"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


def missing_papers():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, title, doi, journal, year, file_path FROM papers").fetchall()
    conn.close()
    out = []
    for r in rows:
        stem = stem_of(r["file_path"] or "")
        if not (PDF_DIR / f"{stem}.pdf").exists():
            out.append(dict(id=r["id"], title=r["title"], doi=r["doi"] or "",
                            journal=r["journal"] or "", year=r["year"], stem=stem))
    return out


# ── 通道 1：Semantic Scholar ────────────────────────────────────────────
def s2_by_doi(doi):
    url = (S2 + "/graph/v1/paper/DOI:"
           + urllib.parse.quote(doi, safe="")
           + "?fields=openAccessPdf,title,externalIds")
    try:
        d = http_get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(8)
        return None
    if not d:
        return None
    oap = (d.get("openAccessPdf") or {}).get("url")
    arx = (d.get("externalIds") or {}).get("ArXiv")
    return {"pdf": oap, "arxiv": arx}


def s2_by_title(title):
    q = urllib.parse.quote(title[:250])
    url = (S2 + "/graph/v1/paper/search?query=" + q
           + "&limit=3&fields=openAccessPdf,title,externalIds")
    try:
        d = http_get_json(url)
    except Exception:
        return []
    out = []
    for p in (d or {}).get("data", []) or []:
        out.append({"pdf": (p.get("openAccessPdf") or {}).get("url"),
                    "arxiv": (p.get("externalIds") or {}).get("ArXiv"),
                    "title": p.get("title")})
    return out


# ── 通道 2：arXiv ───────────────────────────────────────────────────────
def arxiv_by_id(aid):
    return f"{ARXIV_PDF}/{aid}"


def arxiv_by_title(title):
    q = urllib.parse.quote(f'ti:"{title[:160]}"')
    url = f"{ARXIV_API}?search_query={q}&max_results=3"
    try:
        raw = http_get(url, timeout=30).decode("utf-8", "replace")
    except Exception:
        return []
    ids = re.findall(r"<id>http[s]?://arxiv\.org/abs/([^<]+)</id>", raw)
    titles = re.findall(r"<title>(.*?)</title>", raw, re.S)
    out = []
    for i, aid in enumerate(ids):
        if "arxiv.org/abs/" in aid:
            continue
        out.append({"id": aid.split("v")[0], "title": titles[i + 1].strip() if i + 1 < len(titles) else ""})
    return out


# ── 通道 3：Unpaywall ───────────────────────────────────────────────────
def unpaywall(doi, email=None):
    email = email or MAILTO
    url = f"{UNPAYWALL}/v2/{urllib.parse.quote(doi, safe='')}?email={email}"
    try:
        d = http_get_json(url, timeout=30, retries=2)
    except Exception:
        return None
    if not d:
        return None
    best = d.get("best_oa_location") or {}
    return best.get("url_for_pdf") or best.get("url")


def openalex(doi):
    """OpenAlex：best_oa_location.pdf_url / primary_location.pdf_url（免费无 key）。"""
    url = OPENALEX + "/works/doi:" + urllib.parse.quote(doi, safe="")
    try:
        d = http_get_json(url, timeout=30, retries=2)
    except Exception:
        return None
    if not d:
        return None
    for loc in (d.get("best_oa_location"), d.get("primary_location")):
        if loc and loc.get("pdf_url"):
            return loc["pdf_url"]
    return None


def _pdf_links(html: str, base: str):
    """从落地页 HTML 里挑 PDF 直链（优先 download/pdf 关键字）。"""
    links = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)
    links += re.findall(r'(https?://[^\s"\'<>]+\.pdf)', html)
    out, seen = [], set()
    for l in links:
        if l.startswith("/"):
            l = urllib.parse.urljoin(base, l)
        if l in seen:
            continue
        seen.add(l)
        out.append(l)
    out.sort(key=lambda x: (0 if re.search(r"download|/pdf/|pdf$", x, re.I) else 1))
    return out


def try_download(url, dest: Path, depth=0):
    try:
        data = http_get(url, timeout=90,
                        headers={"Referer": url.split("/")[2] + "/"})
    except urllib.error.HTTPError as e:
        return False, f"http-{e.code}"
    except Exception as e:
        return False, f"dl-fail {type(e).__name__}:{e}"
    if not is_pdf(data):
        # 落地页 → 抽出其中的 PDF 直链再试
        if depth < 2 and len(data) < 3_000_000 and b"<html" in data[:2000].lower():
            html = data.decode("utf-8", "replace")
            links = _pdf_links(html, url)
            for l in links[:6]:
                good, msg = try_download(l, dest, depth + 1)
                if good:
                    return True, f"{msg} (via {url.split('/')[2]})"
            return False, f"html-no-pdf({len(links)} links)"
        return False, f"not-pdf ({len(data)}B)"
    if len(data) < 20000:
        return False, f"too-small ({len(data)}B)"
    dest.write_bytes(data)
    return True, f"ok {len(data)//1024}KB"


def candidates(p):
    """产出候选 PDF URL（按优先级）。"""
    out = []
    doi = (p["doi"] or "").strip()
    title = (p["title"] or "").strip()
    # DOI 里的 arXiv
    m = re.search(r"arxiv\.(\d{4}\.\d{4,5})", doi, re.I)
    if m:
        out.append((f"doi-arxiv:{m.group(1)}", arxiv_by_id(m.group(1))))
    if doi:
        s2 = s2_by_doi(doi)
        if s2:
            if s2.get("arxiv"):
                out.append((f"s2-arxiv:{s2['arxiv']}", arxiv_by_id(s2["arxiv"])))
            if s2.get("pdf"):
                out.append(("s2-doi", s2["pdf"]))
            u = unpaywall(doi)
            if u:
                out.append(("unpaywall", u))
            oa = openalex(doi)
            if oa:
                out.append(("openalex", oa))
    if title:
        for c in s2_by_title(title):
            if c.get("arxiv"):
                out.append((f"s2t-arxiv:{c['arxiv']}", arxiv_by_id(c["arxiv"])))
            if c.get("pdf"):
                out.append(("s2t-pdf", c["pdf"]))
        for c in arxiv_by_title(title):
            out.append((f"arxiv-ti:{c['id']}", arxiv_by_id(c["id"])))
    # 去重保序
    seen, uniq = set(), []
    for tag, url in out:
        if url and url not in seen:
            seen.add(url)
            uniq.append((tag, url))
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--ids", default="")
    ap.add_argument("--only-open", action="store_true", help="只试 openAccessPdf/unpaywall，不试 arxiv 标题匹配")
    args = ap.parse_args()

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    papers = missing_papers()
    if args.ids:
        want = {int(x) for x in args.ids.split(",") if x.strip()}
        papers = [p for p in papers if p["id"] in want]
    print(f"[plan] 待补 {len(papers)} 篇", flush=True)

    ok, fail = 0, 0
    for p in papers:
        dest = PDF_DIR / f"{p['stem']}.pdf"
        if dest.exists():
            print(f"[skip] {p['id']} 已有", flush=True)
            continue
        cands = candidates(p)
        if args.only_open:
            cands = [c for c in cands if not c[0].startswith(("arxiv-ti", "s2t-"))]
        tag_note = ", ".join(t for t, _ in cands) or "no-candidate"
        print(f"[try ] {p['id']} | {p['title'][:50]} | cand: {tag_note}", flush=True)
        done = False
        for tag, url in cands:
            if args.dry:
                good, msg = True, "dry-skip"
            else:
                good, msg = try_download(url, dest)
            print(f"       {tag} -> {msg}", flush=True)
            if good:
                ok += 1
                done = True
                break
            time.sleep(1.5)
        if not done:
            fail += 1
            with LOG.open("a", encoding="utf-8") as f:
                f.write(f"{p['id']}\t{p['doi']}\t{p['title']}\n")
        time.sleep(2.0)
    print(f"[done] ok={ok} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
