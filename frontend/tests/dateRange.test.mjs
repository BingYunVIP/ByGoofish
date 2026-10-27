import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import ts from 'typescript';

// 使用项目已有 TypeScript 编译器运行日期工具，验证自然日、时区和 DST 边界。
process.env.TZ = 'America/New_York';
const source = readFileSync(new URL('../components/DateRangeFilter.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } });
const module = { exports: {} };
// 此处只验证日期运算；实际 PageTabs 和输入交互由浏览器冒烟覆盖。
const require = createRequire(import.meta.url);
vm.runInNewContext(compiled.outputText, { module, exports: module.exports, require: name => name === './ui' ? {} : require(name), Date });
const { dateRangeBounds, defaultDateRange, timestampInRange } = module.exports;
const now = new Date('2026-10-20T15:00:00-04:00');
const bounds = preset => dateRangeBounds({ preset, start: '', end: '' }, now);
assert.equal(defaultDateRange.preset, '7');
assert.equal(bounds('1').startTime, '2026-10-20T04:00:00.000Z');
assert.equal(bounds('1').endTime, '2026-10-21T04:00:00.000Z');
assert.equal(bounds('yesterday').startTime, '2026-10-19T04:00:00.000Z');
assert.equal(bounds('yesterday').endTime, bounds('1').startTime);
assert.equal(bounds('3').startTime, '2026-10-18T04:00:00.000Z');
assert.equal(bounds('7').startTime, '2026-10-14T04:00:00.000Z');
assert.equal(bounds('30').startTime, '2026-09-21T04:00:00.000Z');
const custom = dateRangeBounds({ preset: 'custom', start: '2026-10-01', end: '2026-10-03' }, now);
assert.equal(custom.startTime, '2026-10-01T04:00:00.000Z');
assert.equal(custom.endTime, '2026-10-04T04:00:00.000Z');
assert.equal(dateRangeBounds({ preset: 'custom', start: '', end: '2026-10-03' }, now), null);
assert.equal(dateRangeBounds({ preset: 'custom', start: '2026-10-04', end: '2026-10-03' }, now), null);
const dst = dateRangeBounds({ preset: 'custom', start: '2026-03-08', end: '2026-03-08' });
assert.equal(Date.parse(dst.endTime) - Date.parse(dst.startTime), 23 * 60 * 60 * 1000);
assert.equal(timestampInRange(Date.parse(custom.startTime), custom), true);
assert.equal(timestampInRange(Date.parse(custom.startTime) / 1000, custom), true);
assert.equal(timestampInRange(Date.parse(custom.endTime), custom), false);
assert.equal(timestampInRange(Date.parse(custom.startTime) - 1, custom), false);
assert.equal(timestampInRange(NaN, custom), false);
assert.equal(timestampInRange(0, custom), false);
console.log('日期范围测试通过：默认7天、六种选择、时区、DST、自定义区间及端点');
