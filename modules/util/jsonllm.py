"""LLM 输出的 JSON 解析器。

为什么需要它：模型回复里的 JSON（工具参数、结构化输出）可能因 `max_tokens` 截断而
断在半路——`json.loads` 直接抛错，调用方往往只能吞掉或退化成空 dict，**静默丢内容**。

策略：先 `json.loads`（严格路径，正常情况零开销）；失败再用 `json_repair` 修复
（能补全未闭合的字符串/括号、容忍尾随逗号、单引号、markdown 代码块包裹等）。

约定：JSON 解析一律走这里；被截断的只能是真实内容，
先解析、再按需重组，不要用正则手搓字段。
"""
import json

try:                                    # 可选依赖：缺失时退化为 json.loads + 默认值
    from json_repair import loads as _repair_loads
except Exception:                       # pragma: no cover
    _repair_loads = None


def loads_llm(raw, default=None):
    """解析 LLM 输出的 JSON 文本；失败返回 default（默认 {})。

    永不抛异常——调用方拿到的至少是一个 dict/list 或 default。
    """
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None or not str(raw).strip():
        return {} if default is None else default
    try:
        return json.loads(raw)
    except Exception:
        pass
    if _repair_loads is not None:
        try:
            out = _repair_loads(raw)
            if isinstance(out, (dict, list)):
                return out
        except Exception:
            pass
    return {} if default is None else default


def repair_available() -> bool:
    """当前解释器是否有 json_repair（litreview 各环境均已装，见 requirements*.txt）。"""
    return _repair_loads is not None
