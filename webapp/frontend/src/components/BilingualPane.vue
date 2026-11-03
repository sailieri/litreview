<template>
  <div class="bil-pane">
    <!-- 已有译文：内嵌中文查看器（左栏原文 → 此处译文，构成中英对照） -->
    <div v-if="st?.ready" class="bil-view">
      <PdfPane :pid="pid" zh />
    </div>

    <!-- 翻译中：节点步骤条（实测权重的预估进度 + 翻译段真实计数） -->
    <div v-else-if="st?.running" class="card pad run">
      <div class="pane-label">翻译进行中</div>
      <div class="ov">
        <div class="ov-bar"><i :style="{ width: prog.overall + '%' }"></i></div>
        <span class="ov-num">{{ prog.overall }}%</span>
      </div>
      <ol class="nodes">
        <li v-for="(n, i) in prog.nodes" :key="i" :class="n.state">
          <span class="dot"></span>
          <span class="nlabel">{{ n.label }}</span>
          <span class="nbar"><i :style="{ width: n.progress + '%' }"></i></span>
          <span class="ninfo" v-if="n.state === 'done'">完成</span>
          <span class="ninfo" v-else-if="n.state === 'active'">
            <template v-if="n.total">{{ n.current }}/{{ n.total }} 段</template>
            <template v-else>{{ Math.round(n.progress) }}%</template>
          </span>
          <span class="ninfo wait" v-else>等待</span>
        </li>
      </ol>
      <p class="dim note">完成后译文保存在此篇名下，下次打开直接加载。</p>
    </div>

    <!-- 未翻译：开始按钮 -->
    <div v-else class="card pad start">
      <div class="pane-label">中英对照</div>
      <p class="dim">本篇尚未生成中文译文。左栏为英文原文，点击开始翻译，完成后译文显示在此处。</p>
      <button class="btn primary go" :disabled="starting" @click="start">
        {{ starting ? '启动中…' : '开始翻译' }}
      </button>
      <p v-if="err" class="err">{{ err }}</p>
      <p class="dim note">流程：启动预热 → PDF 解析 → 翻译 → 排版渲染，约 1 分钟。</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import PdfPane from './PdfPane.vue'

const props = defineProps({ pid: { type: Number, required: true } })
const st = ref(null)
const prog = ref({ overall: 0, nodes: [] })
const err = ref('')
const starting = ref(false)
let statusTimer = null
let progTimer = null

async function jget(url) {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json()
}

function ensureTimers() {
  if (!progTimer) progTimer = setInterval(fetchProg, 1000)
  if (!statusTimer) statusTimer = setInterval(pollStatus, 2000)
}
function clearTimers() {
  if (progTimer) { clearInterval(progTimer); progTimer = null }
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null }
}

async function fetchProg() {
  try { prog.value = await jget(`/api/papers/${props.pid}/bilingual/progress`) } catch { /* 抖动重试 */ }
}

async function pollStatus() {
  try {
    const s = await jget(`/api/papers/${props.pid}/bilingual`)
    st.value = s
    err.value = s.error || ''
    if (s.running) ensureTimers()
    else clearTimers()          // ready 或失败 → 停轮询；ready 变化自动切查看器
  } catch { /* 网络抖动下轮询继续 */ }
}

async function start() {
  starting.value = true
  err.value = ''
  try {
    const r = await fetch(`/api/papers/${props.pid}/bilingual`, { method: 'POST' })
    const s = await r.json()
    if (!r.ok) throw new Error(s.detail || `HTTP ${r.status}`)
    st.value = s
    if (s.running) { fetchProg(); ensureTimers() }
  } catch (e) {
    err.value = String(e?.message || e)
  } finally {
    starting.value = false
  }
}

async function reset() {
  clearTimers()
  st.value = null
  prog.value = { overall: 0, nodes: [] }
  err.value = ''
  await pollStatus()
  if (st.value?.running) { fetchProg(); ensureTimers() }
}

onMounted(reset)
onBeforeUnmount(clearTimers)
watch(() => props.pid, reset)
</script>

<style scoped>
.bil-pane {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  padding: 6px 12px 40px;
  box-sizing: border-box;
}
.bil-view { flex: 1; min-height: 0; display: flex; overflow: hidden; }
/* 嵌入的 PdfPane 必须撑满：否则按内容收缩 → 页面渲染宽度塔缩到 320 下限 */
.bil-view > :deep(.pdf-pane) { flex: 1 1 auto; min-width: 0; width: 100%; }

.ov { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.ov-bar {
  flex: 1; height: 10px; border-radius: 5px;
  background: var(--bg-hover); overflow: hidden;
}
.ov-bar i {
  display: block; height: 100%;
  background: var(--accent, #4a7dbd);
  transition: width .6s ease;
}
.ov-num {
  font-size: 13px; font-variant-numeric: tabular-nums;
  color: var(--accent); font-weight: 600; min-width: 44px; text-align: right;
}

.nodes { list-style: none; margin: 0; padding: 0; }
.nodes li {
  display: flex; align-items: center; gap: 10px;
  padding: 5px 0; font-size: 13.5px; color: var(--text-dim);
}
.nodes .dot {
  flex: none; width: 8px; height: 8px; border-radius: 50%;
  background: var(--line);
}
.nodes li.done { color: var(--text); }
.nodes li.done .dot { background: #4a9d5f; }
.nodes li.active { color: var(--accent); font-weight: 600; }
.nodes li.active .dot { background: var(--accent, #4a7dbd); animation: pulse 1.1s ease infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
.nlabel { flex: none; width: 92px; }
.nbar {
  flex: 1; height: 6px; border-radius: 3px;
  background: var(--bg-hover); overflow: hidden;
}
.nbar i { display: block; height: 100%; background: var(--accent, #4a7dbd); transition: width .5s ease; }
.nodes li.done .nbar i { background: #4a9d5f; }
.ninfo {
  flex: none; width: 76px; text-align: right;
  font-size: 12px; font-variant-numeric: tabular-nums;
}
.ninfo.wait { opacity: .55; }

.note { font-size: 12.5px; margin-top: 12px; }
.go { margin-top: 8px; }
.err { color: var(--warn); font-size: 13px; margin-top: 8px; }
</style>
