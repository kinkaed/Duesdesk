import { defineConfig, type Plugin } from 'vite';
import { BACKEND_ORIGIN, BACKEND_PORT, DJANGO_PATHS } from './dev.config.mjs';

const BASE='/static/app/';

// Every page path the shell owns, mirroring the paths in backend/config/urls.py
// and PAGE_PATHS in work.tsx. The root is not listed: Vite already answers it.
const SPA_PATHS=['overview','members','payments','reports','manage','audit','profile'];

// Because base is not '/', Vite's history fallback only rewrites URLs under the
// base, so a direct load or reload of any page path 404s in dev. Rewriting to the
// base's index.html serves the shell while leaving the browser URL alone, so
// pageFromPath still sees the path it is meant to read.
//
// The target must be under the base. Rewriting to a root '/index.html' makes Vite
// redirect the browser to the base, which silently replaces the requested page
// with the overview: the URL and the rendered page then disagree with no error.
//
// These paths must NOT be proxied to Django: they are the same shell as /, so
// Django answers with react.html asking for the built /static/app/app.js. That
// module does not exist in dev, so Vite replies to it with index.html as text/html
// and the browser rejects the script, leaving a blank page. Django owns these
// paths in production, where the built app.js is real.
function spaPageRoutes():Plugin{
  const route=new RegExp('^/(?:'+SPA_PATHS.join('|')+')/?(\\?.*)?$');
  return {name:'duedesk-spa-page-routes',configureServer(server){
    server.middlewares.use((req,_res,next)=>{
      if(req.url&&route.test(req.url))req.url=BASE+'index.html';
      next();
    });
  }};
}

export default defineConfig({
  base: BASE,
  plugins: [spaPageRoutes()],
  build: {
    outDir: 'backend/static/app',
    emptyOutDir: true,
    rollupOptions: {
      input: 'main.tsx',
      output: {entryFileNames:'app.js',chunkFileNames:'chunks/[name]-[hash].js',assetFileNames: asset => asset.names?.some(n=>n.endsWith('.css'))?'style.css':'[name]-[hash][extname]'}
    }
  },
  server: {port:5173,strictPort:true,proxy:Object.fromEntries([
    ...DJANGO_PATHS.map(path=>[path,BACKEND_ORIGIN]),
    // Django renders the auth and receipt pages, but its own stylesheet lives under /static.
    // Without this the proxied pages load with no CSS at all. Vite only treats string proxy
    // keys beginning with ^ as patterns, so this excludes /static/app/ to keep Vite's base working.
    ['^/static/(?!app/)',BACKEND_ORIGIN],
  ])}
});

export { BACKEND_PORT };
