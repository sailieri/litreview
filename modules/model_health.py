# -*- coding: utf-8 -*-
"""模型能力探活。

入库流程依赖三类外部模型，任何一类不可达都会让某些阶段无法完成：

    能力         用途                          来源
    llm          规范化 / 摘要 / 实体 / 超边    steps.normalize|summary|extract_hyper 的 provider
    embedding    文本向量                      config.embedding（/api/embed 端点，含候选链）
    judge        实体合并终审（决策模型）      steps.merge 的 provider

设计原则：
  1. **只读探测**，不写库、不产生业务数据；判定失败绝不影响已有数据。
  2. **快**：并发探、单点 5 秒超时；结果缓存 30 秒，避免面板刷新打爆端点。
  3. **禁系统代理**：urllib 默认会读取系统代理配置，代理存在时请求可能被劫持成 502
     ，这里显式 ProxyHandler({})。
  4. 探测方式优先用「列模型」这类轻请求（GET /models）；不支持时退化为一次
     极小 chat 调用。embedding 用一条 "ping" 文本换向量。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_CACHE: dict = {"at": 0.0, "data": None}
CACHE_TTL = 30.0
TIMEOUT = 5.0


def _load_cfg() -> dict:
    """统一走 modules.config（唯一读者）。失败时返回空 dict，不让探活自身报错。"""
    try:
        from modules.config import load as _load
        return _load()
    except Exception:  # noqa: BLE001
        return {}


def _step_cfg(cfg: dict, step: str) -> dict:
    """steps.<step> → providers.<name>，返回 {base_url, api_key, model}。"""
    sc = ((cfg.get("steps") or {}).get(step) or {})
    prov = ((cfg.get("providers") or {}).get(sc.get("provider") or "") or {})
    if prov:
        alias = sc.get("model", "default")
        model = (prov.get("models") or {}).get(alias, "")
        return {"base_url": (prov.get("base_url") or "").rstrip("/"),
                "api_key": prov.get("api_key") or "", "model": model}
    legacy = cfg.get(step) or {}
    if isinstance(legacy, dict) and legacy.get("base_url"):
        return {"base_url": legacy["base_url"].rstrip("/"),
                "api_key": legacy.get("api_key") or "",
                "model": legacy.get("model") or ""}
    return {"base_url": "", "api_key": "", "model": ""}


def _get_json(url: str, api_key: str = "", timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}" if api_key else "",
        "Content-Type": "application/json",
        "User-Agent": "litreview-health/1.0",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")


def _post_json(url: str, payload: dict, api_key: str = "", timeout: float = TIMEOUT):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}" if api_key else "",
        "Content-Type": "application/json",
        "User-Agent": "litreview-health/1.0",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")


def _probe_llm(step: str, cfg: dict) -> dict:
    """探一个 LLM 端点：先 GET /models（轻），失败再试最小 chat。"""
    sc = _step_cfg(cfg, step)
    out = {"ok": False, "step": step, "endpoint": sc["base_url"],
           "model": sc["model"], "latency_ms": 0, "error": "", "models_seen": 0}
    if not sc["base_url"]:
        out["error"] = "config 未配置该步骤的 provider"
        return out
    t0 = time.time()
    try:
        status, data = _get_json(sc["base_url"] + "/models", sc["api_key"])
        ids = [m.get("id", "") for m in (data.get("data") or []) if isinstance(m, dict)]
        out["models_seen"] = len(ids)
        out["ok"] = status == 200
        if sc["model"] and ids and sc["model"] not in ids:
            # 端点通、但目标模型名不在列表里：部署名与配置名不一致时常见
            out["error"] = "端点可达但模型名不在列表（可能仍可用）"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        # /models 不被支持时，用一次 1-token chat 复核
        try:
            status, _ = _post_json(sc["base_url"] + "/chat/completions", {
                "model": sc["model"], "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1, "stream": False,
            }, sc["api_key"])
            out["ok"] = status == 200
            if out["ok"]:
                out["error"] = ""
        except Exception as e2:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e} / chat: {type(e2).__name__}: {e2}"
    out["latency_ms"] = int((time.time() - t0) * 1000)
    return out


def _probe_embedding(cfg: dict) -> dict:
    """按候选链探 embedding（env → config → 本机 11434）。"""
    ec = cfg.get("embedding") or {}
    model = ec.get("model") or ""
    api_key = ec.get("api_key") or ""
    import os
    cands = []
    _chain = [os.environ.get("LITREVIEW_EMBED_URL", "").rstrip("/"),
              (ec.get("base_url") or "").rstrip("/")]
    _chain += [str(u).rstrip("/") for u in (ec.get("candidates") or [])]
    for u in _chain:
        if u and u not in cands:
            cands.append(u)
    out = {"ok": False, "step": "embedding", "endpoint": "", "model": model,
           "latency_ms": 0, "error": "", "candidates_tried": 0}
    t0 = time.time()
    errs = []
    for base in cands:
        out["candidates_tried"] += 1
        try:
            status, data = _post_json(base + "/api/embed",
                                      {"model": model, "input": ["ping"]}, api_key)
            vecs = data.get("embeddings") or []
            if status == 200 and vecs and vecs[0]:
                out.update(ok=True, endpoint=base, error="",
                           dim=len(vecs[0]))
                break
            errs.append(f"{base}: 返回无向量")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{base}: {type(e).__name__}")
    if not out["ok"]:
        out["error"] = "; ".join(errs[-3:]) or "无候选端点"
    out["latency_ms"] = int((time.time() - t0) * 1000)
    return out


def probe(refresh: bool = False, cfg: dict | None = None) -> dict:
    """并发探测三类能力，返回能力表 + 阶段可行性（带 30s 缓存）。"""
    now = time.time()
    if not refresh and _CACHE["data"] and now - _CACHE["at"] < CACHE_TTL:
        d = dict(_CACHE["data"])
        d["cached"] = True
        return d

    cfg = cfg or _load_cfg()
    jobs = {
        "llm": lambda: _probe_llm("summary", cfg),        # 摘要/规范化/超边共用同一 provider
        "judge": lambda: _probe_llm("merge", cfg),        # 实体合并终审
        "embedding": lambda: _probe_embedding(cfg),
    }
    caps = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {k: ex.submit(f) for k, f in jobs.items()}
        for k, fu in futs.items():
            try:
                caps[k] = fu.result()
            except Exception as e:  # noqa: BLE001
                caps[k] = {"ok": False, "step": k, "endpoint": "", "model": "",
                           "latency_ms": 0, "error": f"{type(e).__name__}: {e}"}

    def stage(key, label, requires, note=""):
        miss = [r for r in requires if not caps.get(r, {}).get("ok")]
        return {"key": key, "label": label, "requires": requires,
                "ok": not miss, "blocked_by": miss, "note": note}

    stages = [
        stage("extract", "PDF 解析", [], "不依赖任何模型"),
        stage("index", "索引入库", [], "不依赖任何模型"),
        stage("normalize", "文本规范化", ["llm"], "LLM 不可达时回退清洗原文"),
        stage("summary", "摘要生成", ["llm"]),
        stage("hypergraph", "实体 / 超边", ["llm"]),
        stage("embedding", "向量计算", ["embedding"], "不可达则跳过，后续可补"),
        stage("entity_merge", "实体合并映射", ["judge", "llm"], "走补齐，不在单篇入库内"),
    ]

    data = {"probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cached": False, "capabilities": caps, "stages": stages,
            "timeout_s": TIMEOUT, "ttl_s": CACHE_TTL}
    _CACHE.update(at=now, data=data)
    return data


def can(stage_key: str, refresh: bool = False) -> tuple[bool, str]:
    """单阶段可行性：返回 (能不能做, 不能做的原因)。"""
    d = probe(refresh=refresh)
    for s in d["stages"]:
        if s["key"] == stage_key:
            if s["ok"]:
                return True, ""
            parts = []
            for k in s["blocked_by"]:
                c = d["capabilities"].get(k, {})
                parts.append(f"{k}: {c.get('error') or '不可达'}")
            return False, "; ".join(parts)
    return False, f"未知阶段 {stage_key}"
