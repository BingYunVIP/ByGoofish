import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/itemCounts.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } });
const module = { exports: {} };
vm.runInNewContext(compiled.outputText, { module, exports: module.exports });
const counts = module.exports.onSaleItemCounts([
  { cookie_id: 'small', listing_status: 'off_shelf' },
  { cookie_id: 'large', listing_status: 'on_sale' },
  { cookie_id: 'large', listing_status: 'off_shelf' },
  { cookie_id: 'legacy', listing_status: null },
  { cookie_id: 'legacy' },
  { cookie_id: 'legacy', listing_status: 'unknown' },
]);
assert.equal(counts.get('small') || 0, 0);
assert.equal(counts.get('large'), 1);
assert.equal(counts.get('legacy') || 0, 0);
console.log('商品账号计数测试通过：只计在售，保留下架记录供页面管理');
