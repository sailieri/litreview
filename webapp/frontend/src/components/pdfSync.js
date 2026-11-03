// 双栏 PDF 滚动同步（原文查看器 ↔ 中文译文查看器）
// 锁定状态模块级共享；滚动事件走 EventTarget 总线，按文档进度比例同步。
import { reactive } from 'vue'

export const syncState = reactive({ locked: false, lastRatio: 0 })

export const syncBus = new EventTarget()

export function makePaneId() {
  return Math.random().toString(36).slice(2, 10)
}
