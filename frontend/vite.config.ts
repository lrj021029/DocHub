/// <reference types="vitest/config" />
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// 计算当前目录绝对路径，供路径别名 @ 指向 src 目录
const currentDir = path.dirname(fileURLToPath(import.meta.url));

/**
 * Vite 与 Vitest 统一构建/测试配置。
 * - 开发环境将 /api 请求代理至本地后端 FastAPI（http://127.0.0.1:8000）
 * - 测试环境采用 jsdom 模拟浏览器 DOM，并加载全局 Testing Library 断言
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(currentDir, './src'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        // 不重写路径，直接透传 /api/v1/* 至后端，并保持 SSE 流式响应不被代理缓冲
        ws: false,
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
});
