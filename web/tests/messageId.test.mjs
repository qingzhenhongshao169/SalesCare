import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/messageId.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
function load(crypto) {
  const exports = {};
  runInNewContext(outputText, { exports, crypto, Uint8Array });
  return exports.createMessageId;
}

test('secure origins use the native UUID implementation with its receiver', () => {
  const crypto = { randomUUID() { assert.equal(this, crypto); return 'native-id'; } };
  assert.equal(load(crypto)(), 'native-id');
});

test('LAN HTTP without randomUUID produces distinct valid message IDs', () => {
  const crypto = { getRandomValues(bytes) { return webcrypto.getRandomValues(bytes); } };
  const createId = load(crypto);
  const ids = Array.from({ length: 1000 }, () => createId());
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('unsupported browsers receive a readable error instead of TypeError', () => {
  for (const crypto of [undefined, {}]) {
    assert.throws(load(crypto), /当前浏览器不支持发送消息/);
  }
});
