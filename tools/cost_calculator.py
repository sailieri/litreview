#!/usr/bin/env python3
"""cost_calculator.py —— litreview 全流程成本计算器。

本管线是重上下文任务（主智能体累积历史 + kb_qa 全库题录注入 + 全文规范化重写），
一次「建库 + 3 轮综述」开销可观。跑批前先估算成本，避免实际支出超出预期。

token 模型口径（取自一次建库 + 3 轮综述的运行记录）：
  - 产物口径：extracted 均值 78.5k 字符/篇、normalized 均值 50.8k（≈5 万截断）、
    summary 2.1k 字符、entities+hyperedges JSON 29.8k 字符、定稿 44k→62k 字符
  - 调用计数：budget.json（kb_qa 16+17+3、probe 2+3、review 各 1）、
    agent ~30 iter/轮、kb 单上下文 85k token（前缀命中 82.9%）
  - 换算系数：英文 4 字符/token、中文 3 字符/token、JSON 3.5 字符/token
  - 不确定度 ±30%（换算系数与 kb_qa 内部 loop 平均上下文是估算项）

用法：
  python tools/cost_calculator.py                          # 默认：flash·空闲·50 篇·全 DeepSeek
  python tools/cost_calculator.py --papers 100             # 改篇数（缺省自动查库，查不到用 50）
  python tools/cost_calculator.py --model pro              # deepseek-v4-pro
  python tools/cost_calculator.py --peak                   # 高峰时段价（工作日 9-12/14-18 点）
  python tools/cost_calculator.py --profile current        # 当前 config 混合口径（部分步骤走远程端点）
  python tools/cost_calculator.py --price-miss 2 --price-hit 0.04 --price-out 8   # 自定义三件套
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


_PROJECT = Path(__file__).resolve().parent.parent

# ─────────────────────────────────────────────────────────────
# token 模型（单位：token）。四个数依次 = 输入未命中 / 输入缓存命中 / 输出，
# 最后一个布尔 = 是否走付费 API（False 的组件在 current 口径下走本地部署的推理端点，不计费）。
# ─────────────────────────────────────────────────────────────

# 建库：每篇 5 次 LLM 调用，严格随篇数线性
# （on_ds=False = 该步骤在 current 口径下走本地部署端点；cloud 口径下全部按付费 API 计费）
LIBRARY_STEPS = [
    # (步骤, 输入miss, 输入hit, 输出, deepseek?)
    ("① 元数据提取",          1_000,      0,     150, False),
    ("② 正文规范化（全文重写）", 20_000,     0,  12_700, False),
    ("③ 叙事摘要",            13_000,      0,     700, False),
    ("④ 实体提取",            14_000, 12_700,   5_000, False),
    ("⑤ 超边提取",             2_000, 18_000,   3_500, False),
]

# 综述：3 轮全流程，与篇数基本无关（检索有截断、上下文有上限）
REVIEW_COMPONENTS = [
    # (组件, 输入miss, 输入hit, 输出, deepseek?)
    ("main_agent 主智能体（×93 iter）",   380_000, 5_600_000,  90_000, True),
    ("kb_qa 库调研（×36 次）",         1_300_000, 5_200_000, 133_000, False),
    ("审稿团队（6 persona ×3 轮）",      580_000,         0,  36_000, False),
    ("compile 定稿转写（×3）",            63_000,         0,  52_000, True),
    ("probe 追问（×5）",                 110_000,         0,  15_000, False),
    ("gap_audit 断言核实（×2）",          30_000,         0,   6_000, False),
]

# 价格预设（元 / 百万 token），取自官方定价页；改价时在此更新。
# 高峰 = 北京时间周一至周五 9:00-12:00 / 14:00-18:00，价格为空闲档 ×2。
PRESETS = {
    "deepseek-flash": {
        "off":  {"miss": 1.0, "hit": 0.02, "out": 4.0},
        "peak": {"miss": 2.0, "hit": 0.04, "out": 8.0},
    },
    "deepseek-v4-pro": {
        "off":  {"miss": 4.5, "hit": 0.15, "out": 13.5},
        "peak": {"miss": 9.0, "hit": 0.30, "out": 27.0},
    },
}

DEFAULT_PAPERS = 50


def count_papers() -> int | None:
    """从当前 data_root 的 papers.db 查论文数；失败返回 None。"""
    try:
        sys.path.insert(0, str(_PROJECT))
        from modules.paths import db_path
        conn = sqlite3.connect(str(db_path()))
        n = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return None


def _component_cost(miss, hit, out, price):
    return miss / 1e6 * price["miss"] + hit / 1e6 * price["hit"] + out / 1e6 * price["out"]


def estimate(papers: int, price: dict, profile: str = "cloud") -> dict:
    """计算全流程成本。profile: cloud=全 DeepSeek；current=当前 config 混合口径。

    返回 {"library": {...}, "review": {...}, "total": float, "details": [...]}
    """
    details = []

    lib_miss = lib_hit = lib_out = 0
    lib_cost = 0.0
    for name, m, h, o, on_ds in LIBRARY_STEPS:
        c = _component_cost(m, h, o, price) if (on_ds or profile == "cloud") else 0.0
        lib_miss += m; lib_hit += h; lib_out += o; lib_cost += c
        details.append({"phase": "library", "name": name, "miss": m * papers,
                        "hit": h * papers, "out": o * papers, "cost": c * papers,
                        "workstation": not (on_ds or profile == "cloud")})
    lib_miss *= papers; lib_hit *= papers; lib_out *= papers; lib_cost *= papers

    rev_miss = rev_hit = rev_out = 0
    rev_cost = 0.0
    for name, m, h, o, on_ds in REVIEW_COMPONENTS:
        billed = on_ds or profile == "cloud"
        c = _component_cost(m, h, o, price) if billed else 0.0
        rev_miss += m; rev_hit += h; rev_out += o; rev_cost += c
        details.append({"phase": "review", "name": name, "miss": m, "hit": h,
                        "out": o, "cost": c, "workstation": not billed})
    return {
        "papers": papers,
        "profile": profile,
        "library": {"miss": lib_miss, "hit": lib_hit, "out": lib_out, "cost": lib_cost},
        "review": {"miss": rev_miss, "hit": rev_hit, "out": rev_out, "cost": rev_cost},
        "total": lib_cost + rev_cost,
        "details": details,
    }


def _fmt_tokens(n) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def render(res: dict, preset_name: str, slot: str) -> str:
    price = PRESETS[preset_name][slot]
    lines = []
    bar = "═" * 68
    lines.append(bar)
    lines.append(f"  litreview 成本估算 · {res['papers']} 篇 · "
                 f"{'全程 ' + preset_name if res['profile'] == 'cloud' else '当前混合口径(部分走远程端点)'}"
                 f" · {'高峰' if slot == 'peak' else '空闲'}档")
    lines.append(bar)
    lines.append("")
    lines.append("  ⚠️  跑前必读：本管线重上下文（主智能体累积历史 + 全库题录注入 +")
    lines.append("      全文规范化重写），一次全流程是真实开销。工作日 9-12/14-18 点")
    lines.append("      为高峰档，价格 ×2。各组件口径与常数见文件头注释。")
    lines.append("")

    lines.append("【建库】每篇 5 次调用，随篇数线性")
    lines.append(f"  {'步骤':<14}{'输入miss':>10}{'输入hit':>10}{'输出':>10}{'小计':>10}")
    for d in res["details"]:
        if d["phase"] != "library":
            continue
        tag = "  (远程端点)" if d["workstation"] else ""
        lines.append(f"  {d['name']:<14}{_fmt_tokens(d['miss']):>10}"
                     f"{_fmt_tokens(d['hit']):>10}{_fmt_tokens(d['out']):>10}"
                     f"{'—' if d['workstation'] else '¥%.3f' % d['cost']:>10}{tag}")
    lib = res["library"]
    lines.append(f"  {'建库小计':<14}{_fmt_tokens(lib['miss']):>10}"
                 f"{_fmt_tokens(lib['hit']):>10}{_fmt_tokens(lib['out']):>10}"
                 f"  ¥{lib['cost']:.2f}")
    lines.append("")
    lines.append("【综述 3 轮】与篇数基本无关（kb 检索截断 + 上下文上限）")
    lines.append(f"  {'组件':<26}{'输入miss':>10}{'输入hit':>10}{'输出':>10}{'小计':>10}")
    for d in res["details"]:
        if d["phase"] != "review":
            continue
        tag = "  (远程端点)" if d["workstation"] else ""
        lines.append(f"  {d['name']:<26}{_fmt_tokens(d['miss']):>10}"
                     f"{_fmt_tokens(d['hit']):>10}{_fmt_tokens(d['out']):>10}"
                     f"{'—' if d['workstation'] else '¥%.2f' % d['cost']:>10}{tag}")
    rev = res["review"]
    lines.append(f"  {'综述小计':<26}{_fmt_tokens(rev['miss']):>10}"
                 f"{_fmt_tokens(rev['hit']):>10}{_fmt_tokens(rev['out']):>10}"
                 f"  ¥{rev['cost']:.2f}")
    lines.append("")
    lines.append(f"  ╔══════════════════════════════════════════════════╗")
    lines.append(f"  ║   全流程总计：¥{res['total']:.2f}（不确定度 ±30%）")
    lines.append(f"  ╚══════════════════════════════════════════════════╝")
    lines.append(f"  建库 ≈ ¥{lib['cost']:.2f}（≈¥{lib['cost'] / max(res['papers'], 1):.3f}/篇，线性）"
                 f"  综述 ≈ ¥{rev['cost']:.2f}（基本固定）")
    lines.append(f"  价格档：miss ¥{price['miss']}/M · hit ¥{price['hit']}/M · out ¥{price['out']}/M")
    return "\n".join(lines)


def print_estimate_for_agent():
    """供 main_agent.py 在 preflight 后调用：打印两行成本预估（混合 + 全 DeepSeek）。

    静默失败——成本提示永远不该挡住主流程。
    """
    try:
        n = count_papers() or DEFAULT_PAPERS
        price = PRESETS["deepseek-flash"]["off"]
        cloud = estimate(n, price, "cloud")
        cur = estimate(n, price, "current")
        print(f"\n💰 [成本预估·{n} 篇] 本轮综述 + 已建库全流程：")
        print(f"   当前混合口径（kb/审稿/probe 走远程端点）≈ ¥{cur['total']:.2f}")
        print(f"   全程 DeepSeek 口径（若切换 provider）≈ ¥{cloud['total']:.2f}（高峰档 ×2）")
        print(f"   明细：python tools/cost_calculator.py --papers {n}\n")
    except Exception as e:
        print(f"[cost] 预估跳过（{e}）")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    ap = argparse.ArgumentParser(description="litreview 全流程成本计算器")
    ap.add_argument("--papers", type=int, default=None, help="论文篇数（缺省查 papers.db，查不到用 50）")
    ap.add_argument("--model", choices=["flash", "pro"], default="flash",
                    help="deepseek-flash（默认）或 deepseek-v4-pro")
    ap.add_argument("--peak", action="store_true", help="高峰时段价（工作日 9-12/14-18 点，×2）")
    ap.add_argument("--profile", choices=["cloud", "current"], default="cloud",
                    help="cloud=全程 DeepSeek；current=当前 config 混合口径（部分步骤走远程端点）")
    ap.add_argument("--price-miss", type=float, default=None, help="自定义输入未命中价（元/M token）")
    ap.add_argument("--price-hit", type=float, default=None, help="自定义输入缓存命中价（元/M token）")
    ap.add_argument("--price-out", type=float, default=None, help="自定义输出价（元/M token）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供脚本调用）")
    args = ap.parse_args()

    papers = args.papers if args.papers is not None else (count_papers() or DEFAULT_PAPERS)
    if args.papers is None and count_papers() is None:
        print(f"[i] 未能连接 papers.db，按默认 {DEFAULT_PAPERS} 篇估算\n")

    preset_name = "deepseek-flash" if args.model == "flash" else "deepseek-v4-pro"
    slot = "peak" if args.peak else "off"
    if args.price_miss is not None or args.price_hit is not None or args.price_out is not None:
        base = PRESETS[preset_name][slot]
        price = {"miss": args.price_miss if args.price_miss is not None else base["miss"],
                 "hit": args.price_hit if args.price_hit is not None else base["hit"],
                 "out": args.price_out if args.price_out is not None else base["out"]}
        PRESETS["custom"] = {slot: price}
        preset_name, slot = "custom", slot

    res = estimate(papers, PRESETS[preset_name][slot], args.profile)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render(res, preset_name, slot))

    # 附：另一种口径的单行对照
    other = "current" if args.profile == "cloud" else "cloud"
    res2 = estimate(papers, PRESETS[preset_name][slot], other)
    label = {"cloud": "全程 DeepSeek", "current": "当前混合口径（kb/审稿/probe/gap 走远程端点）"}
    print(f"\n[i] 对照口径 · {label[other]}：¥{res2['total']:.2f}")


if __name__ == "__main__":
    main()
