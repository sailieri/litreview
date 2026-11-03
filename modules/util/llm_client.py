"""
统一 LLM 客户端。自动根据配置选择 API 模式：

配置结构（config.yaml）:
  providers:       ← API 提供者定义（deepseek, workstation 等）
    deepseek:
      base_url: ...
      api_key: ...
      default_model: ...
    workstation:
      base_url: ...

  steps:           ← 各步骤指定用哪个 provider
    extract:
      provider: workstation
      temperature: 0.1
      ...

兼容平铺配置（llm: / extraction: / actor_critic:）：未定义 providers/steps 时按平铺段读取。
"""

import json, os, sys, time, re
from pathlib import Path

import httpx
import yaml


def load_config(config_path: str | None = None) -> dict:
    """加载配置：统一走 modules.config（唯一读者）。

    config_path 仅在显式传入时生效（测试用），否则读 modules.config 选定的文件。
    """
    from modules import config as _cfg
    if config_path:
        import yaml
        raw = Path(config_path).read_text(encoding="utf-8")
        data = yaml.safe_load(_cfg._expand_env(raw))
        return data if isinstance(data, dict) else {}
    return _cfg.load()


def _is_qwen_model(model: str) -> bool:
    return "qwen" in model.lower()


def get_step_config(step_name: str, cfg: dict | None = None) -> dict:
    """根据步骤名解析 LLM 配置。

    steps 格式: cfg.steps.<step> → 查 cfg.providers.<provider> 获取 base_url/api_key
    平铺格式: cfg.<step> 直接作为配置段（兜底）

    Returns:
        {base_url, api_key, model, params: {timeout, ...}}
    """
    if cfg is None:
        cfg = load_config()

    # steps.<step> → providers.<provider>
    steps = cfg.get("steps", {})
    step_cfg = steps.get(step_name, {})
    if step_cfg:
        provider_name = step_cfg.get("provider", "")
        provider = cfg.get("providers", {}).get(provider_name, {})
        if provider:
            # 合并：provider 提供 base_url/api_key, step 提供 model 别名 + 参数
            model_alias = step_cfg.get("model", "default")
            model = provider.get("models", {}).get(model_alias, "")
            return {
                "base_url": provider["base_url"],
                "api_key": provider["api_key"],
                "model": model,
                "params": {k: v for k, v in step_cfg.items()
                          if k in ("temperature", "max_tokens", "timeout", "reasoning_effort")},
            }

    # 平铺格式: cfg.<step_name> 直接当作配置段
    legacy = cfg.get(step_name)
    if legacy and "base_url" in legacy:
        return legacy

    # 最后兜底: cfg.llm
    llm = cfg.get("llm", {})
    if llm:
        model = llm.get("model") or llm.get("models", {}).get("fast", "")
        return {
            "base_url": llm["base_url"],
            "api_key": llm["api_key"],
            "model": model,
            "params": llm.get("params", {}),
        }

    raise ValueError(f"找不到步骤 '{step_name}' 的 LLM 配置")


