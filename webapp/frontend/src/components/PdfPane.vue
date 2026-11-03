<template>
  <div class="pdf-pane">
    <div class="pdf-bar">
      <button class="btn ghost" :disabled="page <= 1" @click="jump(page - 1)" title="上一页">↑</button>
      <span class="page-ind dim">{{ page }} / {{ numPages || '…' }}</span>
      <button class="btn ghost" :disabled="page >= numPages" @click="jump(page + 1)" title="下一页">↓</button>
      <span class="spacer"></span>
      <template v-if="zh">
        <button
          class="btn ghost lock" :class="{ on: syncState.locked }"
          :title="syncState.locked ? '两侧已同步滚动，点击解锁' : '点击后两侧滚动进度保持一致'"
          @click="toggleLock"
        >{{ syncState.locked ? '已锁定' : '锁定同步' }}</button>
        <span class="dim" style="font-size: 12.5px">中文译文</span>
      </template>
      <span v-if="loading" class="dim" style="font-size: 12.5px"><span class="spinner"></span> 载入中</span>
      <span v-else-if="error" class="err-txt" :title="error">载入失败</span>
    </div>
    <div class="pdf-scroll" ref="scroller" @scroll="onScroll">
      <div class="page-host" ref="host"></div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import * as pdfjsLib from 'pdfjs-dist'
import PdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { syncState, syncBus, makePaneId } from './pdfSync.js'

pdfjsLib.GlobalWorkerOptions.workerSrc = PdfWorker

const props = defineProps({
  pid: { type: Number, required: true },
  zh: { type: Boolean, default: false },   // true = 内嵌中文译文（中英对照面板）
})
const paneId = makePaneId()
let applyingUntil = 0        // 程序化滚动后的静默窗口，防止回环

const scroller = ref(null)
const host = ref(null)
const numPages = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref('')

let doc = null
let renderToken = 0
let pageDivs = []
const resizeObs = new ResizeObserver(debounce(() => rerender(), 300))

function debounce(fn, ms) {
  let t
  return (...a) => {
    clearTimeout(t)
    t = setTimeout(() => fn(...a), ms)
  }
}

async function load() {
  const token = ++renderToken
  destroy()
  loading.value = true
  error.value = ''
  page.value = 1
  try {
    doc = await pdfjsLib.getDocument({
      url: props.zh
        ? `/api/papers/${props.pid}/bilingual/pdf`
        : `/api/papers/${props.pid}/pdf`,
      isEvalSupported: false,
    }).promise
    if (token !== renderToken) return
    numPages.value = doc.numPages
    await nextTick()
    if (scroller.value) resizeObs.observe(scroller.value)
    await renderAll(token)
    // 锁定态下新加载的窗：直接对齐另一侧最后广播的进度
    if (token === renderToken && syncState.locked && syncState.lastRatio && scroller.value) {
      const el = scroller.value
      const max = el.scrollHeight - el.clientHeight
      if (max > 0) {
        applyingUntil = performance.now() + 200
        el.scrollTop = syncState.lastRatio * max
      }
    }
  } catch (e) {
    if (token === renderToken) error.value = String(e?.message || e)
  } finally {
    if (token === renderToken) loading.value = false
  }
}

