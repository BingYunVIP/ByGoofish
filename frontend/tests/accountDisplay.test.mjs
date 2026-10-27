import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/accountDisplay.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } });
const module = { exports: {} };
vm.runInNewContext(compiled.outputText, { module, exports: module.exports });
const { accountDisplayName, accountDisplayNameWithMeta } = module.exports;
assert.equal(accountDisplayName({ remark: '  店铺备注  ', nickname: '平台昵称', displayName: '旧显示名', id: 'fixture' }), '平台昵称（店铺备注）');
assert.equal(accountDisplayName({ remark: '   ', nickname: '平台昵称', id: 'fixture' }), '平台昵称');
assert.equal(accountDisplayName({ displayName: '旧接口显示名', remark: '旧备注', accountId: 'fixture' }), '旧接口显示名（旧备注）');
assert.equal(accountDisplayName({ nickname: '旧备注', remark: '旧备注', id: 'fixture' }), '旧备注');
assert.equal(accountDisplayName({ accountId: 'fixture' }), 'fixture');
assert.equal(accountDisplayName(undefined, '未选择账号'), '未选择账号');
assert.equal(accountDisplayNameWithMeta({ nickname: '测试平台昵称', remark: '测试大号' }, 1), '测试平台昵称（测试大号 · 1）');
console.log('账号显示测试通过：平台名称加备注、重复名称、空白备注和回退');
