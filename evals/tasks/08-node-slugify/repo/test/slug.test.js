'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { slugify } = require('../src/slug.js');

test('basic', () => assert.equal(slugify('Hello World'), 'hello-world'));
test('collapses punctuation and spaces', () => assert.equal(slugify('Hello,   World!!!'), 'hello-world'));
test('strips edges', () => assert.equal(slugify('  --Hello World--  '), 'hello-world'));
test('null and undefined', () => {
  assert.equal(slugify(null), '');
  assert.equal(slugify(undefined), '');
});
