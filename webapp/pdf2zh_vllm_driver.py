"""pdf2zh_next 直驱 vLLM 薄驱动（monkey-patch，不改上游源码）。

设计：
  1. 全文做前缀：抽输入 PDF 全文 → --custom-system-prompt（role_block 打头，
     同篇所有片段请求前缀逐字节一致 → vLLM prefix cache 命中；片段 JSON 在尾部，
     「输入全文但只译一部分」——模型读到全部语义）
  2. 关思考：OpenAITranslator.extra_body 注入 chat_template_kwargs.enable_thinking=false
  3. 前缀热身：正式翻译前发一个同前缀小请求，把全文 prefill 灌进缓存

⚠ Windows multiprocessing spawn 会重新 import 本模块（子进程才是真正翻译的地方）：
  - patch 必须放【模块级】→ 子进程 import 时同样生效
  - 实际执行放 main() + __main__ 守卫 → 子进程 import 时不会重跑 CLI
    （缺守卫会导致子进程重跑 CLI，报 bootstrapping 错误）

用法（pdf2zh_next CLI 参数原样，无需自己传 --custom-system-prompt）：
    <venv>/python pdf2zh_vllm_driver.py input.pdf --openaicompatible \
        --openai-compatible-base-url ... --openai-compatible-api-key ... \
        --openai-compatible-model ... --qps 16 --output ... --lang-in en ...
"""
import sys

CTX_CAP = 150_000          # 全文前缀上限（字符）。常规论文 4-6 万，极少触顶
WARMUP = True
# 纯逐块翻译，不加全文前缀：长上下文会把每字 KV 读取量拉满，并行收益被带宽抵消，
# 前缀命中率也有限。
FULLTEXT_PREFIX = False


# ── 关思考 patch（模块级调用：父进程 + spawn 子进程都生效） ───────
def _patch_thinking_off():
    try:
        from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator
    except ImportError as e:
        print(f"[driver] patch skipped: {e}", file=sys.stderr)
        return
    if getattr(OpenAITranslator.__init__, "_litread_patched", False):
        return

    _orig_init = OpenAITranslator.__init__

    def __init__(self, settings, rate_limiter):
        _orig_init(self, settings, rate_limiter)
        eb = dict(self.options.get("extra_body") or {})
        eb.setdefault("chat_template_kwargs", {"enable_thinking": False})
        self.options["extra_body"] = eb
        try:
            self.add_cache_impact_parameters("extra_body", eb)
        except Exception:
            pass

    __init__._litread_patched = True
    OpenAITranslator.__init__ = __init__
    print("[driver] thinking-off ON (chat_template_kwargs.enable_thinking=false)",
          file=sys.stderr)


_patch_thinking_off()   # 模块级：spawn 子进程 re-import 本模块时同样打上


# ── 阶段时间戳探针（默认开启，PDF2ZH_TS=0 关闭） ──────────
# 生产进度接口的数据源：前端节点进度实时解析这些行。
# 默认开启而非 env 门控：父进程的环境变量不一定能透传到 pdf2zh 子进程，
# 门控一旦失效就是静默丢进度。
# 原理：babeldoc 事件流是唯一能拿到阶段转换的地方（Rich 进度条在非 TTY
# 下只在结束时渲染一次，日志里没有中间过程）；perf_counter 跨进程同源，
# 父子进程事件可对齐到同一时间轴。
def _patch_ts():
    import os
    import time as _t
    if os.environ.get("PDF2ZH_TS") == "0":
        return
    print(f"[TS0] perf={_t.perf_counter():.3f} driver-import pid={os.getpid()}",
          file=sys.stderr, flush=True)
    try:
        import pdf2zh_next.high_level as hl
    except Exception as e:  # noqa: BLE001
        print(f"[TS] patch skipped: {e}", file=sys.stderr)
        return
    orig = getattr(hl, "babeldoc_translate", None)
    if orig is None or getattr(orig, "_litread_ts", False):
        return

    def wrapped(*a, **k):
        t0 = _t.perf_counter()
        gen = orig(*a, **k)

        async def timed():
            async for ev in gen:
                now = _t.perf_counter()
                print(f"[TS +{now - t0:7.2f}s] pid={os.getpid()} {str(ev)[:400]}",
                      file=sys.stderr, flush=True)
                yield ev

        return timed()

    wrapped._litread_ts = True
    hl.babeldoc_translate = wrapped
    print(f"[TS] event probe armed pid={os.getpid()} t0={_t.perf_counter():.3f}",
          file=sys.stderr, flush=True)


