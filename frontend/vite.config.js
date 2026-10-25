import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 1. 安全防御: 禁止外部请求读取或解析项目非静态配置文件，杜绝扫描触发 import-analysis 异常
    fs: {
      deny: [
        '**/Dockerfile*',
        '**/.git/**',
        '**/.env*',
        '**/*.conf',
        '**/*.sh',
        '**/*.md',
        '**/package*.json',
        '**/vite.config.*',
        '**/eslint.config.*'
      ]
    },
    // 2. 体验防御: 关闭全屏错误遮罩层 (Error Overlay)，防止偶发扫描请求打断正常页面显示
    hmr: {
      overlay: false
    },
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      }
    }
  },
  preview: {
    host: '0.0.0.0',
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      }
    }
  }
})
