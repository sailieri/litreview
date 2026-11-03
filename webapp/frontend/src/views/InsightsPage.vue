<template>
  <div class="ins-page">
    <header class="topbar">
      <router-link class="back" to="/">← 文献库</router-link>
      <div class="brand">
        <span class="logo">分析</span>
        <span class="sub">库级洞察 · 趋势 / 分布 / 关联</span>
      </div>
      <span class="grow"></span>
      <button class="btn ghost theme" @click="toggleTheme" title="切换主题">◐</button>
    </header>

    <div v-if="loading" class="state"><span class="spinner"></span></div>
    <div v-else-if="error" class="state err">{{ error }}</div>

    <template v-else-if="data">
      <!-- 概览卡片 -->
      <section class="cards">
        <div class="card c" v-for="c in cards" :key="c.k">
          <div class="n">{{ fmt(c.v) }}</div>
          <div class="l">{{ c.k }}</div>
        </div>
        <div class="card c">
          <div class="n">{{ data.year_span[0] }}–{{ data.year_span[1] }}</div>
          <div class="l">年份跨度</div>
        </div>
      </section>

      <!-- 年度趋势 + 期刊分布 -->
      <section class="grid2">
        <div class="card pane">
          <h3>年度发表趋势</h3>
          <div ref="yearEl" class="chart tall"></div>
        </div>
        <div class="card pane">
          <h3>期刊分布 TOP10</h3>
          <div class="bars">
            <div class="bar-row" v-for="j in journals10" :key="j.value">
              <span class="bv" :title="j.value">{{ j.value }}</span>
              <span class="track"><i :style="{ width: (j.count / journals10[0].count * 100) + '%' }"></i></span>
              <span class="bc">{{ j.count }}</span>
            </div>
            <div v-if="!journals10.length" class="empty">暂无期刊数据</div>
          </div>
        </div>
      </section>

      <!-- 共现网络 -->
      <section class="card pane">
        <h3>关键词共现网络 <span class="hint">（同一论文内共同出现 ≥2 次的关键词对，拖拽可调整）</span></h3>
        <div ref="graphEl" class="chart graph"></div>
      </section>

      <!-- 关键词 TOP + 年度演化 -->
      <section class="grid2">
        <div class="card pane">
          <h3>高频关键词 TOP15</h3>
          <div class="bars">
            <div class="bar-row" v-for="k in keywords15" :key="k.value">
              <span class="bv" :title="k.value">{{ k.value }}</span>
              <span class="track"><i :style="{ width: (k.count / keywords15[0].count * 100) + '%' }"></i></span>
              <span class="bc">{{ k.count }}</span>
            </div>
          </div>
        </div>
        <div class="card pane">
          <h3>研究热点演化 <span class="hint">（各年度 top 关键词）</span></h3>
          <div class="timeline">
            <div class="tl-row" v-for="y in evolve" :key="y.year">
              <span class="ty">{{ y.year }}</span>
              <span class="tk">
                <span class="chip" v-for="k in y.keywords" :key="k.value"
                      :style="{ opacity: 0.45 + 0.55 * (k.count / y.keywords[0].count) }">
                  {{ k.value }}<b>{{ k.count }}</b>
                </span>
              </span>
            </div>
          </div>
        </div>
      </section>

      <!-- 入库情况管理面板（2026-11-02）：以数据库为准的对账与管理 -->
      <IngestPanel />
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { api } from '../api.js'
import IngestPanel from '../components/IngestPanel.vue'
import { toggleTheme } from '../theme.js'
import * as echarts from 'echarts/core'
import { BarChart, GraphChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, GraphChart, GridComponent, TooltipComponent, CanvasRenderer])

const data = ref(null)
const loading = ref(true)
const error = ref('')
const yearEl = ref(null)
const graphEl = ref(null)
let yearChart = null
let graphChart = null

const fmt = (n) => (n ?? 0).toLocaleString()

const cards = computed(() => {
  const o = data.value?.overview || {}
  return [
    { k: '论文', v: o.papers }, { k: '章节', v: o.sections },
    { k: '文本分块', v: o.chunks }, { k: '实体', v: o.entities },
    { k: '跨文献超边', v: o.hyperedges },
  ]
})
const journals10 = computed(() => (data.value?.journals || []).slice(0, 10))
const keywords15 = computed(() => (data.value?.keywords || []).slice(0, 15))
const evolve = computed(() => (data.value?.yearly_keywords || []).slice(-8))

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

