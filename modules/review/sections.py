"""综述稿章节解析（唯一实现）。

初稿（`draft_rN.md`）与成稿（`review_draft_rN.md`）共用同一套标题约定：

    章      `## N. 标题`（也接受 `## 第 N 章 标题`、`## N、标题`）
    节      `### N.M 标题`（编号前缀必须与所在章一致，否则记入 sub_mismatch）
    无编号  `## 摘要` / `## 已知局限` 等：自成一块，边界处理
    终止    `## 参考文献` / `## References` 及其后内容不属正文

为什么必须只认二级标题：`### 2.1 风能` 若按 `^#{1,4}` 匹配，会被当成"第 2 章、标题 '1 风能'"，
章节计数与章内统计全错。
"""
from __future__ import annotations

import re

SEC_RE = re.compile(r"^#{2}(?!#)\s*(?:第\s*)?(\d+)\s*[章.、：:]\s*(.+?)\s*$")
SUB_RE = re.compile(r"^#{3}(?!#)\s*(\d+)\s*[.、]\s*(\d+)\s+(.+?)\s*$")
STOP_RE = re.compile(r"^#{1,6}\s*(?:参考文献|References|文献列表)\s*$", re.I)
UNNUM_RE = re.compile(r"^#{1,6}(?!#)\s*[^\d\s]")
CITE_RE = re.compile(r"\[([SE]\d+)\]")


def parse_sections(text: str) -> list[dict]:
    """拆章。返回 [{num, title, body, subs, sub_mismatch}]，body 为纯文本。"""
    secs: list[dict] = []
    cur: dict | None = None
    for ln in text.splitlines():
        if STOP_RE.match(ln):
            break                     # 参考文献及其后不属正文
        m = SEC_RE.match(ln)
        if m:
            if cur:
                secs.append(cur)
            cur = {"num": m.group(1), "title": m.group(2).strip(),
                   "body": [], "subs": [], "sub_mismatch": []}
            continue
        if UNNUM_RE.match(ln):        # 摘要 / 已知局限 等：自成一块，不并入上一章
            if cur:
                secs.append(cur)
            cur = None
            continue
        ms = SUB_RE.match(ln)
        if ms and cur is not None:
            if ms.group(1) == cur["num"]:
                cur["subs"].append(f"{ms.group(1)}.{ms.group(2)} {ms.group(3).strip()}")
            else:
                cur["sub_mismatch"].append(ln.strip()[:40])
        if cur is not None:
            cur["body"].append(ln)
    if cur:
        secs.append(cur)
    for s in secs:
        s["body"] = "\n".join(s["body"]).strip()
    return [s for s in secs if s["title"]]


def section_body(text: str, num: str) -> str:
    """取某章正文（不含标题行）；找不到返回空串。"""
    for s in parse_sections(text):
        if s["num"] == num:
            return s["body"]
    return ""
