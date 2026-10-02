import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { api, signOut } from '../api.ts';
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
test('sign out posts to the Django logout view and returns to login', async () => {
 globalThis.document = { cookie: 'csrftoken=signout-token' };
 let target, called = '';
 globalThis.location = {assign: value => {target = value;}};
 globalThis.fetch = async (url, options) => {
  called = url;
  assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.headers['X-CSRFToken'], 'signout-token');
  return new Response('', {status:200});
 };
 await signOut();
 assert.equal(called, '/logout/');
 assert.equal(target, '/login/');
});
test('a rejected sign out does not pretend the session ended', async () => {
 let navigated = false;
 globalThis.document = { cookie: 'csrftoken=signout-token' };
 globalThis.location = {assign: () => {navigated = true;}};
 globalThis.fetch = async () => new Response('', {status:403});
 await assert.rejects(signOut(), /did not complete/);
 assert.equal(navigated, false, 'must not navigate away while the session is still live');
});
test('an unreachable server does not pretend the session ended', async () => {
 let navigated = false;
 globalThis.document = { cookie: 'csrftoken=signout-token' };
 globalThis.location = {assign: () => {navigated = true;}};
 globalThis.fetch = async () => {throw new TypeError('Failed to fetch');};
 await assert.rejects(signOut(), /could not reach the server/);
 assert.equal(navigated, false, 'must not navigate away while the session is still live');
});
