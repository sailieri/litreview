"""元数据去重模块——按论文标题规范化匹配，识别重复收录的 PDF。

设计要点：
1. 标题规范化：小写、去标点、压缩空白，保留中英文
2. 占位标题防护：'full paper title' / 'unknown' / 空标题 不能作为去重键
   （不同论文的元数据标题可能全是 'Full paper title' 这类提取失败占位符，
    不防护会把它们误判为一组）
3. 只做精确匹配 + 可选模糊匹配（difflib > 阈值），避免误删
4. dry-run 默认，--delete 才实际删除

用法：
  python modules/util/deduplicate.py --meta-dir data/metadata          # 只看报告
  python modules/util/deduplicate.py --meta-dir data/metadata --delete  # 删除重复
"""
import argparse, json, os, re, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from modules.paths import metadata_dir

# 占位标题（元数据提取失败时会写入这些值）
PLACEHOLDER_TITLES = {
    "", "unknown", "n/a", "na", "none", "null", "-",
    "full paper title", "title", "untitled", "no title",
    "nan", "not available", "tbd",
}


def normalize_title(title: str) -> str | None:
    """规范化标题；占位/空返回 None（不可作为去重键）。"""
    if not title:
        return None
    t = title.strip().lower()
    if t in PLACEHOLDER_TITLES:
        return None
    # 去标点（保留字母数字中文），压缩空白
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    t = re.sub(r"\s+", "", t)
    return t or None


def is_bad_filename(name: str) -> bool:
    """文件名含 URL 编码残留（+、%）或纯数字开头 = 非规范命名。"""
    base = os.path.basename(name)
    if "+" in base or "%" in base:
        return True
    return bool(re.match(r"^\d+", base))


def find_duplicates(meta_dir, fuzzy_threshold: float = 0.95) -> list[list[dict]]:
    """扫描 metadata 目录，返回重复组列表（组内为文件信息 dict）。"""
    meta_dir = Path(meta_dir)
    files = sorted(meta_dir.glob("*.json"))
    groups = defaultdict(list)
    skipped_placeholder = []

    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            title = d.get("title", "")
            authors = d.get("authors", "")
            year = d.get("year")
        except Exception:
            continue
        key = normalize_title(title)
        if key is None:
            skipped_placeholder.append(f.name)
            continue
        groups[key].append({
            "file": f.name,
            "title": title,
            "authors": str(authors)[:60],
            "year": year,
        })

    dup_groups = [v for v in groups.values() if len(v) > 1]

    # 模糊匹配：精确分组后再合并标题高度相似且作者重叠的组（保守，默认 0.95）
    if fuzzy_threshold < 1.0 and len(dup_groups) > 1:
        merged = []
        used = set()
        keys = list(groups.keys())
        for i, k in enumerate(keys):
            if k in used or k not in groups:
                continue
            cluster = list(groups[k])
            used.add(k)
            for j in range(i + 1, len(keys)):
                k2 = keys[j]
                if k2 in used or k2 not in groups:
                    continue
                # 标题相似 + 作者有交集才合并（保守）
                from difflib import SequenceMatcher
                r = SequenceMatcher(None, k, k2).ratio()
                if r >= fuzzy_threshold and _authors_overlap(groups[k], groups[k2]):
                    cluster.extend(groups[k2])
                    used.add(k2)
            merged.append(cluster)
        dup_groups = [c for c in merged if len(c) > 1]

    return dup_groups


def _authors_overlap(g1: list[dict], g2: list[dict]) -> bool:
    def names(g):
        s = set()
        for item in g:
            for a in re.split(r"[;,，、]", item["authors"]):
                a = a.strip().lower()
                if len(a) > 1:
                    s.add(a)
        return s
    return len(names(g1) & names(g2)) > 0


def pick_keep(group: list[dict]) -> dict:
    """保留策略：非 URL 编码名优先；再按文件名标题与元数据 title 的相似度，
    相似度高者优先（文件名与内容一致的更可信，防止文件名错乱的反向保留）。"""
    good = [g for g in group if not is_bad_filename(g["file"])]
    pool = good or group

    from difflib import SequenceMatcher

    def score(g):
        base = os.path.basename(g["file"])[:-5]  # 去 .json
        # 提取文件名中的标题部分（去掉 "作者 - 年份 - " 前缀）
        m = re.match(r"^[^\-]+ - \d{4} - (.*)$", base)
        fn_title = m.group(1) if m else base
        return SequenceMatcher(None, fn_title.lower(), g["title"].lower()).ratio()

    return max(pool, key=score)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta-dir", default=str(metadata_dir()))
    ap.add_argument("--delete", action="store_true", help="实际删除重复文件")
    ap.add_argument("--fuzzy", type=float, default=0.95, help="模糊匹配阈值(0-1)，1=只精确")
    ap.add_argument("--scan-db", action="store_true",
                    help="全库体检：papers 表两两比对（DOI/标题）找重复，只报不删")
    args = ap.parse_args()

    # 控制台编码不支持 ✗/✓ 时会 UnicodeEncodeError，统一按 UTF-8 输出
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.scan_db:
        from modules.util.dedup_gate import find_db_duplicates
        db_groups = find_db_duplicates(fuzzy_threshold=args.fuzzy)
        if not db_groups:
            print("papers 表无重复。")
            return
        print(f"共 {len(db_groups)} 组疑似重复（只报不删，清理前先人工核对）：")
        for g in db_groups:
            print("\n=== 组 ===")
            for r in g:
                print(f"  id={r['id']} | {r['year'] or '?'} | doi={r['doi'] or '-'}")
                print(f"    {r['title'][:95]}")
                print(f"    stem={r['stem'][:80]}")
        return

    groups = find_duplicates(args.meta_dir, fuzzy_threshold=args.fuzzy)
    if not groups:
        print("无重复。")
        return

    total_delete = 0
    for g in groups:
        keep = pick_keep(g)
        print(f"\n=== 重复组 ({len(g)} 个) 保留: {keep['file']}")
        for item in g:
            if item["file"] == keep["file"]:
                continue
            print(f"  ✗ {item['file']}")
            print(f"    标题: {item['title'][:70]} | {item['authors'][:40]} | {item['year']}")
            total_delete += 1
            if args.delete:
                os.remove(os.path.join(args.meta_dir, item["file"]))
                print(f"    [已删除]")

    print(f"\n共 {len(groups)} 组重复，将删除 {total_delete} 个文件"
          + ("（已执行）" if args.delete else "（dry-run，加 --delete 执行）"))


if __name__ == "__main__":
    main()
