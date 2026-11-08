import test from 'node:test'
import assert from 'node:assert/strict'
import os from 'node:os'
import path from 'node:path'
import fs from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { ZipWriter, BlobWriter, TextReader } from '@zip.js/zip.js'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'p20-range-test-'))
await build({
  absWorkingDir: root, entryPoints: ['src/browser_receiver.ts'],
  outfile: path.join(temp, 'receiver.mjs'), bundle: true, platform: 'node', format: 'esm',
  logLevel: 'silent',
})
const { receiveInBrowser } = await import(pathToFileURL(path.join(temp, 'receiver.mjs')).href)

const password = 'synthetic-p20-test-password'
const writer = new ZipWriter(new BlobWriter())
await writer.add('receiver-test.txt', new TextReader('P20 synthetic Range fallback content'), {
  password, encryptionStrength: 3,
})
const archive = new Uint8Array(await (await writer.close()).arrayBuffer())

function setupBrowser() {
  const downloads = []
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent: 'Mozilla/5.0 Macintosh Chrome/156', language: 'zh-CN', platform: 'MacIntel', maxTouchPoints: 0, storage: {} },
  })
  globalThis.window = { setTimeout: () => 0 }
  globalThis.document = {
    createElement: () => ({
      set href(value) { this._href = value },
      set download(name) { this._download = name },
      click() { downloads.push(this._download) },
    }),
  }
  return downloads
}

function ticket(size = archive.length) {
  return {
    transfer_id: 'synthetic-p20',
    artifacts: [{ name: 'synthetic-test.zip', size, browser_resolve_url: 'http://localhost/mock-resolve' }],
    postprocess: { kind: 'archive', format: 'zip-aes256', password },
  }
}

function interceptFetch(mode, trace) {
  let resolutions = 0
  return async (input, init = {}) => {
    const url = String(input)
    if (url.includes('mock-resolve')) {
      resolutions++
      trace.push({ kind: 'resolve', attempt: resolutions })
      return Response.json({ url: `https://multimedia.qfile.qq.com/synthetic-${resolutions}`, resolver: 'qq_runtime' })
    }
    assert.match(url, /^https:\/\/multimedia\.qfile\.qq\.com\/synthetic-\d+$/)
    assert.equal(init.referrerPolicy, 'no-referrer')
    assert.equal(init.referrer, '')
    const range = init.headers?.Range
    if (range) {
      const m = /^bytes=(\d+)-(\d+)$/.exec(range)
      assert.ok(m, 'single byte range required')
      const start = Number(m[1]); const end = Number(m[2])
      trace.push({ kind: 'range', start, end, url })
      if (['reject-all', 'reject-full', 'wrong-full-size'].includes(mode) || (mode === 'refresh-works' && url.includes('synthetic-1'))) {
        return new Response('{"retcode":-5503014,"retmsg":"invalid url"}', { status: 400 })
      }
      const data = archive.slice(start, end + 1)
      return new Response(data, {
        status: 206,
        headers: { 'Content-Length': String(data.byteLength), 'Content-Range': `bytes ${start}-${end}/${archive.length}` },
      })
    }
    trace.push({ kind: 'whole', url })
    if (mode === 'reject-full') return new Response('invalid url', { status: 400 })
    if (mode === 'wrong-full-size') return new Response(archive, { status: 200, headers: { 'Content-Length': String(archive.length + 1) } })
    return new Response(archive, { status: 200, headers: { 'Content-Length': String(archive.length) } })
  }
}

for (const mode of ['refresh-works', 'reject-all']) {
  test(`QQ CDN Range failure recovers: ${mode}`, async () => {
    const downloads = setupBrowser()
    const trace = []
    globalThis.fetch = interceptFetch(mode, trace)
    const events = []; const debug = []
    const result = await receiveInBrowser(ticket(), (event) => events.push(event), (entry) => debug.push(entry))
    assert.equal(result.extracted, 1)
    assert.deepEqual(downloads, ['receiver-test.txt'])
    assert.ok(trace.filter((e) => e.kind === 'resolve').length >= 2)
    assert.equal(trace.filter((e) => e.kind === 'whole').length, mode === 'reject-all' ? 1 : 0)
    assert.equal(events.at(-1)?.phase, 'WAITING_ACK')
    assert.equal(events.at(-1)?.download_bytes_done, archive.length)
    assert.ok(debug.some((e) => e.message.includes('ZIP 解密解压完成')))
    assert.ok(debug.every((e) => !JSON.stringify(e).includes('synthetic-p20-test-password')))
  })
}

test('Oversized in-memory fallback refuses, without downloading whole archive', async () => {
  setupBrowser()
  const trace = []
  globalThis.fetch = interceptFetch('reject-all', trace)
  await assert.rejects(receiveInBrowser(ticket(256 * 1024 * 1024 + 1), () => {}), /文件较大/)
  assert.equal(trace.filter((e) => e.kind === 'whole').length, 0)
})

for (const mode of ['reject-full', 'wrong-full-size']) {
  test(`Malformed full fallback is rejected without a successful extraction: ${mode}`, async () => {
    setupBrowser()
    const trace = []
    globalThis.fetch = interceptFetch(mode, trace)
    const progress = []
    await assert.rejects(
      receiveInBrowser(ticket(), (event) => progress.push(event)),
      mode === 'reject-full' ? /完整下载 HTTP 400/ : /Content-Length 与任务文件大小不一致/,
    )
    assert.equal(trace.filter((e) => e.kind === 'whole').length, 1)
    assert.ok(!progress.some((event) => event.phase === 'WAITING_ACK'))
  })
}

test.after(async () => { await fs.rm(temp, { recursive: true, force: true }) })
