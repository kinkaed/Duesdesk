import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

// The frontend stage copies an explicit allowlist, so that editing a source file
// does not invalidate the cached dependency layer. That is worth having, but it
// makes the build depend on a list maintained by hand, and a module that is
// imported but not listed fails in exactly one place: `docker build`. Nothing
// else in the suite looks, `pnpm run build` passes against the working tree, and
// tsconfig's include ("*.ts") resolves the import locally whether or not the file
// was copied. The failure surfaces as TS2307 on a CI step whose name gives no
// hint that a file list is involved.
//
// theme-bridge.ts was added this way: the theme work imported it, every local
// gate stayed green, and only the image build broke.
test('every module the frontend imports is copied into the image build', () => {
  const dockerfile = read('Dockerfile');
  const frontendCopy = dockerfile
    .split('\n')
    .find(line => /^\s*COPY\b/.test(line) && line.includes('tsconfig.json'));
  assert.ok(frontendCopy, 'no frontend COPY line in the Dockerfile');

  const copied = new Set(
    frontendCopy.replace(/^\s*COPY\s+/, '').replace(/\s+\.\/\s*$/, '').trim().split(/\s+/),
  );

  const missing = [];
  for (const file of [...copied].filter(name => /\.(ts|tsx|mjs)$/.test(name))) {
    for (const [, specifier] of read(file).matchAll(/from '(\.[^']+)'/g)) {
      const base = specifier.replace(/^\.\//, '').replace(/\.[jt]sx?$/, '');
      const resolved = [...copied].some(name => name.replace(/\.[jt]sx?$/, '') === base);
      if (!resolved) missing.push(`${file} imports '${specifier}', which is not copied`);
    }
  }

  assert.deepEqual(missing, [], `the image build cannot see:\n  ${missing.join('\n  ')}`);
});

// The allowlist only stays small if it is deliberate, so assert it has not grown
// into "copy everything", which would defeat the layer caching it exists for.
test('the frontend COPY line stays an allowlist', () => {
  const frontendCopy = read('Dockerfile')
    .split('\n')
    .find(line => /^\s*COPY\b/.test(line) && line.includes('tsconfig.json'));
  const files = frontendCopy.replace(/^\s*COPY\s+/, '').replace(/\s+\.\/\s*$/, '').trim().split(/\s+/);
  assert.ok(files.length <= 12, `allowlist has grown to ${files.length} entries: ${files.join(' ')}`);
  assert.ok(!files.includes('.'), 'COPY . . would copy node_modules and defeat the layer caching');
});