#!/usr/bin/env python3
"""批量合并管线 - DBSCAN 分簇 → LLM 批量合并 → 入库 litreview"""
import json, httpx, sqlite3, time, os, concurrent.futures

from modules.paths import db_path, entity_merge_dir

DB_PATH = str(db_path())


def load_config():
    """读 config.yaml 的 merge 配置。

    解析顺序：steps.merge → providers.<name>（取 base_url / api_key / model /
    temperature / max_tokens / timeout）；steps.merge 缺失时回退顶层 merge 段；
    两者都没有则直接报错，不做隐式默认。
    """
    import yaml  # noqa: F401
    from modules import config as _cfgmod
    cfg = _cfgmod.load()

    # steps.merge → providers.<provider>
    sc = (cfg.get("steps") or {}).get("merge")
    if sc:
        prov = (cfg.get("providers") or {}).get(sc.get("provider", ""), {})
        mk = sc.get("model", "default")
        return {
            "base_url": prov.get("base_url", ""),
            "api_key": prov.get("api_key", ""),
            "model": (prov.get("models") or {}).get(mk, mk),
            "temperature": sc.get("temperature", 0.1),
            "max_tokens": sc.get("max_tokens", 4096),
            "timeout": sc.get("timeout", 180),
        }

    # 回退：顶层 merge 段（旧式平铺配置）
    mc = cfg.get("merge", {})
    if mc:
        return mc

    raise RuntimeError(
        "未配置实体合并用的 LLM：请在 config.yaml 设置 steps.merge"
        "（provider + model），或提供顶层 merge 段。")


_FAIL_LOG = None       # 由 run() 设置：失败批原始输出落盘（诊断用）


def _log_fail(tag, text):
    """把失败信息写盘：含批次号 + 原始输出片段，便于事后定位。"""
    if not _FAIL_LOG:
        return
    try:
        os.makedirs(os.path.dirname(_FAIL_LOG), exist_ok=True)
        with open(_FAIL_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 60}\n[{time.strftime('%H:%M:%S')}] {tag}\n{text}\n")
    except Exception:  # noqa: BLE001
        pass


def get_llm_fn():
    cfg = load_config()
    url = cfg.get("base_url", "").rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}",
               "Content-Type": "application/json"}
    model = cfg.get("model", "")
    if not model:
        raise RuntimeError("实体合并未指定模型：请在 config.yaml 的 steps.merge 里写 model")
    # 一律请求关闭思考链：部分模型默认开思考，会输出推理文本把 JSON 淹掉。
    extra = {"chat_template_kwargs": {"enable_thinking": False}}
    to = cfg.get("timeout", 180)
    def_mt = cfg.get("max_tokens", 8192)

    def llm_call(messages, max_tokens=None, tag=""):
        """流式调用。

        非流式长响应在等待期容易被网关断掉；流式持续吐数据、连接保持活跃，
        服务端真卡死时靠 read timeout 快速失败重试。最多 3 次。
        """
        payload = {"model": model, "messages": messages,
                   "temperature": cfg.get("temperature", 0.1),
                   "max_tokens": max_tokens or def_mt, "stream": True}
        payload.update(extra)
        for at in range(3):
            try:
                buf = []
                # trust_env=False：同 llm_client——禁系统代理，否则请求会被代理劫持
                client = httpx.Client(timeout=to, trust_env=False)
                with client.stream("POST", url, json=payload, headers=headers) as r:
                    if r.status_code != 200:
                        _log_fail(f"{tag} try{at + 1} HTTP {r.status_code}",
                                  str(r.read()[:300]))
                        if at < 2:
                            time.sleep(3)
                        continue
                    for line in r.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        d = line[5:].strip()
                        if d == "[DONE]":
                            break
                        try:
                            j = json.loads(d)
                            ch = j["choices"][0].get("delta") or {}
                            buf.append(ch.get("content") or "")
                        except Exception:  # noqa: BLE001
                            pass
                text = "".join(buf)
                if text.strip():
                    return text
                _log_fail(f"{tag} try{at + 1} empty", "(empty response)")
            except Exception as e:  # noqa: BLE001
                _log_fail(f"{tag} try{at + 1} {type(e).__name__}", str(e))
            if at < 2:
                time.sleep(3)
        return None
    return llm_call


