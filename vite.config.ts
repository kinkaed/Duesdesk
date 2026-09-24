import { defineConfig } from 'vite';
export default defineConfig({
  base: '/static/app/',
  build: {
    outDir: 'backend/static/app',
    emptyOutDir: true,
    rollupOptions: {
      input: 'main.tsx',
      output: {entryFileNames:'app.js',chunkFileNames:'chunks/[name]-[hash].js',assetFileNames: asset => asset.names?.some(n=>n.endsWith('.css'))?'style.css':'[name]-[hash][extname]'}
    }
  },
  server: {port:5173,strictPort:true,proxy:{'/api':'http://127.0.0.1:8765','/login':'http://127.0.0.1:8765','/logout':'http://127.0.0.1:8765','/account':'http://127.0.0.1:8765','/receipts':'http://127.0.0.1:8765','/export':'http://127.0.0.1:8765'}}
});
