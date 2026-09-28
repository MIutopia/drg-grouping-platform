import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期把 /api 代理到本地后端（uvicorn :8000），生产由反向代理承担
export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,   // bind 0.0.0.0 so LAN hosts (192.0.2.0.x) can reach the dev server
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' }
  }
})
