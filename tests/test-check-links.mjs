import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CHECKER = path.join(ROOT, 'scripts', 'check-links.mjs');

function fixture(markdown) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'check-links-'));
  fs.mkdirSync(path.join(root, 'scripts'));
  fs.mkdirSync(path.join(root, 'docs', 'dir'), {recursive: true});
  fs.copyFileSync(CHECKER, path.join(root, 'scripts', 'check-links.mjs'));
  fs.writeFileSync(path.join(root, 'docs', 'existing.md'), '# Existing\n');
  fs.writeFileSync(path.join(root, 'docs', 'dir', 'asset.txt'), 'asset');
  fs.writeFileSync(path.join(root, 'index.md'), markdown);
  return root;
}

function run(root) {
  return spawnSync(process.execPath, [path.join(root, 'scripts', 'check-links.mjs')], {
    cwd: root,
    encoding: 'utf8',
  });
}

test('comments, directory links, collapsed and shortcut references pass', (t) => {
  const root = fixture(`<!--
[ignored](./missing-comment.md)
-->
[inline](docs/existing.md)
[directory](docs/dir)
[collapsed][]
[shortcut]

[collapsed]: docs/existing.md
[shortcut]: <docs/existing.md>
`);
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const result = run(root);
  assert.equal(result.status, 0, result.stderr);
});

test('broken collapsed and shortcut references are both reported', (t) => {
  const root = fixture(`<!--
[ignored](./missing-comment.md)
-->
[collapsed][]
[shortcut]

[collapsed]: docs/missing-one.md
[shortcut]: docs/missing-two.md
`);
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const result = run(root);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /missing-one\.md/);
  assert.match(result.stderr, /missing-two\.md/);
  assert.doesNotMatch(result.stderr, /missing-comment\.md/);
});
