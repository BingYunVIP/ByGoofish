import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {normalizeImageUrl} from '../lib/productImages.mjs';
test('product image URLs share backend rules and preserve CDN signatures',()=>{
  const fixtures=JSON.parse(readFileSync(new URL('../../tests/fixtures/product_image_urls.json',import.meta.url),'utf8'));
  for(const [original,expected] of fixtures)assert.equal(normalizeImageUrl(original),expected);
});