def extract_json(text):
    """解析 LLM 输出为 JSON。"""
    if not text:
        return None
    text = text.strip()
    # 剥思考前缀（safety net；理论上一律关思考后不该出现，防意外）
    if "Here's a thinking process" in text:
        text = text.split("Here's a thinking process")[-1]
    try:
        from json_repair import loads as jr_loads
        v = jr_loads(text)
        if isinstance(v, (list, dict)) and v:
            return v
    except Exception:  # noqa: BLE001
        pass
    # 兑底：截首个 [ 到末个 ]
    try:
        a, b = text.find("["), text.rfind("]")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
    except Exception:  # noqa: BLE001
        pass
    return None


def build_prompt(all_ents, incremental=False, eid2merged=None):
    """构造单次调用的合并 prompt：实体名 + 类型 + 描述一并给出，
    要求 LLM 一次输出 name / ids / description / aliases。"""
    eid2merged = eid2merged or {}
    lines = []
    for i, e in enumerate(all_ents, 1):
        p = f"[{i}] {e['name']}"
        if e["type"] not in ("", "material"):
            p += f"\n    类型: {e['type']}"
        if incremental:
            _mn = eid2merged.get(e["id"])
            if _mn:
                p += f" [已归入: {_mn}]"
        _d = (e.get("description") or "").strip().replace("\n", " ")
        if _d:
            p += f"\n    描述: {_d[:200]}"
        lines.append(p)
    list_text = "\n".join(lines)
    return (
        "你是一个知识图谱实体合并专家。下面是需要处理的实体列表，"
        "每个实体给出名称、类型和描述。\n\n"
        "任务：找出表达同一概念的实体，把它们合并成组。\n\n"
        "规则：\n"
        "1. 每个编号只能出现在一组合并中\n"
        "2. 每组合并至少包含2个编号\n"
        "3. 不需要合并的实体不要输出\n"
        "4. name 选最具有代表性的标准名称\n"
        "5. 主要依据 描述 判断（type 字段抽取噪声较大，仅供参考）："
        "描述指向同一概念、只是写法/缩写/词序/大小写不同 → 必须合并；"
        "描述指向明显不同的对象（不同参数、不同数值范围、不同操作）→ 不合并\n"
        "6. 标有【已归入】的实体已有规范名（别名更全、描述更完整）；"
        "若与新实体同义，name 一律沿用该已归入的规范名\n"
        "7. aliases 列出组内其余名称，原样保留（括号内容拆为独立别名），"
        "保留原大小写和连字符，不做任何归一\n"
        "8. description 写一段 50-100 字的综合描述，"
        "综合组内各实体的描述说明这个概念是什么（合并理由即在其中）\n\n"
        '输出JSON数组：\n'
        '[{"name": "Standard term", "ids": [1, 3, 5], '
        '"description": "...", "aliases": ["...", "..."]}, '
        '{"name": "Another term", "ids": [2, 4], '
        '"description": "...", "aliases": ["..."]}]\n\n'
        f"实体列表（共{len(all_ents)}个）：\n{list_text}"
    )


