import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { api } from '../api.ts';
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; delete globalThis.document; delete globalThis.location; });
test('successful response and CSRF on writes', async () => {
 globalThis.document = { cookie: 'csrftoken=release-token' };
 globalThis.fetch = async (url, options) => {
  assert.equal(options.headers['X-CSRFToken'], 'release-token');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.method, 'POST');
  return Response.json({id:1});
 };
 assert.deepEqual(await api('/api/members/', {name:'Sample'}), {id:1});
});
test('network failure rejects instead of reporting success', async () => {
 globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
 await assert.rejects(api('/api/overview/'), /Failed to fetch/);
});
test('expired session redirects to login', async () => {
 let target; globalThis.location = {assign: value => {target=value;}};
 globalThis.fetch = async () => new Response('', {status:401});
 await assert.rejects(api('/api/overview/'), /sign in again/);
 assert.equal(target, '/login/');
});
test('HTML server errors produce a usable error', async () => {
 globalThis.fetch = async () => new Response('<html>Error</html>', {status:500});
 await assert.rejects(api('/api/overview/'), /Please try again/);
});
test('validation and permission errors remain visible', async () => {
 globalThis.fetch = async () => Response.json({error:'Invalid amount'}, {status:400});
 await assert.rejects(api('/api/overview/'), /Invalid amount/);
 globalThis.fetch = async () => new Response('', {status:403});
 await assert.rejects(api('/api/overview/'), /permission/);
});
