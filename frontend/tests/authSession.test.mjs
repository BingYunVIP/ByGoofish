import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import { AxiosError } from 'axios';

const moduleURL = source => 'data:text/javascript;base64,' + Buffer.from(
  ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
).toString('base64');
const sessionURL = moduleURL(await readFile(new URL('../lib/authSession.ts', import.meta.url), 'utf8'));
const source = (await readFile(new URL('../lib/request.ts', import.meta.url), 'utf8'))
  .replace("'./authSession'", JSON.stringify(sessionURL)).replace("'axios'", JSON.stringify(import.meta.resolve('axios')));
const { default: request } = await import(moduleURL(source));
const { clearAuthSession } = await import(sessionURL);

function setup(token = 'fixture-current') {
  const values = new Map([['auth_token', token]]);
  globalThis.localStorage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  globalThis.window = new EventTarget();
  const gates = [], logouts = [];
  window.addEventListener('auth:password-change-required', event => gates.push(event.detail));
  window.addEventListener('auth:logout', event => logouts.push(event.detail));
  return { gates, logouts };
}

async function reject(status, data, beforeResponse = () => {}) {
  return request.get('/fixture', { adapter: async config => {
    beforeResponse();
    throw new AxiosError('Request failed with status code ' + status, undefined, config, undefined,
      { status, data, headers: {}, statusText: 'Forbidden', config });
  }}).catch(error => error);
}

test('bootstrap 403 signals current-session gate and preserves session', async () => {
  const { gates, logouts } = setup();
  const error = await reject(403, { code: 'password_change_required', detail: '请先修改登录密码' });
  assert.equal(gates.length, 1);
  assert.equal(logouts.length, 0);
  assert.equal(localStorage.getItem('auth_token'), 'fixture-current');
  assert.equal(error.message, '请先修改登录密码');
});

test('ordinary permission 403 stays a readable permission error', async () => {
  const { gates } = setup();
  const error = await reject(403, { detail: '需要管理员权限' });
  assert.deepEqual(gates, []);
  assert.equal(error.message, '需要管理员权限');
});

test('late bootstrap 403 cannot gate a newer login', async () => {
  const { gates } = setup('fixture-old');
  await reject(403, { code: 'password_change_required' }, () => localStorage.setItem('auth_token', 'fixture-new'));
  assert.deepEqual(gates, []);
  assert.equal(localStorage.getItem('auth_token'), 'fixture-new');
});

test('late 401 and credential response cannot clear a newer login', async () => {
  const { logouts } = setup('fixture-old');
  await reject(401, {}, () => localStorage.setItem('auth_token', 'fixture-new'));
  assert.equal(clearAuthSession('fixture-old', { credentialsChanged: true }), false);
  assert.deepEqual(logouts, []);
  assert.equal(localStorage.getItem('auth_token'), 'fixture-new');
});

test('current 401 logs out once and never pretends initialization', async () => {
  const { gates, logouts } = setup();
  await reject(401, {});
  assert.equal(logouts.length, 1);
  assert.deepEqual(gates, []);
  assert.equal(localStorage.getItem('auth_token'), null);
});