# ── 实体身份：并查集式（只挂接、不删除，union by age）─────────────
# 规则：公有实体 id 永不复用。两个公有实体需要合并时，id 小的（创建更早）当根，
# 另一个标 status='merged_into' + canonical_id 指向根；根的名称/描述可以原地更新。
IDENTITY_COLUMNS = {
    "canonical_id": "INTEGER DEFAULT NULL",   # 非空 = 已被并入该根
    "status": "TEXT DEFAULT 'active'",        # active / merged_into / superseded
    "generation": "INTEGER DEFAULT 1",        # 第几次合并运行写下的
}
MERGED_DDL = (
    "CREATE TABLE IF NOT EXISTS entities_merged ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
    "aliases TEXT DEFAULT '', type TEXT DEFAULT '', "
    "description TEXT DEFAULT '', source_ids TEXT NOT NULL, "
    "is_merged INTEGER DEFAULT 0, vector BLOB DEFAULT NULL, "
    "canonical_id INTEGER DEFAULT NULL, status TEXT DEFAULT 'active', "
    "generation INTEGER DEFAULT 1)"
)


def ensure_identity_columns(cur) -> None:
    """老库补列（可重复执行）；表不存在时按新结构建。"""
    cols = [r[1] for r in cur.execute("PRAGMA table_info(entities_merged)")]
    if not cols:
        cur.execute(MERGED_DDL)
        return
    for col, ddl in IDENTITY_COLUMNS.items():
        if col not in cols:
            cur.execute(f"ALTER TABLE entities_merged ADD COLUMN {col} {ddl}")


def next_generation(cur) -> int:
    row = cur.execute(
        "SELECT COALESCE(MAX(generation), 0) FROM entities_merged").fetchone()
    return int(row[0] or 0) + 1


def _active_index(cur) -> dict:
    """私有实体 id → 所属 active 行 {id, is_merged}。"""
    idx = {}
    for rid, src, ism in cur.execute(
            "SELECT id, source_ids, is_merged FROM entities_merged "
            "WHERE COALESCE(status,'active')='active'"):
        try:
            members = json.loads(src or "[]")
        except Exception:  # noqa: BLE001
            continue
        for m in members:
            try:
                idx[int(m)] = {"id": rid, "is_merged": ism}
            except (TypeError, ValueError):
                pass
    return idx


def _norm_name(s: str) -> str:
    return (s or "").strip().lower()


def _name_index(cur) -> dict:
    """规范名 → active 行 id（同名视为同一概念，新的挂到旧的上）。"""
    idx = {}
    for rid, name, canon in cur.execute(
            "SELECT id, name, canonical_id FROM entities_merged "
            "WHERE COALESCE(status,'active')='active'"):
        if canon:
            continue
        key = _norm_name(name)
        if key:
            idx.setdefault(key, rid)
    return idx


