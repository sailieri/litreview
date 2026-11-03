# -*- coding: utf-8 -*-
"""配置访问唯一入口（唯一真相源）。

**规矩**：除本模块外，任何模块/脚本**禁止**直接读 config.yaml、
禁止硬编码 URL / 端口 / 密钥 / 绝对路径。需要什么就 `from modules.config import get`。

用法：
    from modules.config import get, section, path_of, require

    base = get("embedding.base_url", default="")
    prov = section("providers")
    tmp  = path_of("paths.tmp_dir")          # 相对路径自动按仓库根解析
    key  = require("providers.deepseek.api_key")

优先级（高 → 低）：
    1. 环境变量覆盖（见 ENV_OVERRIDES，只覆盖少量高频项）
    2. config.yaml（`${VAR}` / `${VAR:-默认}` 会展开成环境变量值）
    3. DEFAULTS（本文件底部，唯一允许写默认值的地方）

配置文件位置：`<仓库根>/config.yaml`，可用环境变量 LITREVIEW_CONFIG 指向别处。
"""
from __future__ import annotations

import copy
import os
import sys
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV = "LITREVIEW_CONFIG"

# 环境变量 → 配置键（少量高频项；其余一律走 config.yaml）
ENV_OVERRIDES = {
    "LITREVIEW_DATA": "data_root",
    "LITREVIEW_EMBED_URL": "embedding.base_url",
    "LITREVIEW_TMP": "paths.tmp_dir",
}

# 唯一允许出现字面默认值的地方
DEFAULTS: dict = {
    # 数据根目录；空 = <仓库根>/data
    "data_root": "",
    "embedding": {
        "base_url": "",              # OpenAI 兼容 /api/embed 端点；空则直接用 candidates
        "api_key": "",
        "model": "",
        "dim": 1024,
        # 候选链：按顺序探活，命中的会被缓存复用
        "candidates": ["http://127.0.0.1:11434"],
    },
    "judge": {                        # 实体分簇的 LLM 判官（可选，缺省停用）
        "base_url": "",
        "api_key": "",
        "workers": 16,
        "instruct": (
            "Are term1 and term2 different spellings of the same entity? "
            "Answer true only for abbreviations, full forms, plurals, case or "
            "punctuation differences. Answer false for related but distinct "
            "concepts, parent-child terms, and names that differ by numbers, "
            "ranges or qualifiers."
        ),
    },
    "endpoints": {                    # 内部服务地址（直连地址 / 远程网关地址）
        "workstation_lan": "",
        "workstation_cf": "",
        "workstation_lan_idx": "",
        "workstation_cf_idx": "",
    },
    "webapp": {
        "host": "0.0.0.0",
        "port": 8790,
        "mcp_host": "0.0.0.0",
        "mcp_port": 8791,
        "mcp_token": "",
        "translate_engine": "wsidx",
    },
    "sources": {                      # 第三方文献/元数据 API（可换成镜像）
        "openalex": "https://api.openalex.org",
        "crossref": "https://api.crossref.org",
        "unpaywall": "https://api.unpaywall.org",
        "semanticscholar": "https://api.semanticscholar.org",
        "arxiv_api": "http://export.arxiv.org/api/query",
        "arxiv_pdf": "https://arxiv.org/pdf",
        "doi": "https://doi.org",
        "arxiv_search": "https://arxiv.org/list/cs.CV/recent?search_type=all&query=",
        "google_scholar": "https://scholar.google.com/scholar?q=",
        "mailto": "litreview@example.org",
        "proxy": "",                  # 如 http://127.0.0.1:<端口>；空 = 不走代理
    },
    "paths": {                        # 可选路径覆盖；空 = 用默认位置
        "tmp_dir": "",
        "pdf2zh_python": "",
        "pdfs_dir": "",
    },
    "providers": {},
    "steps": {},
}


def config_path() -> Path:
    env = os.environ.get(CONFIG_ENV, "").strip()
    return Path(env) if env else (PROJECT_ROOT / "config.yaml")


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
    """展开 ${VAR} 与 ${VAR:-默认}；变量不存在且无默认时原样保留。"""
    def sub(m):
        name, dflt = m.group(1), m.group(2)
        val = os.environ.get(name)
        if val is not None:
            return val
        return dflt if dflt is not None else m.group(0)
    return _ENV_RE.sub(sub, text)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_cache: dict | None = None
_cache_path: Path | None = None


# 这些键为空是合法状态（空 = 不启用/不鉴权），不因未设环境变量而报警
_OPTIONAL_EMPTY = {"webapp.mcp_token", "endpoints.workstation_lan",
                  "endpoints.workstation_cf", "judge.api_key"}


def _warn_unresolved(cfg: dict) -> None:
    """配置里残留 ${VAR} 说明环境变量没设；早提示，比后面 401 好定位。"""
    missing: list[str] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            for m in _ENV_RE.finditer(node):
                if m.group(2) is None:      # 无默认值 → 原样保留，肯定没展开
                    if path not in _OPTIONAL_EMPTY:
                        missing.append(path)

    walk(cfg)
    if missing:
        uniq = sorted(set(missing))
        head = "、".join(uniq[:6]) + (" 等" if len(uniq) > 6 else "")
        print(f"[config] 警告：{head} 里的 ${{VAR}} 未展开（环境变量未设）；"
              "请设环境变量或直接写值，否则相关调用会报鉴权失败。", file=sys.stderr)


def load(reload: bool = False) -> dict:
    """加载并合并配置（带缓存）。外部改完 config.yaml 可 load(reload=True)。"""
    global _cache, _cache_path
    p = config_path()
    if _cache is not None and not reload and _cache_path == p:
        return _cache

    data: dict = {}
    if p.exists():
        raw = _expand_env(p.read_text(encoding="utf-8"))
        try:
            import yaml
            loaded = yaml.safe_load(raw)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"config.yaml 解析失败（{p}）：{e}") from e
        # 铁律：yaml 对垃圾输入可能返回 str/list 而不抛异常
        if isinstance(loaded, dict):
            data = loaded
        elif loaded is not None:
            raise RuntimeError(f"config.yaml 顶层必须是映射，实际是 {type(loaded).__name__}")

    merged = _deep_merge(DEFAULTS, data)

    # 环境变量覆盖（点号路径）
    for env_name, dotted in ENV_OVERRIDES.items():
        val = os.environ.get(env_name)
        if val:
            _set_dotted(merged, dotted, val)

    _cache, _cache_path = merged, p
    _warn_unresolved(merged)
    return merged


def _set_dotted(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def get(dotted: str, default=None):
    """按点号路径取值：get("embedding.dim", 1024)。"""
    cur = load()
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


def section(name: str) -> dict:
    v = get(name, {})
    return v if isinstance(v, dict) else {}


def require(dotted: str, hint: str = ""):
    """必填项：缺失直接报错，错误信息指到 config.yaml。"""
    val = get(dotted)
    if val in (None, "", {}, []):
        msg = f"配置缺失：{dotted}（{config_path()}）"
        if hint:
            msg += f"\n  → {hint}"
        raise RuntimeError(msg)
    return val


def path_of(dotted: str, default: Path | None = None) -> Path:
    """取路径并解析：相对路径按仓库根展开，支持 ~。空值返回 default。"""
    raw = get(dotted)
    if not raw:
        return default if default is not None else Path("")
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def relpath_of(dotted: str) -> str:
    """取路径的字符串形式（保留空值），常用于传给子进程。"""
    raw = get(dotted)
    if not raw:
        return ""
    return str(path_of(dotted))
