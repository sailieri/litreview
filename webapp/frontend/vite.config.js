import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// litread 前端开发/构建配置
// 开发：npm run dev → :8190（/api 代理到后端 :8790）
// 生产：npm run build → dist/ 由 webapp/server.py 单进程托管
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 8190,
    proxy: { '/api': 'http://127.0.0.1:8790' },
  },
  build: {
    chunkSizeWarningLimit: 1500,
  },
})