def write_groups(cur, groups, noise_ids, idm, paper_rows, generation) -> dict:
    """把本轮分组写进 entities_merged —— 只挂接、不删除。

    返回 {"kept", "merged_into", "inserted"}。
    """
    idx = _active_index(cur)
    name_idx = _name_index(cur)
    stats = {"kept": 0, "merged_into": 0, "inserted": 0}

    def _update_row(rid, name, aliases, etype, desc, members, is_merged):
        cur.execute(
            "UPDATE entities_merged SET name=?, aliases=?, type=?, description=?, "
            "source_ids=?, is_merged=?, status='active', generation=? WHERE id=?",
            (name, aliases, etype, desc, json.dumps(members, ensure_ascii=False),
             is_merged, generation, rid))

    def _insert_row(name, aliases, etype, desc, members, is_merged):
        cur.execute(
            "INSERT INTO entities_merged (name, aliases, type, description, "
            "source_ids, is_merged, status, generation) "
            "VALUES (?,?,?,?,?,?,'active',?)",
            (name, aliases, etype, desc, json.dumps(members, ensure_ascii=False),
             is_merged, generation))
        return cur.lastrowid

    def _register(rid, members, is_merged):
        for m in members:
            idx[int(m)] = {"id": rid, "is_merged": is_merged}

    # 1) LLM 合并出的组
    for g in groups:
        members = [int(x) for x in g["entity_ids"]]
        hits = []
        for m in members:
            row = idx.get(m)
            if row and row["id"] not in [h["id"] for h in hits]:
                hits.append(row)
        if not hits:
            # 成员全不认识，但同名概念已存在 → 挂到那个旧根（union by age）
            rid = name_idx.get(_norm_name(g.get("name", "")))
            if rid is not None:
                hits = [{"id": rid, "is_merged": 1}]
        if hits:
            root = min(hits, key=lambda r: r["id"])      # union by age：老 id 当根
            cur_members = set(members)
            for r in hits:                                # 把旧行的成员并进来
                for m, rec in list(idx.items()):
                    if rec["id"] == r["id"]:
                        cur_members.add(m)
            _update_row(root["id"], g["name"], ", ".join(g["aliases"]),
                        g.get("type", ""), g.get("description", ""),
                        sorted(cur_members), 1)
            for r in hits:
                if r["id"] == root["id"]:
                    continue
                cur.execute(
                    "UPDATE entities_merged SET canonical_id=?, status='merged_into', "
                    "generation=? WHERE id=?", (root["id"], generation, r["id"]))
                stats["merged_into"] += 1
            _register(root["id"], cur_members, 1)
            name_idx[_norm_name(g.get("name", ""))] = root["id"]
            stats["kept"] += 1
        else:
            rid = _insert_row(g["name"], ", ".join(g["aliases"]), g.get("type", ""),
                              g.get("description", ""), members, 1)
            _register(rid, members, 1)
            name_idx[_norm_name(g.get("name", ""))] = rid
            stats["inserted"] += 1

    # 2) 未进任何组的私有实体 → 单例行
    for eid in sorted(noise_ids):
        e = idm.get(eid)
        if e is None:
            continue
        row = idx.get(int(eid))
        if row:                       # 已被某个组收进去，不重复建行
            continue
        rid = _insert_row(e["name"], "", e.get("type", ""),
                          e.get("description", ""), [int(eid)], 0)
        _register(rid, [int(eid)], 0)
        stats["inserted"] += 1

    # 3) 论文实体单例（跨文献超边的「论文: 标题」参与者靠它解析）
    for pid, pname, pdesc in paper_rows:
        row = idx.get(int(pid))
        if row:
            continue
        rid = _insert_row(pname, "", "paper", pdesc or "", [int(pid)], 0)
        _register(rid, [int(pid)], 0)
        stats["inserted"] += 1

    # 4) 收尾一：同名多根 → 只留最老的一根，其余挂过去
    dup = cur.execute(
        "SELECT LOWER(TRIM(name)) k, COUNT(*) n FROM entities_merged "
        "WHERE COALESCE(status,'active')='active' GROUP BY k HAVING n > 1").fetchall()
    for (key, _n) in dup:
        rids = [r[0] for r in cur.execute(
            "SELECT id FROM entities_merged WHERE LOWER(TRIM(name))=? "
            "AND COALESCE(status,'active')='active' ORDER BY id", (key,))]
        root = rids[0]
        members = set()
        for rid, src in cur.execute(
                f"SELECT id, source_ids FROM entities_merged WHERE id IN "
                f"({','.join('?' * len(rids))})", rids):
            try:
                members |= {int(x) for x in json.loads(src or "[]")}
            except Exception:  # noqa: BLE001
                pass
        # 根要吃下被并行的全部成员，否则 _o2m 会丢映射（私有实体变未映射）
        cur.execute("UPDATE entities_merged SET source_ids=? WHERE id=?",
                    (json.dumps(sorted(members), ensure_ascii=False), root))
        for other in rids[1:]:
            cur.execute(
                "UPDATE entities_merged SET canonical_id=?, status='merged_into', "
                "generation=? WHERE id=?", (root, generation, other))
            stats["merged_into"] += 1

    # 5) 收尾二：成员全部已从 entities 消失的行 → superseded（不删，留身份记录）
    alive = {r[0] for r in cur.execute("SELECT id FROM entities")}
    for rid, src in cur.execute(
            "SELECT id, source_ids FROM entities_merged "
            "WHERE COALESCE(status,'active')='active'").fetchall():
        try:
            members = set(int(x) for x in json.loads(src or "[]"))
        except Exception:  # noqa: BLE001
            continue
        if members and not (members & alive):
            cur.execute(
                "UPDATE entities_merged SET status='superseded', generation=? WHERE id=?",
                (generation, rid))
            stats["superseded"] = stats.get("superseded", 0) + 1
    return stats


