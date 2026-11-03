<template>
  <div class="list-page">
    <header class="topbar">
      <div class="brand">
        <span class="logo">litread</span>
        <span class="sub">生物质能源 · 文献库</span>
      </div>
      <div class="stats" v-if="stats">
        <span class="chip plain">{{ stats.papers }} 篇</span>
        <span class="chip plain">{{ fmt(stats.chunks) }} 分块</span>
        <span class="chip plain">{{ fmt(stats.entities) }} 实体</span>
        <span class="chip plain">{{ fmt(stats.hyperedges) }} 超边</span>
      </div>
      <button class="btn ghost" title="导入 PDF 并入库" @click="importEl?.show()">导入</button>
      <router-link class="btn ghost" to="/insights" title="库级分析仪表盘">分析</router-link>
      <button class="btn ghost" title="图书管理员智能体（问答入库）" @click="agentStore.openPanel()">智能体</button>
      <router-link class="btn ghost" to="/favorites" title="收藏夹：多个夹可分类，同一篇可属多个夹">★ 收藏夹</router-link>
      <button class="btn ghost theme" @click="toggleTheme" title="切换主题">◐</button>
    </header>

    <div class="filters">
      <input
        class="input search"
        v-model="query"
        placeholder="搜索标题 / 作者 / 期刊（英文关键词最准）；输入 #ID 可直达单篇"
        @keyup.enter="reload()"
      />
      <button class="btn primary" @click="reload()">检索</button>
    </div>

    <div class="chips" v-if="currentCollection">
      <span class="chip f">收藏夹：{{ currentCollectionName || `#${currentCollection}` }}<b>{{ total }}</b></span>
      <button class="link" @click="clearCollection">查看全部</button>
    </div>

    <div class="facetbar">
      <FacetSelect label="年份" :options="facetYears" v-model="selYears" />
      <FacetSelect label="期刊" :options="facetJournals" v-model="selJournals" searchable />
      <FacetSelect label="关键词" :options="facetKeywords" v-model="selKws" searchable />
      <select class="input sort" v-model="sort" @change="onSort">
        <option value="">{{ query.trim() ? '相关度' : '年份 新→旧' }}</option>
        <option value="year_desc">年份 新→旧</option>
        <option value="year_asc">年份 旧→新</option>
        <option value="title_asc">标题 A→Z</option>
        <option value="title_desc">标题 Z→A</option>
        <option value="id_desc">ID 新→旧</option>
        <option value="id_asc">ID 旧→新</option>
      </select>
      <span class="total dim" v-if="!loading">共 {{ total }} 篇</span>
    </div>

    <div class="chips" v-if="activeChips.length">
      <span
        v-for="c in activeChips"
        :key="c.group + c.value"
        class="chip f"
        :title="'点击移除：' + c.value"
        @click="removeChip(c)"
      >{{ c.label }}：{{ shortV(c.value) }} <b>×</b></span>
      <button class="link" @click="clearAll">清空全部</button>
    </div>

    <main class="grid">
      <div v-if="loading && !items.length" class="state full"><span class="spinner"></span></div>
      <div v-else-if="error" class="state full err">{{ error }}</div>
      <div v-else-if="!items.length" class="state full">没有匹配的论文</div>

      <router-link
        v-for="p in items"
        :key="p.id"
        class="pcard"
        :to="{ name: 'paper', params: { id: p.id } }"
      >
        <div class="pc-head">
          <span class="yr">{{ p.year || '—' }}</span>
          <span class="pdf-dot" :class="{ on: p.pdf }" :title="p.pdf ? '有 PDF 原文' : '无 PDF'">◍</span>
          <span class="fav" :class="{ on: p.favorite }" :title="p.favorite ? '调整收藏夹归属' : '一键加入默认收藏夹'" @click.prevent.stop="onStar(p, $event)">{{ p.favorite ? '★' : '☆' }}</span>
          <span class="fav-more" title="选择收藏夹 / 移动到其他夹" @click.prevent.stop="openMenu(p, $event)">▾</span>
        </div>
        <h4 class="pc-title">{{ p.title || p.stem }}</h4>
        <div class="pc-meta">
          <span class="au" :title="p.authors">{{ shortAuthors(p.authors) }}</span>
          <span class="sep">·</span>
          <span class="jn" :title="p.journal">{{ p.journal || '未知期刊' }}</span>
        </div>
        <p class="pc-abs">{{ p.abstract || '（暂无摘要）' }}</p>
        <div class="pc-tags">
          <span class="tag" v-for="k in (p.keywords || []).slice(0, 4)" :key="k" :title="k">{{ k }}</span>
          <span class="tag ent" v-if="p.entity_count" title="本文实体数">{{ p.entity_count }} 实体</span>
        </div>
        <span class="pc-id" title="论文 ID（可添加到智能体工作区）">#{{ p.id }}</span>
      </router-link>
    </main>

    <div class="more" v-if="items.length && page < pages">
      <button class="btn" :disabled="loading" @click="loadMore">
        {{ loading ? '加载中…' : `加载更多（已 ${items.length} / ${total} 篇）` }}
      </button>
    </div>
    <div class="more end dim" v-else-if="items.length && !loading">
      已加载全部 {{ total }} 篇
    </div>

    <ImportDialog ref="importEl" />

    <!-- 收藏夹菜单（▾）：勾选式多选，同一篇可同时属于多个夹 -->
    <div v-if="menuPaper" class="fm-mask" @click="menuPaper = null"></div>
    <div v-if="menuPaper" class="fav-menu" :style="{ left: menuPos.x + 'px', top: menuPos.y + 'px' }" @click.stop>
      <div class="fm-head">
        <span>收藏到</span>
        <span class="fm-pid">#{{ menuPaper.id }}</span>
      </div>
      <label v-for="c in collections" :key="c.id" class="fm-row">
        <input type="checkbox" :checked="inMenu(c.id)" @change="toggleIn(c)" />
        <span class="fm-name">{{ c.name }}</span>
        <span class="fm-count">{{ c.count }}</span>
      </label>
      <div v-if="!collections.length" class="fm-empty">还没有收藏夹</div>
      <div v-if="currentCollection && menuInCurrent && collections.length > 1" class="fm-move">
        <span>移动到</span>
        <select @change="doMove($event)">
          <option value="">选择夹…</option>
          <option v-for="c in collections.filter((x) => x.id !== currentCollection)" :key="c.id" :value="c.id">{{ c.name }}</option>
        </select>
      </div>
      <div class="fm-foot">
        <router-link to="/favorites" @click="menuPaper = null">管理收藏夹</router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'