_patch_ts()   # 模块级：同上，子进程也打上


# ── 全文前缀 ─────────────────────────────────────────────────
def _extract_fulltext(pdf_path: str) -> str:
    try:
        import fitz  # pymupdf（venv 自带）
        doc = fitz.open(pdf_path)
        parts = [page.get_text() for page in doc]
        doc.close()
        text = "\n\n".join(p for p in parts if p and p.strip())
    except Exception as e:  # noqa: BLE001
        print(f"[driver] fulltext extract failed: {e}", file=sys.stderr)
        return ""
    return text


def _build_context_prompt(fulltext: str) -> str:
    return (
        "You are translating an academic paper from English into Simplified Chinese "
        "(zh-CN).\n\n"
        "=== FULL DOCUMENT (context only — for semantic understanding; do NOT "
        "translate this whole block) ===\n"
        f"{fulltext}\n"
        "=== END FULL DOCUMENT ===\n\n"
        "You will receive only a SEGMENT of this document as JSON input below. "
        "Translate ONLY that segment into fluent Simplified Chinese, consistent "
        "with the terminology of the full document above. Keep placeholders, tags "
        "and numbers unchanged.\n\n"
        "Follow all rules strictly."
    )


# ── 前缀热身（把全文 prefill 灌进 vLLM prefix cache） ──────────
def _warmup(base_url: str, key: str, model: str, prefix: str):
    import httpx

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prefix}],
        "max_tokens": 4,
        "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        r = httpx.post(
            base_url.rstrip("/") + "/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=180.0,
            trust_env=False,          # 禁系统代理（避免本地代理劫持请求）
        )
        r.raise_for_status()
        usage = r.json().get("usage", {})
        print(
            f"[driver] warmup OK: prompt={usage.get('prompt_tokens')} "
            f"cache_hit={usage.get('prompt_cache_hit_tokens', usage.get('cached_tokens', '?'))}",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[driver] warmup failed (continue anyway): {e}", file=sys.stderr)


def _argv_val(argv: list[str], flag: str) -> str | None:
    try:
        i = argv.index(flag)
        return argv[i + 1] if i + 1 < len(argv) else None
    except ValueError:
        return None


def main():
    argv = sys.argv[1:]
    # 输入 PDF = 第一个非 flag 参数
    input_pdf = next((a for a in argv if not a.startswith("-")), None)

    if input_pdf and FULLTEXT_PREFIX and _argv_val(argv, "--custom-system-prompt") is None:
        fulltext = _extract_fulltext(input_pdf)
        if len(fulltext) >= 300:
            prompt = _build_context_prompt(fulltext[:CTX_CAP])
            argv = ["--custom-system-prompt", prompt] + argv
            print(f"[driver] fulltext prefix ON ({len(fulltext)} chars, "
                  f"capped at {CTX_CAP})", file=sys.stderr)

            if WARMUP:
                base = _argv_val(argv, "--openai-compatible-base-url")
                key = _argv_val(argv, "--openai-compatible-api-key")
                model = _argv_val(argv, "--openai-compatible-model")
                if base and key and model:
                    _warmup(base, key, model, prompt)
        else:
            print(f"[driver] fulltext too short ({len(fulltext)}), "
                  "use default prompt", file=sys.stderr)

    sys.argv = ["pdf2zh_next"] + argv
    from pdf2zh_next.main import cli
    cli()


if __name__ == "__main__":
    main()
