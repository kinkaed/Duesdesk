// Single source of truth for the local development backend.
//
// vite.config.ts and scripts/dev.mjs both import this. It exists because the dev
// backend and the Vite proxy have to agree on one port: serve.py reads $PORT and
// defaults to 10000, while the proxy targets 8765, so launching the two by hand
// left them unable to see each other and every API call failed to proxy.
export const BACKEND_PORT = 8765;
export const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`;

// Paths Django renders itself. Everything else is a page of the app shell, which
// Vite serves from its own index.html in dev.
export const DJANGO_PATHS = [
  '/api',
  '/login',
  '/signup',
  '/logout',
  '/account',
  '/receipts',
  '/export',
  '/organizations',
];
