#!/usr/bin/env python3
"""实体分簇管线

目的：把实体划成等价类（同一概念的不同写法归为一类），为后续
LLM 合并提供输入。

复杂度设计
----------
  全量判官两两判定        O(N^2)   规模大时不可行
  锚点扫描（簇内一对一）   O(N)     每簇只需 m-1 次判官（m = 唯一名字数）
  先用综合相似度分簇      O(N·k)   把大簇拆小，后续 O(N) 的规模更小

三个阶段
--------
  阶段 1  综合相似度 + 阈值分簇（无判官调用）
          相似度 = 0.8 * 字面 + 0.2 * 向量
          字面  = max(token_jaccard, char3_jaccard, acronym)
          实现  = 阈值建边 + 并查集连通分量（等价 DBSCAN min_samples=2, eps=1-tau）
  阶段 2  大簇锚点扫描（判官调用，O(m)）
          锚点 = 簇内出现次数最多的名字；与其余唯一名字逐一判官
          yes 留在锚点簇（必然干净），no 进入 split 池
  阶段 3  split 池按阶段 1 的原图连通性自动成簇（零成本）

不依赖 sklearn：连通分量实现与 DBSCAN(min_samples=2, eps=1-tau) 等价，
不引入额外依赖。建边的相似度是字面与余弦的加权组合，不是纯余弦邻域。
若改用 ANN/HDBSCAN，需重写建边与成簇两步。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.paths import db_path, entity_merge_dir  # noqa: E402
from modules.config import get as _cfg_get  # noqa: E402

# ---------------- 默认参数（改动需重跑全部阶段）----------------
# 判官（决策模型）地址/密钥/开关一律走 config.yaml 的 judge 段，代码中不硬编码地址。
DEFAULTS = dict(
    topk=30,            # 每实体的近邻候选数（阶段 1 的稀疏化）
    w_lexical=0.8,      # 综合相似度中字面权重
    tau=0.88,           # 建边阈值（等价 DBSCAN 的 eps = 1 - tau）
    big_threshold=60,   # 超过该规模的簇进入阶段 2
    judge_workers=_cfg_get("judge.workers", 16),
    judge_url=_cfg_get("judge.base_url", ""),
    judge_key=_cfg_get("judge.api_key", None) or None,
    judge_instruct=_cfg_get("judge.instruct", ""),
)

PAREN = re.compile(r"\([^)]*\)")
NON = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
TOK = re.compile(r"[a-z0-9\u4e00-\u9fff]+")
STOP = {"of", "the", "and", "for", "in", "on", "to", "with", "a", "an"}


# ---------------- 字面相似度 ----------------
def norm(s: str) -> str:
    """规范化：去括号内容、小写、去非字母数字（保留中英文）。"""
    return NON.sub("", PAREN.sub(" ", s.lower()))


def toks(s: str) -> frozenset:
    return frozenset(TOK.findall(s.lower()))


def char3(s: str) -> frozenset:
    n = norm(s)
    if len(n) >= 3:
        return frozenset(n[i:i + 3] for i in range(len(n) - 2))
    return frozenset([n])


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def acronym(short: str, long_: str) -> float:
    """short 是否为 long_ 的缩写。

    两条路径：
      ① 括号内容与 short 相同（允许数字）：'Coefficient of Determination (R²)' vs 'R²'
      ② 词首缩写（禁数字，需 >=2 词、short >=2 字符）：'Mean Intersection over Union' vs 'mIoU'
    单字符 short 不算（'R²'->'r' 会误配 'RF'）。
    """
    s = norm(short)
    if len(s) < 2:
        return 0.0
    for m in re.findall(r"\(([^)]*)\)", long_):
        if norm(m) == s:
            return 1.0
    if any(ch.isdigit() for ch in s):
        return 0.0
    words = [t for t in TOK.findall(PAREN.sub(" ", long_.lower())) if t not in STOP]
    if words and len(words) >= 2 and s == "".join(w[0] for w in words):
        return 1.0
    return 0.0


def lexical(name_a: str, name_b: str, cache: dict) -> float:
    """字面相似度 = max(token_jaccard, char3_jaccard, acronym)。"""
    key = (name_a, name_b) if name_a <= name_b else (name_b, name_a)
    v = cache.get(key)
    if v is not None:
        return v
    ta = toks(name_a)
    tb = toks(name_b)
    ca = char3(name_a)
    cb = char3(name_b)
    v = max(jaccard(ta, tb), jaccard(ca, cb),
            acronym(name_a, name_b), acronym(name_b, name_a))
    cache[key] = v
    return v


# ---------------- 数据加载 ----------------
def load(db: str):
    conn = sqlite3.connect(db, timeout=120)
    rows = conn.execute(
        "SELECT id, name, vector FROM entities "
        "WHERE vector IS NOT NULL AND type NOT IN ('paper') ORDER BY id").fetchall()
    conn.close()
    ids = [int(r[0]) for r in rows]
    names = [r[1] for r in rows]
    V = np.array([np.frombuffer(r[2], dtype=np.float32) for r in rows])
    return ids, names, V


def build_edges(names, V, topk, w, cache, log=print):
    """kNN 候选 + 综合相似度。返回 (edges dict[(a,b)]=score, 统计)。"""
    n = len(V)
    cand = {}
    CH = 1000
    for s in range(0, n, CH):
        S = V[s:s + CH] @ V.T
        for i in range(len(S)):
            S[i, s + i] = -1.0
        idx = np.argpartition(-S, topk, axis=1)[:, :topk]
        vals = np.take_along_axis(S, idx, axis=1)
        for r in range(len(idx)):
            for j in range(topk):
                a, b = s + r, int(idx[r, j])
                if a > b:
                    a, b = b, a
                v = float(vals[r, j])
                if cand.get((a, b), -1.0) < v:
                    cand[(a, b)] = v
    log(f"  kNN 候选 {len(cand)} 条")
    edges = {}
    exact = 0
    for (a, b), sim in cand.items():
        if norm(names[a]) and norm(names[a]) == norm(names[b]) and len(norm(names[a])) >= 2:
            exact += 1
            edges[(a, b)] = 1.0
            continue
        L = lexical(names[a], names[b], cache)
        edges[(a, b)] = w * L + (1 - w) * sim
    log(f"  其中规范化完全同名 {exact} 条")
    return edges


# ---------------- 阶段 1：分簇 ----------------
def components(n, edges, tau):
    """返回 [成员索引列表]。

    实现：阈值建边 + 并查集连通分量（等价 DBSCAN min_samples=2, eps=1-tau；
    孤立单点对应 DBSCAN 的噪声点）。不依赖 sklearn。
    """
    par = list(range(n))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    for (a, b), v in edges.items():
        if v >= tau:
            ra, rb = find(a), find(b)
            if ra != rb:
                par[ra] = rb
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


# ---------------- 阶段 2：锚点扫描 ----------------
def judge_pair(url, instruct, a, b, key=None, retries=3):
    body = {"state": {"term1": a, "term2": b},
            "questions": {"same": {"type": "noul", "instructions": instruct,
                                   "criteria": {"true": "same", "false": "different"}}}}
    headers = {"Content-Type": "application/json",
               "User-Agent": "curl/8.5.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/systemone",
        data=json.dumps(body, ensure_ascii=False).encode(), headers=headers)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(retries):
        try:
            with op.open(req, timeout=60) as r:
                return float(json.loads(r.read().decode())["answers"]["same"]["noul"])
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return float("nan")


def anchor_scan(cluster, names, cfg, log=print):
    """把一个簇按锚点拆开。返回 (锚点簇, [(子簇, 原因)]) 的成员索引分组。

    - 锚点 = 簇内出现次数最多的名字
    - 与其余唯一名字逐一判官：>=0.5 留在锚点簇，否则进 split 池
    - split 池稍后由调用方按原图连通性切分
    """
    uniq = sorted({names[i] for i in cluster})
    if len(uniq) <= 1:
        return cluster, [], 0
    cnt = Counter(names[i] for i in cluster)
    anchor = max(uniq, key=lambda x: (cnt[x], len(x)))
    others = [u for u in uniq if u != anchor]
    with ThreadPoolExecutor(max_workers=cfg["judge_workers"]) as ex:
        scores = list(ex.map(
            lambda u: judge_pair(cfg["judge_url"], cfg["judge_instruct"],
                                 anchor, u, cfg.get("judge_key")),
            others))
    # NaN = 调用失败：不能静默当作"不同"，否则整簇被误拆
    nan = sum(1 for x in scores if x != x)
    if nan:
        log(f"    [警告] {nan}/{len(scores)} 次判官调用失败（NaN），这些按'相同'处理")
    keep_names = {anchor} | {u for u, s in zip(others, scores) if not (s == s) or s >= 0.5}
    keep = [i for i in cluster if names[i] in keep_names]
    rest = [i for i in cluster if names[i] not in keep_names]
    log(f"    锚点 '{anchor[:40]}' 判 {len(others)} 次 → 留下 {len(keep)} 实体, "
        f"分出 {len(rest)} 实体")
    return keep, rest, len(others)


def split_rest(rest, names, edges, tau, log=print):
    """split 池按原图连通性切分（零成本，不重算相似度）。"""
    pos = {i: k for k, i in enumerate(rest)}
    m = len(rest)
    par = list(range(m))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    for (a, b), v in edges.items():
        if v >= tau and a in pos and b in pos:
            ra, rb = find(pos[a]), find(pos[b])
            if ra != rb:
                par[ra] = rb
    groups = defaultdict(list)
    for i in rest:
        groups[find(pos[i])].append(i)
    return list(groups.values())


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(db_path()))
    ap.add_argument("--out", default=str(entity_merge_dir() / "clusters.json"))
    ap.add_argument("--stage", default="1", choices=["1", "all"])
    ap.add_argument("--topk", type=int, default=DEFAULTS["topk"])
    ap.add_argument("--w", type=float, default=DEFAULTS["w_lexical"])
    ap.add_argument("--tau", type=float, default=DEFAULTS["tau"])
    ap.add_argument("--big", type=int, default=DEFAULTS["big_threshold"])
    ap.add_argument("--judge-url", default=DEFAULTS["judge_url"])
    ap.add_argument("--judge-key", default=None,
                    help="经 proxy/CF 访问时需要的 Bearer key")
    ap.add_argument("--judge-instruct", default=DEFAULTS["judge_instruct"])
    a = ap.parse_args()

    cfg = dict(DEFAULTS)
    cfg.update(judge_url=a.judge_url, judge_instruct=a.judge_instruct,
               judge_key=a.judge_key)
    t0 = time.time()

    log = print
    log("=" * 60)
    log(f"参数: topk={a.topk} w_lexical={a.w} tau={a.tau} big={a.big}")
    log("=" * 60)

    ids, names, V = load(a.db)
    n = len(ids)
    log(f"实体 {n}")
    cache = {}
    edges = build_edges(names, V, a.topk, a.w, cache, log)
    log(f"建边完成 {len(edges)} 条，用时 {time.time()-t0:.1f}s")

    comps = components(n, edges, a.tau)
    sizes = sorted((len(c) for c in comps), reverse=True)
    log(f"\n阶段 1：{len(comps)} 个分量，最大 {sizes[0]}，"
        f">{a.big} 的分量 {sum(1 for s in sizes if s > a.big)} 个，"
        f"单例 {sum(1 for s in sizes if s == 1)} 个")

    # 阶段 2/3
    clusters = []
    judged = 0
    if a.stage == "all":
        big = [c for c in comps if len(c) > a.big]
        log(f"\n阶段 2：对 {len(big)} 个 >{a.big} 的分量做锚点扫描")
        for c in big:
            keep, rest, nj = anchor_scan(c, names, cfg, log)
            judged += nj
            clusters.append(keep)
            if rest:
                subs = split_rest(rest, names, edges, a.tau)
                log(f"      剩余 {len(rest)} 实体 → {len(subs)} 个子簇")
                clusters.extend(subs)
        small = [c for c in comps if len(c) <= a.big]
        clusters.extend(small)
        log(f"\n判官调用合计 {judged} 次")
    else:
        clusters = comps

    sizes = sorted((len(c) for c in clusters), reverse=True)
    log(f"\n最终：{len(clusters)} 个簇，最大 {sizes[0]}，"
        f"单例 {sum(1 for s in sizes if s == 1)} 个")
    log(f"总用时 {(time.time()-t0)/60:.1f} 分钟")

    out = {
        "params": {"topk": a.topk, "w_lexical": a.w, "tau": a.tau,
                   "big_threshold": a.big, "stage": a.stage},
        "n_entities": n,
        "n_edges": len(edges),
        "n_clusters": len(clusters),
        "judge_calls": judged,
        "clusters": [
            {"ids": [ids[i] for i in c],
             "names": sorted({names[i] for i in c}),
             "size": len(c)}
            for c in clusters],
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(out, open(a.out, "w"), ensure_ascii=False)
    log(f"已写入 {a.out}")


if __name__ == "__main__":
    main()
