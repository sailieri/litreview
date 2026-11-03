"""全局数据路径收口（唯一真相源）。

所有模块的数据目录/数据库路径从这里取，禁止再各自硬编码 data/。

优先级（统一由 modules/config.py 裁决）：
  1. 环境变量 LITREVIEW_DATA —— 临时切换（进程启动前设定）
  2. config.yaml 顶层 data_root 字段 —— 持久切换（改一行）
  3. 默认 <项目根>/data —— 向后兼容

用法：
    from modules.paths import data_root, db_path, entities_dir
    conn = sqlite3.connect(db_path())
"""
import os
import re
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # modules/ 的上一级


def _to_platform_path(p: Path) -> Path:
    """盘符路径跨平台自适应：Windows 风格 E:/x/y 在非 Windows 平台转 /mnt/<盘符>/x/y。"""
    s = str(p)
    m = re.match(r"^([A-Za-z]):[/\\](.+)$", s)
    if m and os.name != "nt":
        mnt = Path("/mnt") / m.group(1).lower() / m.group(2).replace("\\", "/")
        if mnt.exists():
            return mnt.resolve()
    return p


@lru_cache(maxsize=1)
def _root_str() -> str:
    """数据根目录：config.data_root（env LITREVIEW_DATA 已在 config 层覆盖）→ 默认 data/。"""
    try:
        from modules.config import get  # 惰性导入，避免循环依赖
        dr = str(get("data_root", "") or "").strip()
        if dr:
            p = Path(dr).expanduser()
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            return str(_to_platform_path(p))
    except Exception:
        pass  # config 缺失/损坏时回退默认
    return str(PROJECT_ROOT / "data")


def data_root() -> Path:
    """当前数据集根目录。进程启动时解析并缓存（不支持运行中切换）。"""
    return Path(_root_str())


# ── 标准子路径（与 data/ 目录布局一一对应）──
def db_path() -> Path:          return db_dir() / "papers.db"
def db_dir() -> Path:           return data_root() / "database"
def pdfs_dir() -> Path:         return data_root() / "pdfs"
def extracted_dir() -> Path:    return data_root() / "extracted"
def metadata_dir() -> Path:     return data_root() / "metadata"
def normalized_dir() -> Path:   return data_root() / "normalized"
def summaries_dir() -> Path:    return data_root() / "summaries"
def entities_dir() -> Path:     return data_root() / "entities"
def hyperedges_file() -> Path:  return data_root() / "hyperedges.json"
def entity_merge_dir() -> Path: return data_root() / "entity_merge"
def qa_dir() -> Path:           return data_root() / "qa"
