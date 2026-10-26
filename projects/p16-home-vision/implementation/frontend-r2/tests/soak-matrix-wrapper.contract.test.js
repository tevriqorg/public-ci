import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { createMatrixStateDir, readSourceRevision } from './soak-scenarios.global-setup.js'

test('required matrix source mapping fails closed when marker is missing', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'p16-matrix-marker-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  assert.throws(() => readSourceRevision(path.join(root, 'SOURCE_REVISION'), { required: true }),
    { message: 'P16_SOURCE_REVISION_MARKER_REQUIRED' })
  assert.equal(readSourceRevision(path.join(root, 'SOURCE_REVISION')), null)
})

test('matrix source mapping accepts one exact source commit and rejects malformed markers', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'p16-matrix-marker-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const marker = path.join(root, 'SOURCE_REVISION')
  const source = 'a'.repeat(40)
  writeFileSync(marker, `repository=tevriqorg/public-ci\ncommit=${source}\n`)
  assert.equal(readSourceRevision(marker, { required: true }), source)
  writeFileSync(marker, 'commit=not-an-exact-sha\n')
  assert.throws(() => readSourceRevision(marker, { required: true }),
    { message: 'P16_SOURCE_REVISION_MARKER_INVALID' })
  writeFileSync(marker, `commit=${source}\ncommit=${'b'.repeat(40)}\n`)
  assert.throws(() => readSourceRevision(marker, { required: true }),
    { message: 'P16_SOURCE_REVISION_MARKER_INVALID' })
})

test('matrix state fallback is unique and outside the implementation tree', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'p16-matrix-state-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const implementation = path.join(root, 'implementation')
  const fallbackTemp = path.join(root, 'temp')
  const first = createMatrixStateDir({ implementationDir: implementation, fallbackTemp })
  const second = createMatrixStateDir({ implementationDir: implementation, fallbackTemp })
  assert.notEqual(first, second)
  assert.ok(first.startsWith(`${fallbackTemp}${path.sep}`))
  assert.ok(!first.startsWith(`${implementation}${path.sep}`))
  assert.ok(!second.startsWith(`${implementation}${path.sep}`))
})

test('matrix state honors runner temp while allocating a fresh directory', (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'p16-matrix-runner-temp-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const implementation = path.join(root, 'implementation')
  const runnerTemp = path.join(root, 'runner-temp')
  const state = createMatrixStateDir({ runnerTemp, implementationDir: implementation })
  assert.ok(state.startsWith(`${runnerTemp}${path.sep}`))
  assert.ok(!state.startsWith(`${implementation}${path.sep}`))
})
