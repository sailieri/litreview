<template>
  <div class="import-mask" v-if="open" @click.self="close">
    <div class="import-panel">
      <header class="ip-head">
        <h3>导入 PDF</h3>
        <button class="x" @click="close" title="关闭">×</button>
      </header>

      <div class="ip-pick">
        <input ref="fileEl" type="file" accept=".pdf,application/pdf" @change="onPick" />
        <button class="btn primary" :disabled="!file || busy" @click="upload">
          {{ busy ? '上传中…' : '上传并入库' }}
        </button>
      </div>
      <p class="ip-hint" v-if="file">
        已选：<b>{{ file.name }}</b>（{{ (file.size / 1048576).toFixed(1) }} MB）
      </p>
      <p class="ip-err" v-if="err">{{ err }}</p>

      <h4 class="ip-sub" v-if="jobs.length">任务（{{ jobs.length }}）</h4>
      <div class="ip-jobs">
        <div v-for="jb in jobs" :key="jb.job_id" class="ip-job" :class="jb.state">
          <div class="ip-row">
            <span class="ip-stem" :title="jb.stem">{{ jb.stem }}</span>
            <span class="ip-state">{{ stateLabel(jb.state) }}</span>
          </div>
          <div class="ip-msg">{{ jb.msg }}</div>
          <div class="ip-bar" v-if="jb.state === 'running' || jb.state === 'queued'">
            <i></i>
          </div>
          <div v-if="jb.state === 'need_confirm'" class="ip-confirm">
            <span>⚠ 查重命中，疑似已存在</span>
            <button class="btn small" @click="confirm(jb)">仍要入库</button>
          </div>
          <details v-if="jb.log_tail" class="ip-log">
            <summary>日志</summary>
            <pre>{{ jb.log_tail }}</pre>
          </details>
        </div>
      </div>
      <p class="ip-empty" v-if="!jobs.length">暂无任务</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onUnmounted } from 'vue'
import { api } from '../api.js'

const open = ref(false)
const file = ref(null)
const fileEl = ref(null)
const busy = ref(false)
const err = ref('')
const jobs = ref([])
let timer = null

// 父组件通过 ref 调用：importEl.show()
function show() {
  open.value = true
  refresh()
  if (!timer) timer = setInterval(refresh, 2500)
}
function close() {
  open.value = false
  if (timer) { clearInterval(timer); timer = null }
}
function onPick(e) {
  file.value = e.target.files?.[0] || null
  err.value = ''
}
async function upload() {
  if (!file.value) return
  busy.value = true
  err.value = ''
  try {
    await api.importPdf(file.value)
    file.value = null
    if (fileEl.value) fileEl.value.value = ''
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
async function confirm(jb) {
  try {
    await api.importConfirm(jb.job_id)
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}
async function refresh() {
  try {
    jobs.value = (await api.ingestJobs()).jobs
  } catch { /* 轮询失败静默忽略 */ }
}
const stateLabel = (s) =>
  ({ queued: '排队中', running: '进行中', need_confirm: '待确认', done: '完成', error: '失败' }[s] || s)

defineExpose({ show, close })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.import-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex;
  align-items: center; justify-content: center; z-index: 100; }
.import-panel { background: var(--bg, #fff); color: var(--fg, #222); width: min(680px, 92vw);
  max-height: 84vh; overflow: auto; border-radius: 10px; padding: 18px 20px;
  box-shadow: 0 12px 40px rgba(0,0,0,.25); }
.ip-head { display: flex; align-items: center; justify-content: space-between; }
.ip-head h3 { margin: 0; font-size: 17px; }
.ip-head .x { border: none; background: none; font-size: 22px; cursor: pointer; line-height: 1; }
.ip-pick { display: flex; gap: 10px; align-items: center; margin: 14px 0 6px; flex-wrap: wrap; }
.ip-hint { font-size: 13px; opacity: .75; margin: 4px 0; }
.ip-err { color: #c0392b; font-size: 13px; margin: 6px 0; }
.ip-sub { margin: 16px 0 8px; font-size: 14px; opacity: .85; }
.ip-job { border: 1px solid rgba(128,128,128,.28); border-radius: 8px; padding: 10px 12px;
  margin-bottom: 10px; }
.ip-job.done { border-color: rgba(46,160,67,.5); }
.ip-job.error { border-color: rgba(200,60,50,.5); }
.ip-job.need_confirm { border-color: rgba(210,150,20,.6); }
.ip-row { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.ip-stem { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.ip-state { font-size: 12px; opacity: .8; flex: none; }
.ip-msg { font-size: 12.5px; opacity: .75; margin-top: 3px; }
.ip-bar { height: 4px; border-radius: 2px; background: rgba(128,128,128,.2); margin-top: 8px;
  overflow: hidden; }
.ip-bar i { display: block; width: 35%; height: 100%; background: #3b82f6;
  animation: indet 1.1s ease-in-out infinite; }
@keyframes indet { 0% { margin-left: -35%; } 100% { margin-left: 100%; } }
.ip-confirm { display: flex; align-items: center; gap: 10px; margin-top: 8px; font-size: 13px; }
.ip-log { margin-top: 8px; }
.ip-log summary { cursor: pointer; font-size: 12px; opacity: .7; }
.ip-log pre { font-size: 11px; white-space: pre-wrap; max-height: 200px; overflow: auto;
  background: rgba(128,128,128,.1); padding: 8px; border-radius: 6px; }
.ip-empty { font-size: 13px; opacity: .6; }
</style>