function renderCharts(d) {
  const accent = cssVar('--accent', '#7c9cff')
  const line = cssVar('--line', '#262c38')
  const faint = cssVar('--text-faint', '#6b7382')

  if (yearEl.value && d.yearly.length) {
    yearChart = echarts.init(yearEl.value)
    yearChart.setOption({
      grid: { left: 44, right: 12, top: 20, bottom: 56 },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: {
        type: 'category',
        data: d.yearly.map((y) => y.year),
        axisLabel: { rotate: 45, color: faint, fontSize: 11 },
        axisLine: { lineStyle: { color: line } },
      },
      yAxis: {
        type: 'value', minInterval: 1,
        axisLabel: { color: faint },
        splitLine: { lineStyle: { color: line } },
      },
      series: [{
        type: 'bar',
        data: d.yearly.map((y) => y.count),
        barMaxWidth: 20,
        itemStyle: { color: accent, borderRadius: [3, 3, 0, 0] },
      }],
    })
  } else if (d.yearly.length) {
    console.warn('[insights] yearEl 未挂载，柱图跳过')
  }

  if (graphEl.value && d.cooc.length) {
    const countOf = new Map((d.keywords || []).map((k) => [k.value, k.count]))
    const names = new Set()
    d.cooc.forEach((e) => { names.add(e.a); names.add(e.b) })
    const nodes = [...names].map((n) => {
      const c = countOf.get(n) || 1
      return {
        name: n, value: c,
        symbolSize: Math.min(56, 12 + Math.sqrt(c) * 5),
        itemStyle: { color: accent, opacity: 0.9 },
        label: { show: c >= 8, fontSize: 11, color: '#fff' },
      }
    })
    const links = d.cooc.map((e) => ({
      source: e.a, target: e.b, value: e.w,
      lineStyle: { width: 0.8 + e.w * 0.6, opacity: 0.4 },
    }))
    graphChart = echarts.init(graphEl.value)
    graphChart.setOption({
      tooltip: {
        formatter: (p) => (p.dataType === 'edge'
          ? `${p.data.source} × ${p.data.target}：共现 ${p.data.value} 篇`
          : `${p.name}：出现 ${p.value} 篇`),
      },
      series: [{
        type: 'graph', layout: 'force', roam: true, draggable: true,
        force: { repulsion: 240, edgeLength: 110, gravity: 0.09 },
        data: nodes, links,
        lineStyle: { color: faint, curve: 0.15 },
        emphasis: { focus: 'adjacency' },
      }],
    })
  } else if (d.cooc.length) {
    console.warn('[insights] graphEl 未挂载，共现网络跳过')
  }
}

function onResize() {
  yearChart?.resize()
  graphChart?.resize()
}

onMounted(async () => {
  try {
    data.value = await api.insights()
  } catch (e) {
    error.value = `加载失败：${e.message}`
  }
  loading.value = false
  // ⚠️ 必须先让 v-else-if="data" 分支进 DOM，再拿 ref 初始化 echarts
  // （首版顺序反了：容器尚不存在 → ref 为 null → 两张图静默不渲染）
  await nextTick()
  if (data.value) renderCharts(data.value)
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  yearChart?.dispose()
  graphChart?.dispose()
})
</script>

<style scoped>
.ins-page { max-width: 1180px; margin: 0 auto; padding: 26px 28px 60px; }
.topbar { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }
.back { font-size: 13.5px; color: var(--text-dim); }
.brand .logo { font-family: var(--font-serif); font-size: 25px; font-weight: 700; }
.brand .sub { margin-left: 10px; color: var(--text-faint); font-size: 13px; }
.grow { flex: 1; }
.theme { font-size: 16px; padding: 6px 10px; }

.cards { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 16px; }
.card.c { padding: 14px 16px; text-align: center; }
.card.c .n { font-size: 21px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text); }
.card.c .l { font-size: 12px; color: var(--text-faint); margin-top: 3px; }

.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
.pane { padding: 16px 18px; margin-bottom: 14px; }
.pane h3 { margin: 0 0 12px; font-size: 14.5px; font-weight: 600; color: var(--text); }
.hint { font-size: 12px; font-weight: 400; color: var(--text-faint); }
.chart { width: 100%; }
.chart.tall { height: 300px; }
.chart.graph { height: 430px; }

.bars { display: flex; flex-direction: column; gap: 7px; }
.bar-row { display: grid; grid-template-columns: 170px 1fr 34px; gap: 10px; align-items: center; font-size: 12.8px; }
.bv { color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.track { background: var(--bg-hover); border-radius: 4px; height: 13px; overflow: hidden; }
.track i { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); border-radius: 4px; }
.bc { color: var(--text-faint); text-align: right; font-variant-numeric: tabular-nums; }

.timeline { display: flex; flex-direction: column; gap: 9px; }
.tl-row { display: grid; grid-template-columns: 46px 1fr; gap: 10px; align-items: start; }
.ty { font-size: 13px; font-weight: 600; color: var(--text-dim); font-variant-numeric: tabular-nums; padding-top: 2px; }
.tk { display: flex; flex-wrap: wrap; gap: 6px; }
.tk .chip b { font-weight: 400; color: var(--text-faint); margin-left: 4px; font-size: 11px; }

.state { padding: 60px; text-align: center; color: var(--text-dim); }
.state.err { color: var(--warn); }
.empty { color: var(--text-faint); font-size: 13px; padding: 14px; text-align: center; }

@media (max-width: 900px) {
  .cards { grid-template-columns: repeat(3, 1fr); }
  .grid2 { grid-template-columns: 1fr; }
}
</style>