import { toggleTheme } from '../theme.js'
import { agentStore } from '../agentStore.js'
import FacetSelect from '../components/FacetSelect.vue'
import ImportDialog from '../components/ImportDialog.vue'

const stats = ref(null)
const facetYears = ref([])
const facetJournals = ref([])
const facetKeywords = ref([])
const query = ref('')
const selYears = ref([])        // 多选：年份
const selJournals = ref([])     // 多选：期刊
const selKws = ref([])          // 多选：关键词
const sort = ref('')            // '' | year_desc | year_asc
const collections = ref([])           // 全部收藏夹（菜单与当前夹名用）
const currentCollection = ref(0)      // 0=不按夹筛选；>0=只看该夹
const menuPaper = ref(null)           // 打开 ▾ 菜单的论文
const menuPos = ref({ x: 0, y: 0 })
const route = useRoute()
const router = useRouter()
const page = ref(1)
const pages = ref(1)
const total = ref(0)
const items = ref([])
const loading = ref(true)
const error = ref('')
const importEl = ref(null)

const fmt = (n) => (n ?? 0).toLocaleString()

function shortAuthors(a) {
  if (!a) return '—'
  const first = a.split(',')[0].trim()
  const n = (a.match(/,/g) || []).length
  return n > 0 ? `${first} 等${n ? `(${n + 1})` : ''}` : first
}

const activeChips = computed(() => [
  ...selYears.value.map((v) => ({ group: 'year', value: v, label: '年份' })),
  ...selJournals.value.map((v) => ({ group: 'journal', value: v, label: '期刊' })),
  ...selKws.value.map((v) => ({ group: 'keywords', value: v, label: '关键词' })),
])

const shortV = (v) => (v.length > 30 ? v.slice(0, 30) + '…' : v)

const currentCollectionName = computed(() => {
  const c = collections.value.find((x) => x.id === currentCollection.value)
  return c ? c.name : ''
})

const inMenu = (cid) => !!(menuPaper.value && (menuPaper.value.collections || []).includes(cid))
const menuInCurrent = computed(
  () => !!(menuPaper.value && (menuPaper.value.collections || []).includes(currentCollection.value)),
)

function removeChip(c) {
  const t = { year: selYears, journal: selJournals, keywords: selKws }[c.group]
  t.value = t.value.filter((x) => x !== c.value)   // watcher 触发重查
}
function clearAll() {
  selYears.value = []
  selJournals.value = []
  selKws.value = []
}
function onSort() {
  page.value = 1
  reload()
}

// 任一筛选变化 → 回到第 1 页重查（维度间 AND、维度内 OR 由后端实现）
watch([selYears, selJournals, selKws], () => {
  page.value = 1
  reload()
})

