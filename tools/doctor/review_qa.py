"""综述质量检查：对生成的综述 markdown 跑一组通用写作规范检查，输出报告。
检查项均为通用写作规范：

  1. 标题语言：章节/小标题是否含中英混合（应纯中文，缩写如 P2P 可保留）。
  2. 机械收束：'综上所述/总而言之/综上' 等套路化章末收束出现次数（建议多样化）。
  3. 指令腔：'必须严格/需严格/应当明确/避免混淆/严禁/务必' 等生硬表述残留。
  4. 公式密度：是否含数学/记号特征（Σ ∑ $ \\frac \\begin equation），判断是否需要补核心公式。
  5. 章节结构：列出所有 ## 章节标题与近似长度。

用法：
  python tools/doctor/doctor.py review [path/to/review.md]
  python tools/doctor/doctor.py review data/reviews/final_review.md
"""
import sys, re, argparse
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.review.paths import CURRENT_FINAL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(CURRENT_FINAL),
                    help="待检查综述 markdown 路径（默认 final/final_review.md）")
    args = ap.parse_args()
    f = Path(args.path)
    if not f.exists():
        print(f"[ERR] 找不到 {f}")
        return
    txt = f.read_text(encoding="utf-8")

    print("=" * 60)
    print(f"文件：{f.name}  ({len(txt)} 字符)")
    print("=" * 60)

    # 1) 标题语言
    headings = re.findall(r"^(#{1,4})\s+(.+)$", txt, re.M)
    mixed = [(lv, t.strip()) for lv, t in headings
             if re.search(r"[A-Za-z]", t) and not re.fullmatch(r"[^一-鿿]*[A-Za-z0-9]+[^一-鿿]*", t)]
    print(f"\n[标题] 共 {len(headings)} 个；可能中英混合的：")
    for lv, t in mixed:
        print(f"  {'#'*len(lv)} {t}")
    if not mixed:
        print("  无（纯中文）")

    # 2) 机械收束
    closings = ["综上所述", "总而言之", "综上", "综上所述，", "总的来看", "总体来看"]
    cc = {c: txt.count(c) for c in closings}
    total_close = sum(cc.values())
    print(f"\n[收束] 套路化章末收束共 {total_close} 处：{cc}")

    # 3) 指令腔
    inst = [p for p in ["必须严格", "需严格", "应当明确", "避免混淆", "严禁", "务必", "切不可"]
            if p in txt]
    print(f"[指令腔] 残留生硬表述：{inst if inst else '无'}")

    # 4) 公式密度
    formula_hits = len(re.findall(
        r"\$|∑|∏|∫|≈|≤|≥|\\frac|\\begin|节点方程|守恒方程|notation|Σ", txt))
    print(f"[公式] 数学/记号特征命中 {formula_hits} 处")

    # 5) 章节结构
    chaps = re.findall(r"^## (\d+)\.\s*(.+)$", txt, re.M)
    print(f"\n[章节] 共 {len(chaps)} 章：")
    for num, t in chaps:
        m = re.search(rf"^## {num}\..*$", txt, re.M)
        start = m.start()
        nm = re.search(rf"^## {int(num)+1}\.\s", txt, re.M)
        end = nm.start() if nm else len(txt)
        print(f"  {num}. {t.strip()}  (~{end-start} 字符)")
    print("=" * 60)


if __name__ == "__main__":
    main()
