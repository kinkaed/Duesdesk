import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { default as config } from '../vite.config.ts';
import { BACKEND_PORT, BACKEND_ORIGIN, DJANGO_PATHS } from '../dev.config.mjs';

const PAGES = ['overview', 'members', 'payments', 'reports', 'manage', 'audit', 'profile'];

const plugin = (config.plugins ?? []).find(p => p.name === 'duedesk-spa-page-routes');
assert.ok(plugin, 'expected the SPA page routes plugin to be registered');

// Drive configureServer directly with a stub so this tests real behaviour rather
// than the shape of the source text.
let middleware;
plugin.configureServer({ middlewares: { use: fn => { middleware = fn; } } });
const rewrite = url => { const req = { url }; middleware(req, {}, () => { }); return req.url; };

const target = rewrite('/profile/');

test('every page path is served the dev shell', () => {
  for (const page of PAGES) {
    for (const url of [`/${page}/`, `/${page}`, `/${page}/?month=2026-01`]) {
      assert.equal(rewrite(url), target, `${url} should be rewritten to the shell`);
    }
  }
});

test('the rewrite target lives under base, so Vite does not redirect away the URL', () => {
  // Rewriting to a root /index.html makes Vite 302 the browser to the base, which
  // silently replaces the requested page with the overview: the app reads
  // location.pathname, so a moved URL and the rendered page disagree with no error.
  assert.ok(target.startsWith(config.base), `${target} must sit under base ${config.base}`);
  assert.notEqual(target, '/index.html');
});

test('Django-rendered routes and the app root are left alone', () => {
  for (const url of ['/', '/static/app/', '/api/session/', '/login/', '/signup/', '/logout/',
    '/account/password/', '/account/reset/', '/organizations/<uuid>/logo/', '/health/',
    '/receipts/1/', '/export/', '/export/excel/']) {
    assert.equal(rewrite(url), url, `${url} must be left alone`);
  }
});

test('page paths are not proxied to Django in dev', () => {
  // These are the same shell as /, so Django would answer with react.html asking
  // for the built /static/app/app.js. That module does not exist in dev: Vite
  // replies with index.html as text/html and the browser rejects the script,
  // leaving a blank page. Django owns these paths in production.
  const proxied = Object.keys(config.server.proxy).filter(k => !k.startsWith('^') && PAGES.some(p => k === `/${p}`));
  assert.deepEqual(proxied, [], 'no page path may be proxied in dev');
});

test('the dev backend port is defined once and used by the proxy', () => {
  // serve.py reads $PORT and defaults to 10000 while the proxy targets 8765.
  // Launched by hand they could not see each other and every API call failed to
  // proxy, so the port has to come from one shared place.
  const targets = new Set(Object.values(config.server.proxy));
  assert.equal(targets.size, 1, 'every proxy must target the same backend');
  assert.equal([...targets][0], BACKEND_ORIGIN);
  assert.equal(BACKEND_ORIGIN, `http://127.0.0.1:${BACKEND_PORT}`);

  // The launcher must pass that same port to serve.py, or it starts on 10000.
  const dev = fs.readFileSync(new URL('../scripts/dev.mjs', import.meta.url), 'utf8');
  assert.ok(/PORT:\s*String\(BACKEND_PORT\)/.test(dev), 'scripts/dev.mjs must set PORT from BACKEND_PORT');
  assert.ok(/health\//.test(dev), 'the launcher should wait for the backend before starting Vite');
});

test('the login redirect lands on a real page path', () => {
  // LOGIN_REDIRECT_URL '/': in development Vite answers '/' at its base
  // '/static/app/', so the app booted on a URL that is not one of its pages. It
  // rendered, but a reload or a shared link lost the reader's place.
  const settings = fs.readFileSync(new URL('../backend/config/settings.py', import.meta.url), 'utf8');
  const redirect = settings.match(/LOGIN_REDIRECT_URL\s*=\s*'([^']+)'/)?.[1];
  assert.equal(redirect, '/overview/');
  assert.ok(PAGES.includes(redirect.replace(/^\/|\/$/g, '')),
    'the login redirect must be a page path the app can route');
});

test('pnpm dev runs both processes', () => {
  const pkg = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
  assert.equal(pkg.scripts.dev, 'node scripts/dev.mjs',
    'pnpm dev must start the backend and Vite together, or they cannot find each other');
  assert.ok(pkg.scripts['dev:frontend'], 'a frontend-only script is still useful for CSS work');
});

test('Django routes and the Vite proxy cover the same paths', () => {
  // A path in one and not the other fails in a way that looks like an app bug:
  // proxied but unrouted returns 404, routed but unproxied returns Vite's shell.
  const urls = fs.readFileSync(new URL('../backend/config/urls.py', import.meta.url), 'utf8');
  const proxied = DJANGO_PATHS.map(p => p.replace(/^\//, ''));
  for (const path of proxied) {
    if (path === 'api') continue;
    assert.ok(new RegExp(`path\\('${path}/?`).test(urls) || urls.includes(`'${path}/`),
      `${path} is proxied to Django in dev but has no route in urls.py`);
  }
  // Django-rendered pages must not be swallowed by the SPA shell rewrite.
  for (const path of ['/login', '/signup', '/account', '/receipts', '/export', '/organizations']) {
    assert.ok(DJANGO_PATHS.includes(path), `${path} is Django-rendered and must stay proxied`);
    assert.ok(!PAGES.includes(path.slice(1)), `${path} must not also be an app page path`);
  }
});

test('work.tsx, urls.py and vite.config.ts agree on every page path', () => {
  // The path list is written out in three places because each has a different job:
  // the app renders from it, Django routes the shell, and Vite rewrites it in dev.
  // Nothing at runtime checks they match, so assert it here instead.
  const work = fs.readFileSync(new URL('../work.tsx', import.meta.url), 'utf8');
  const urls = fs.readFileSync(new URL('../backend/config/urls.py', import.meta.url), 'utf8');
  const vite = fs.readFileSync(new URL('../vite.config.ts', import.meta.url), 'utf8');

  const workPaths = Object.fromEntries([...work.matchAll(/(\w+):'\/(\w+)\/'/g)]
    .filter(m => PAGES.includes(m[2])).map(m => [m[2], `/${m[2]}/`]));
  assert.deepEqual(Object.keys(workPaths).sort(), [...PAGES].sort(),
    'PAGE_PATHS in work.tsx must cover exactly the seven pages');

  const urlPages = [...urls.matchAll(/for page in \(([^)]*)\)/g)]
    .flatMap(m => [...m[1].matchAll(/'(\w+)'/g)].map(x => x[1])).filter(p => PAGES.includes(p));
  assert.deepEqual(urlPages.sort(), [...PAGES].sort(), 'urls.py must route the shell for every page');

  const vitePages = [...(vite.match(/SPA_PATHS=\[([^\]]*)\]/)?.[1] ?? '')
    .matchAll(/'(\w+)'/g)].map(m => m[1]);
  assert.deepEqual(vitePages.sort(), [...PAGES].sort(), 'SPA_PATHS must cover every page');

  for (const page of PAGES) assert.equal(workPaths[page], `/${page}/`, `${page} path must be /${page}/`);
});