def run(cluster_path="data/entity_merge/clusters.json",
        db_path=DB_PATH, max_workers=8, batch_size=100,
        incremental_paper_ids=None):
    """加载分簇结果 → LLM 批量合并 → 写入 entities_merged 表

    incremental_paper_ids: 增量模式。给定时只处理「含这些论文实体的簇」，
        并【追加更新】entities_merged（不 DROP 重建）：只删/插受影响实体的行，
        其余行原样保留。
    """
    global _FAIL_LOG
    _FAIL_LOG = os.path.join(os.path.dirname(os.path.abspath(cluster_path)),
                             "merge_fails.log")
    llm = get_llm_fn()

    incremental = incremental_paper_ids is not None
    new_ids = set()
    eid2merged = {}   # 私有实体 id -> 已归入的共有实体规范名
    if incremental:
        # 增量：用 cluster_pipeline 的全库分簇结果，只取含「未映射私有实体」的簇
        clusters = json.load(open(cluster_path, encoding="utf-8"))["clusters"]
        for _c in clusters:
            if "entity_ids" not in _c and "ids" in _c:
                _c["entity_ids"] = _c["ids"]
        _c = sqlite3.connect(db_path)
        mapped = set()
        for (s,) in _c.execute(
                "SELECT source_ids FROM entities_merged "
                "WHERE COALESCE(status,'active')='active'"):
            try:
                mapped.update(json.loads(s))
            except Exception:
                pass
        new_ids = {r[0] for r in _c.execute(
            "SELECT id FROM entities WHERE type != 'paper'")} - mapped
        for _mid, _name, _src in _c.execute(
                "SELECT id, name, source_ids FROM entities_merged "
                "WHERE COALESCE(status,'active')='active'"):
            try:
                for _eid in json.loads(_src):
                    eid2merged[_eid] = _name
            except Exception:
                pass
        _c.close()
        clusters = sorted(
            [c for c in clusters
             if c["size"] >= 2 and set(c["entity_ids"]) & new_ids],
            key=lambda x: -x["size"])
        print(f"[增量] 未映射私有实体 {len(new_ids)} → 受影响簇 {len(clusters)}")
    else:
        clusters = json.load(open(cluster_path, encoding="utf-8"))["clusters"]
        for _c in clusters:   # 兼容 cluster_pipeline.py 输出的 ids 字段
            if "entity_ids" not in _c and "ids" in _c:
                _c["entity_ids"] = _c["ids"]
        clusters = sorted([c for c in clusters if c["size"] >= 2],
                          key=lambda x: -x["size"])

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, name, type, description FROM entities "
        "WHERE type != 'paper' ORDER BY id"
    ).fetchall()
    conn.close()
    idm = {r[0]: {"id": r[0], "name": r[1], "type": r[2], "description": r[3]}
           for r in rows}

    total_ents = sum(c["size"] for c in clusters)
    batches = []
    cur = []
    cur_size = 0
    for cl in clusters:
        ents = [idm[eid] for eid in cl["entity_ids"] if eid in idm]
        if not ents:
            continue
        n = len(ents)
        # 簇不可拆：装满前先预判，超过 batch_size 就切（>batch_size 的大簇独占一批）
        if cur and cur_size + n > batch_size:
            batches.append(cur)
            cur, cur_size = [], 0
        cur.append({"source": cl.get("source", "?"), "entities": ents})
        cur_size += n
    if cur:
        batches.append(cur)

    print(f"加载 {len(clusters)} 簇, 分批 {len(batches)}, 实体 {total_ents}")

    all_groups = []
    seen_global = set()
    fail = 0
    t0 = time.time()

    for bidx, batch in enumerate(batches):
        all_ents = [e for cl in batch for e in cl["entities"]]

        prompt = build_prompt(all_ents, incremental, eid2merged)

        r1 = llm([
            {"role": "system",
             "content": "你只输出 JSON 数组，不要任何解释、思考过程或 Markdown 围栏。"},
            {"role": "user", "content": prompt}
        ], tag=f"batch{bidx + 1}")

        groups = extract_json(r1)
        if not isinstance(groups, list):
            print(f"批 {bidx + 1} ❌")
            _log_fail(f"batch{bidx + 1} parse_fail",
                      "raw=\n" + (r1 or "(None)")[:3000])
            fail += 1
            continue
        # 空数组 = 本批没有可合并的同义实体，属正常情况（不计失败）

        seen = set()
        valid = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            ids = {i for i in set(g.get("ids", []))
                   if 1 <= i <= len(all_ents)}
            eids = {all_ents[i-1]["id"] for i in ids}
            if len(ids) >= 2 and not (seen & ids) and not (seen_global & eids):
                seen.update(ids)
                seen_global.update(eids)
                _names = [all_ents[i-1]["name"] for i in ids]
                _al = g.get("aliases") or []
                if not isinstance(_al, list):
                    _al = []
                # 别名 = LLM 给出的 + 组内原始名称，完全相等去重（不做大小写归一）
                aliases = list(dict.fromkeys(
                    a for a in (list(_al) + _names)
                    if isinstance(a, str) and a.strip()
                    and a != g.get("name")
                ))
                _types = [all_ents[i-1]["type"] for i in ids
                          if all_ents[i-1].get("type")]
                valid.append({
                    "name": g.get("name") or _names[0],
                    "ids": sorted(ids),
                    "entity_ids": sorted(eids),
                    "aliases": aliases,
                    "description": (g.get("description") or "").strip()[:500],
                    "type": (max(set(_types), key=_types.count)
                             if _types else ""),
                })

        all_groups.extend(valid)

        elapsed = time.time() - t0
        eta = (elapsed / (bidx + 1) * (len(batches) - bidx - 1)
               if bidx > 0 else 0)
        print(f"批 {bidx+1}/{len(batches)}: {len(all_ents)}实体 "
              f"{len(valid)}组 ETA:{eta/60:.0f}m")

    # ── 写入 entities_merged：并查集式「只挂接、不删除」（union by age）──
    conn2 = sqlite3.connect(db_path)
    cur = conn2.cursor()
    ensure_identity_columns(cur)
    generation = next_generation(cur)

    paper_rows = conn2.execute(
        "SELECT id, name, description FROM entities WHERE type='paper'").fetchall()
    noise_ids = set(idm.keys()) - seen_global
    wstats = write_groups(cur, all_groups, noise_ids, idm, paper_rows, generation)
    conn2.commit()
    print(f"  entities_merged 第 {generation} 代：沿用/更新 {wstats['kept']} 行，"
          f"挂接到旧根 {wstats['merged_into']} 行，新增 {wstats['inserted']} 行")

    # 建 _m2o 映射表（merged_id → source_ids）
    cur.execute("DROP TABLE IF EXISTS _m2o")
    cur.execute(
        "CREATE TABLE _m2o AS SELECT id, source_ids FROM entities_merged "
        "WHERE COALESCE(status,'active')='active'"
    )
    conn2.commit()

    # 建 _o2m 反向映射（source_id → merged_id）：
    # 超边/检索统一以【合并后实体】为基准，单篇超边与问答衍生边的原始实体 id 是中间态
    n_o2m = rebuild_o2m(cur)
    conn2.commit()
    print(f"  _o2m 反向映射: {n_o2m} 条")

    # 公有 id 随合并而变 → 重算两张超边表的 participants_merged（派生列）
    for _t, _r in refresh_edge_domains(conn2).items():
        print(f"  [id域] {_t}: 刷新 {_r.get('updated', 0)} 行, "
              f"悬空 {len(_r.get('dangling') or [])}")

    # 标记实体映射阶段完成：合并跑完 → 有实体的论文全部置 entities_mapped
    try:
        from modules.ingest_status import set_stage
        pids = [r[0] for r in conn2.execute(
            "SELECT DISTINCT paper_id FROM entities WHERE type != 'paper' AND paper_id IS NOT NULL")]
        for _pid in pids:
            set_stage(_pid, conn=conn2, entities_mapped=True)
        print(f"  entities_mapped 已标记 {len(pids)} 篇")
    except Exception as _e:  # noqa: BLE001
        print(f"  entities_mapped 标记失败（不阻断）: {_e}")

    total = cur.execute(
        "SELECT COUNT(*) FROM entities_merged"
    ).fetchone()[0]
    merged_cnt = cur.execute(
        "SELECT COUNT(*) FROM entities_merged WHERE is_merged=1"
    ).fetchone()[0]
    has_desc = cur.execute(
        "SELECT COUNT(*) FROM entities_merged WHERE description != ''"
    ).fetchone()[0]
    has_type = cur.execute(
        "SELECT COUNT(*) FROM entities_merged WHERE type != ''"
    ).fetchone()[0]
    conn2.close()

    print(f"\n{'='*50}")
    print(f"合并完成！")
    print(f"  分批: {len(batches)}, 失败: {fail}")
    cover_pct = (len(seen_global) / total_ents * 100) if total_ents else 0.0
    print(f"  合并组: {merged_cnt}, 覆盖: {len(seen_global)}/{total_ents} "
          f"({cover_pct:.0f}%)")
    print(f"  总行数: {total}, 有描述: {has_desc}, 有类型: {has_type}")
    print(f"  耗时: {(time.time()-t0)/60:.1f}分钟  (_m2o 已建)")