async function reload(append = false) {
  loading.value = true
  error.value = ''
  try {
    const d = await api.papers({
      query: query.value.trim(),
      year: selYears.value.join('|'),
      journal: selJournals.value.join('|'),
      keywords: selKws.value.join('|'),
      sort: sort.value,
      collection_id: currentCollection.value || 0,
      page: page.value,
      page_size: 24,
    })
    items.value = append ? items.value.concat(d.items) : d.items   // 卡片流：追加式加载
    total.value = d.total
    pages.value = d.pages
    page.value = d.page
  } catch (e) {
    error.value = `加载失败：${e.message}`
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (page.value >= pages.value || loading.value) return
  page.value += 1
  await reload(true)
}

function clearCollection() {
  currentCollection.value = 0
  page.value = 1
  router.push({ path: '/' })
  reload()
}

async function loadCollections() {
  try {
    const d = await api.collections()
    collections.value = d.items || []
  } catch (e) {
    /* 菜单加载失败不阻断列表 */
  }
}

async function openMenu(p, ev) {
  menuPaper.value = p
  const r = ev?.currentTarget?.getBoundingClientRect()
  if (r) {
    const w = 236
    menuPos.value = {
      x: Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)),
      y: Math.min(window.innerHeight - 260, r.bottom + 8),
    }
  }
  await loadCollections()
}

/** 星标：未收藏 → 一键进默认夹；已收藏 → 打开菜单精细调整（避免误删分类） */
async function onStar(p, ev) {
  if (p.favorite) {
    openMenu(p, ev)
    return
  }
  try {
    const d = await api.favorite(p.id)
    p.collections = d.collections || []
    p.favorite = d.favorite
    await loadCollections()
    if (currentCollection.value && !p.collections.includes(currentCollection.value)) {
      items.value = items.value.filter((x) => x.id !== p.id)
      total.value = Math.max(0, total.value - 1)
    }
  } catch (e) {
    error.value = `收藏操作失败：${e.message}`
  }
}

/** 勾选/取消某个夹：整体覆盖式提交（同一篇可属多个夹） */
async function toggleIn(c) {
  const p = menuPaper.value
  if (!p) return
  const cur = new Set(p.collections || [])
  if (cur.has(c.id)) cur.delete(c.id)
  else cur.add(c.id)
  try {
    const d = await api.setPaperCollections(p.id, [...cur])
    p.collections = d.collections
    p.favorite = d.favorite
    await loadCollections()
    if (currentCollection.value && !p.collections.includes(currentCollection.value)) {
      items.value = items.value.filter((x) => x.id !== p.id)
      total.value = Math.max(0, total.value - 1)
    }
  } catch (e) {
    error.value = `保存收藏夹失败：${e.message}`
  }
}

/** 移动：把当前篇从当前夹移到目标夹（后端单事务） */
async function doMove(ev) {
  const to = Number(ev.target.value)
  ev.target.value = ''
  const p = menuPaper.value
  if (!to || !currentCollection.value || !p) return
  try {
    await api.movePapers(currentCollection.value, [p.id], to)
    items.value = items.value.filter((x) => x.id !== p.id)
    total.value = Math.max(0, total.value - 1)
    menuPaper.value = null
    await loadCollections()
  } catch (e) {
    error.value = `移动失败：${e.message}`
  }
}

watch(() => route.query.collection, (v) => {
  currentCollection.value = Number(v) || 0
  page.value = 1
  reload()
})

onMounted(async () => {
  currentCollection.value = Number(route.query.collection) || 0
  loadCollections()
  reload()
  api.stats().then((s) => (stats.value = s)).catch(() => {})
  api.facets().then((f) => {
    facetYears.value = f.years || []
    facetJournals.value = f.journals || []
    facetKeywords.value = f.keywords || []
  }).catch(() => {})
})
</script>

<style scoped>
.list-page {
  max-width: 1180px;
  margin: 0 auto;
  padding: 26px 28px 60px;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 18px;
  margin-bottom: 22px;
}
.brand .logo {
  font-family: var(--font-serif);
  font-size: 27px;
  font-weight: 700;
  letter-spacing: 0.4px;
  color: var(--text);
}
.brand .logo::after {
  content: "";
  display: inline-block;
  width: 7px; height: 7px;
  margin-left: 5px;
  border-radius: 50%;
  background: var(--accent);
  vertical-align: 2px;
}
.brand .sub {
  margin-left: 10px;
  color: var(--text-faint);
  font-size: 13.5px;
}
.stats { display: flex; gap: 8px; flex: 1; }
.theme { font-size: 16px; padding: 6px 10px; }

.filters {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 16px;
}
.search { flex: 1; }
.total { font-size: 13px; }

