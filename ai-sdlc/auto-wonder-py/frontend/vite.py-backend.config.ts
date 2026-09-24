import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// HMR 开发配置：代理到 Python 服务 :7002。Java 树里的 vite.config.ts 仍指向 :7001。
export default defineConfig({
  root: __dirname,
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:7002',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:7002',
        ws: true,
      },
    },
  },
});