def _is_retryable(exc: Exception) -> bool:
    """连接层抖动是否值得重试。

    网络抖动会表现为 `_ssl.c:983: The handshake operation timed out`
    与 `Connection reset by peer`。单次直发、无重试时，一次抖动就丢掉整篇
    （规范化同理要重跑一次），所以这里按错误类型决定是否重试。
    4xx 不重试（请求本身有问题，重试无意义）；5xx / 连接层 / 超时 才重试。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException, OSError))


def _auto_stream(base_url: str, cfg: dict) -> bool:
    """是否用流式。

    优先级：环境变量 LITREVIEW_FORCE_STREAM=1/0 > step 配置 params.stream >
            端点是否为本机（本机直连走非流式，远程端点走流式以避开网关响应时长上限）。
    端点判定只看主机名。
    """
    forced = os.environ.get("LITREVIEW_FORCE_STREAM", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    params = cfg.get("params") or {}
    if "stream" in params:
        return bool(params["stream"])
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or "").lower()
    return host not in ("127.0.0.1", "localhost", "::1", "")


def call_llm(messages: list[dict], cfg: dict, max_tokens: int = 8192,
             temperature: float = 0.1, stream: bool | None = None,
             retries: int = 3) -> str:
    """统一 LLM 调用。自动处理 Qwen thinking mode。

    Args:
        messages: OpenAI 格式消息列表
        cfg: 配置段（含 base_url, api_key, model, params）
        max_tokens: 最大输出 token
        temperature: 温度
        stream: None=自动（远程端点走流式），True=强制流式，False=禁用

    Returns:
        响应文本
    """
    base_url = cfg["base_url"].rstrip("/")
    api_key = cfg["api_key"]
    model = cfg.get("model") or cfg.get("models", {}).get("fast", "")
    timeout = cfg.get("params", {}).get("timeout", 300)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # 自动流式：远程端点默认走流式（部分网关有响应时长上限），
    # 本机端点默认非流式。判定规则可用 config 覆盖：providers.<p>.stream 或
    # 环境变量 LITREVIEW_FORCE_STREAM=1/0。
    if stream is None:
        stream = _auto_stream(base_url, cfg)
    payload["stream"] = stream

    # Qwen 需要关 thinking，否则输出混入思考过程导致 JSON 解析失败
    if _is_qwen_model(model):
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    # 思考强度（low/high/max）——从 step 配置透传；不传 = 默认档
    _eff = (cfg.get("params") or {}).get("reasoning_effort")
    if _eff:
        payload["reasoning_effort"] = _eff

    # ── 带重试的发射（连接层抖动可重试）──
    # 注意：流式请求若中途断连，已收到的片段作废、重新生成——对提取/规范化等幂等步骤是划算的
    # （重做一个比丢掉一篇便宜）。
    for _attempt in range(1, max(1, retries) + 1):
        try:
            # trust_env=False：httpx 在 Windows 会读注册表系统代理，本地请求被劫持后报 SSL 错误。
            with httpx.Client(timeout=timeout, trust_env=False) as cl:
                if not stream:
                    resp = cl.post(f"{base_url}/chat/completions", json=payload, headers=headers)
                    resp.raise_for_status()
                    return resp.json()["choices"][0]["message"]["content"].strip()

                # 流式：SSE 解析，拼 content
                parts = []
                with cl.stream("POST", f"{base_url}/chat/completions", json=payload,
                               headers=headers) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            delta = obj["choices"][0].get("delta", {})
                            piece = delta.get("content", "")
                            if piece:
                                parts.append(piece)
                        except json.JSONDecodeError:
                            continue
                return "".join(parts).strip()
        except Exception as e:            # noqa: BLE001
            if _attempt >= retries or not _is_retryable(e):
                raise
            wait = 6 * _attempt
            print(f"[llm] 第 {_attempt} 次失败（{type(e).__name__}: {str(e)[:70]}），"
                  f"{wait}s 后重试 ({_attempt}/{retries})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("call_llm: 不可达")


# ── 字段名别名归一（提取阶段第二层防护：解析时字段提醒）──
# LLM 偶发把超边正文写进 "desc" 而导入只读 "description"，导致 description 为空。
# 解析时检测别名：
# 规范字段为空 → 迁移内容（防丢失）；两键都有且不同 → 告警保留两者（不静默丢）。
FIELD_ALIASES = {"desc": "description"}
_HYPERGRAPH_LIST_KEYS = ("items", "entities", "update_entities",
                         "hyperedges", "update_hyperedges")


def normalize_field_aliases(data):
    """原地归一 LLM 输出中实体/超边列表的字段名别名，打印 [field-check] 字段提醒。"""
    if not isinstance(data, dict):
        return data
    for key in _HYPERGRAPH_LIST_KEYS:
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for alias, canonical in FIELD_ALIASES.items():
                if alias not in item:
                    continue
                alias_val = item[alias]
                canon_val = item.get(canonical)
                label = str(item.get("title") or item.get("name") or "(untitled)")[:60]
                if canon_val is None or not str(canon_val).strip():
                    item[canonical] = alias_val
                    item.pop(alias, None)
                    print(f"  [field-check] {key} '{label}': "
                          f"'{alias}' -> '{canonical}' normalized (auto-fixed)")
                elif (str(alias_val).strip()
                      and str(alias_val).strip() != str(canon_val).strip()):
                    # 两键都有内容且不同：都不丢，告警留待人工核对
                    print(f"  [field-check] WARN {key} '{label}': "
                          f"both '{alias}' and '{canonical}' present with DIFFERENT content, "
                          f"keeping both for manual review")
                else:
                    item.pop(alias, None)
                    print(f"  [field-check] {key} '{label}': redundant '{alias}' dropped")
    return data


def extract_json(raw: str) -> dict | list | None:
    """从 LLM 输出中提取 JSON（自动处理 markdown fence 和 thinking 前缀）"""
    if not raw:
        return None
    raw = raw.strip()

    # 去掉 thinking 前缀（Qwen 有时即使关了也会输出少量思考）
    if "Here's a thinking process" in raw:
        # 找最终输出：thinking 后的第一个 ``` 或 {
        parts = raw.split("Here's a thinking process")
        if len(parts) > 1:
            raw = parts[-1]
            # 去掉第一个 ``` 之前的内容
            if "```" in raw:
                raw = raw.split("```", 1)[-1]
                if raw.startswith("json"):
                    raw = raw[4:]

    # 通用移除 <thinking>…</thinking> 与 <think>…</think>
    # （防御：模型未真正关闭 thinking 时仍能正确取到 JSON）
    raw = re.sub(r"<thinking>[\s\S]*?</thinking>", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE)

    # 去掉 markdown fence
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    # 尝试解析
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试 repair
        try:
            from json_repair import repair_json
            data = json.loads(repair_json(raw))
        except Exception:
            return None
    return normalize_field_aliases(data)


class LLMClient:
    """LLM 客户端包装。适配两种配置格式。

    新配置: providers + steps → 根据 step 名解析
    平铺配置: llm 段直接提供参数
    """

    def __init__(self, config_path: str | None = None, step: str | None = None):
        cfg = load_config(config_path)

        # 按 steps 解析
        if step and "steps" in cfg:
            resolved = get_step_config(step, cfg)
            self.base_url = resolved["base_url"].rstrip("/")
            self.api_key = resolved["api_key"]
            self.params = resolved.get("params", {})
            self.models = {"fast": resolved["model"], "strong": resolved["model"]}
            return

        # 平铺格式兜底
        llm_cfg = cfg.get("llm", {})
        self.base_url = llm_cfg.get("base_url", "").rstrip("/")
        self.api_key = llm_cfg.get("api_key", "")
        self.params = llm_cfg.get("params", {})
        self.models = llm_cfg.get("models", {"fast": llm_cfg.get("model", "")})

    def chat(self, prompt: str, model: str | None = None,
             temperature: float | None = None) -> str:
        model = model or self.models.get("fast", "")
        temp = temperature if temperature is not None else self.params.get("temperature", 0.1)
        return call_llm(
            [{"role": "user", "content": prompt}],
            {"base_url": self.base_url, "api_key": self.api_key,
             "model": model, "params": self.params},
            max_tokens=self.params.get("max_tokens", 8192),
            temperature=temp,
        )

    def chat_json(self, prompt: str, model: str | None = None,
                  temperature: float | None = None, retries: int = 2) -> dict:
        for attempt in range(retries + 1):
            try:
                raw = self.chat(prompt, model, temperature)
                result = extract_json(raw)
                if isinstance(result, dict):
                    return result
            except Exception:
                if attempt < retries:
                    time.sleep(1)
                    continue
        return {"error": f"Failed after {retries + 1} attempts"}