async function renderAll(token) {
  const hostEl = host.value
  if (!hostEl || !doc) return
  hostEl.innerHTML = ''
  pageDivs = []
  const width = Math.max(320, hostEl.clientWidth - 28)
  const dpr = Math.min(window.devicePixelRatio || 1, 2)

  for (let i = 1; i <= doc.numPages; i++) {
    if (token !== renderToken) return
    const pdfPage = await doc.getPage(i)
    const vp1 = pdfPage.getViewport({ scale: 1 })
    const scale = width / vp1.width
    const vp = pdfPage.getViewport({ scale })

    const wrap = document.createElement('div')
    wrap.className = 'page'
    wrap.dataset.page = String(i)
    const canvas = document.createElement('canvas')
    canvas.width = Math.floor(vp.width * dpr)
    canvas.height = Math.floor(vp.height * dpr)
    canvas.style.width = `${Math.floor(vp.width)}px`
    canvas.style.height = `${Math.floor(vp.height)}px`
    const label = document.createElement('div')
    label.className = 'page-no dim'
    label.textContent = `${i} / ${doc.numPages}`
    wrap.appendChild(canvas)
    wrap.appendChild(label)
    hostEl.appendChild(wrap)
    pageDivs.push(wrap)

    await pdfPage.render({
      canvasContext: canvas.getContext('2d'),
      viewport: vp,
      transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
    }).promise
  }
}

async function rerender() {
  if (!doc || loading.value) return
  await renderAll(++renderToken)
}

function destroy() {
  resizeObs.disconnect()
  const d = doc
  doc = null
  pageDivs = []
  if (host.value) host.value.innerHTML = ''
  if (d) d.destroy().catch(() => {})
}

function onScroll() {
  if (!pageDivs.length || !scroller.value) return
  const top = scroller.value.scrollTop + 80
  let cur = 1
  for (const d of pageDivs) {
    if (d.offsetTop <= top) cur = Number(d.dataset.page)
    else break
  }
  page.value = cur
  // 锁定态：把本窗进度比例广播给另一侧（静默窗口内不发，防回环）
  if (syncState.locked && performance.now() >= applyingUntil) {
    const el = scroller.value
    const max = el.scrollHeight - el.clientHeight
    const ratio = max > 0 ? el.scrollTop / max : 0
    syncState.lastRatio = ratio
    syncBus.dispatchEvent(new CustomEvent('pdfscroll', { detail: { ratio, id: paneId } }))
  }
}

function onSync(e) {
  if (!syncState.locked || e.detail.id === paneId) return
  const el = scroller.value
  if (!el) return
  const max = el.scrollHeight - el.clientHeight
  if (max <= 0) return
  applyingUntil = performance.now() + 200
  el.scrollTop = e.detail.ratio * max
}

function toggleLock() {
  syncState.locked = !syncState.locked
  if (syncState.locked && scroller.value) {
    // 以本窗当前进度为基准，立刻对齐另一侧
    const el = scroller.value
    const max = el.scrollHeight - el.clientHeight
    const ratio = max > 0 ? el.scrollTop / max : 0
    syncState.lastRatio = ratio
    syncBus.dispatchEvent(new CustomEvent('pdfscroll', { detail: { ratio, id: paneId } }))
  }
}

function jump(n) {
  const d = pageDivs[n - 1]
  if (d && scroller.value) scroller.value.scrollTo({ top: d.offsetTop - 8, behavior: 'smooth' })
}

onMounted(() => { syncBus.addEventListener('pdfscroll', onSync); load() })
onBeforeUnmount(() => { syncBus.removeEventListener('pdfscroll', onSync); destroy() })
watch(() => props.pid, load)
</script>

<style scoped>
.pdf-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  background: var(--pdf-bg);
  border-right: 1px solid var(--line);
}
.pdf-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--line);
  flex: none;
}
.spacer { flex: 1; }
.lock {
  font-size: 12.5px;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.lock.on {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}
.page-ind { font-size: 13px; font-variant-numeric: tabular-nums; }
.err-txt { color: var(--warn); font-size: 12.5px; }
.pdf-scroll {
  flex: 1;
  overflow: auto;
  min-height: 0;
}
.page-host { padding: 14px; display: flex; flex-direction: column; align-items: center; gap: 14px; }
.page {
  position: relative;
  box-shadow: var(--shadow);
  background: #fff;
  line-height: 0;
}
.page canvas { display: block; }
.page-no {
  position: absolute;
  right: 8px;
  bottom: -18px;
  font-size: 11.5px;
  line-height: 1;
}
</style>