def rebuild_o2m(cur) -> int:
    """从 entities_merged 重建反向映射 _o2m（source_id → merged_id）。

    跨文献超边/检索应统一以【合并后实体】为基准；单篇超边
    (hyperedges.participants) 与问答衍生边存的是原始 entities.id（中间态），
    使用时经本表归一。用 Python 展开而非 json_each，避免依赖 JSON1 扩展。
    """
    cur.execute("DROP TABLE IF EXISTS _o2m")
    cur.execute("CREATE TABLE _o2m(source_id INTEGER PRIMARY KEY, "
                "merged_id INTEGER NOT NULL)")
    rows = cur.execute(
        "SELECT id, source_ids FROM entities_merged "
        "WHERE COALESCE(status,'active')='active'").fetchall()
    data = []
    for mid, srcs in rows:
        try:
            ids = json.loads(srcs or "[]")
        except Exception:  # noqa: BLE001
            ids = []
        for s in ids:
            try:
                data.append((int(s), int(mid)))
            except (TypeError, ValueError):
                pass
    cur.executemany(
        "INSERT OR REPLACE INTO _o2m(source_id, merged_id) VALUES(?,?)", data)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_o2m_merged ON _o2m(merged_id)")
    return len(data)


def refresh_edge_domains(conn, dry: bool = False) -> dict:
    """合并后重算两张超边表的 participants_merged（公有 id 会随合并改变）。"""
    from modules.database.id_domain import refresh_table
    out = {}
    for table in ("hyperedges", "cross_doc_hyperedges"):
        out[table] = refresh_table(conn, table, dry=dry)
    return out


if __name__ == "__main__":
    run()