.facetbar {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.facetbar .sort { min-width: 128px; height: 33px; font-size: 13px; }

.chips {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.chip.f {
  cursor: pointer;
  background: var(--accent-soft);
  color: var(--text);
  border: 1px solid transparent;
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip.f:hover { border-color: var(--accent); }
.chip.f b { color: var(--text-faint); font-weight: 400; margin-left: 3px; }
.chips .link {
  border: none;
  background: none;
  color: var(--text-faint);
  font-size: 12.5px;
  cursor: pointer;
  padding: 0;
}
.chips .link:hover { color: var(--accent); text-decoration: underline; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(252px, 1fr));
  gap: 14px;
}
.state {
  padding: 46px;
  text-align: center;
  color: var(--text-dim);
}
.state.full { grid-column: 1 / -1; }
.state.err { color: var(--warn); }

/* ── 论文卡片（小红书/B站式） ── */
.pcard {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 14px 16px 13px;
  background: var(--bg-panel);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  color: inherit;
  text-decoration: none;
  transition: transform var(--t-fast), border-color var(--t-fast), box-shadow var(--t-fast);
}
.pcard:hover {
  transform: translateY(-3px);
  border-color: var(--accent);
  box-shadow: var(--shadow);
  text-decoration: none;
}
.pc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.pc-head .yr {
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 1px 9px;
  border-radius: 999px;
}
.pdf-dot { color: var(--line-strong); font-size: 13px; }
.pdf-dot.on { color: var(--ok); }
.fav { cursor: pointer; font-size: 14px; margin-left: 6px; opacity: 0.4; user-select: none; }
.fav:hover { opacity: 1; }
.fav.on { opacity: 1; color: var(--accent); }

.pc-title {
  margin: 0;
  font-size: 14.5px;
  font-weight: 600;
  line-height: 1.45;
  color: var(--text);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 3.1em;
}
.pc-meta {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 12.3px;
  color: var(--text-faint);
  min-width: 0;
}
.pc-meta .au { max-width: 62%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pc-meta .jn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pc-meta .sep { color: var(--line-strong); flex: none; }
.pc-abs {
  margin: 0;
  font-size: 12.6px;
  line-height: 1.65;
  color: var(--text-dim);
  flex: 1;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.pc-tags { display: flex; flex-wrap: wrap; gap: 5px; padding-right: 42px; }
.pc-id {
  position: absolute;
  right: 12px;
  bottom: 10px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
.pcard:hover .pc-id { color: var(--accent); }
.tag {
  font-size: 11.3px;
  color: var(--text-dim);
  background: var(--bg-hover);
  border: 1px solid var(--line);
  padding: 1px 7px;
  border-radius: 999px;
  max-width: 132px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tag.ent { color: var(--accent); border-color: transparent; background: var(--accent-soft); }

.more { display: flex; justify-content: center; margin-top: 22px; }
.more.end { font-size: 13px; }
/* ── 收藏夹菜单（▾） ── */
.fav-more {
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  padding: 2px 5px;
  margin-left: 2px;
  color: var(--text-dim);
  border-radius: 4px;
  user-select: none;
}
.fav-more:hover { color: var(--accent); background: var(--accent-soft); }
.fm-mask { position: fixed; inset: 0; z-index: 90; }
.fav-menu {
  position: fixed;
  z-index: 91;
  width: 236px;
  max-height: 60vh;
  overflow: auto;
  padding: 8px;
  border-radius: 9px;
  background: var(--bg-elev, #fff);
  border: 1px solid var(--line, #e2e2e2);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.16);
  font-size: 13px;
}
.fm-head {
  display: flex;
  justify-content: space-between;
  padding: 2px 4px 7px;
  color: var(--text-faint);
  font-size: 12px;
}
.fm-pid { opacity: 0.7; }
.fm-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 4px;
  border-radius: 6px;
  cursor: pointer;
}
.fm-row:hover { background: var(--accent-soft); }
.fm-row input { margin: 0; }
.fm-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fm-count { color: var(--text-faint); font-size: 11.5px; }
.fm-empty { padding: 8px 4px; color: var(--text-faint); font-size: 12.5px; }
.fm-move {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 6px 4px 2px;
  padding-top: 7px;
  border-top: 1px solid var(--line, #eee);
  color: var(--text-faint);
  font-size: 12px;
}
.fm-move select { flex: 1; height: 26px; font-size: 12px; }
.fm-foot {
  margin-top: 6px;
  padding-top: 7px;
  border-top: 1px solid var(--line, #eee);
  font-size: 12px;
}
.fm-foot a { color: var(--accent); text-decoration: none; }
</style>
