import { createHash } from 'node:crypto'
import { execFileSync, spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, resolve, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const stable = (value) => Array.isArray(value)
  ? value.map(stable)
  : value && typeof value === 'object'
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]))
    : value

export default async function runRegisteredSoakScenarios() {
  if (process.env.P16_SOAK_WRAPPER_CHILD === '1') return

  const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
  const projectRoot = resolve(frontendDir, '../..')
  const implementationDir = resolve(projectRoot, 'implementation')
  let sourceMarker
  try {
    sourceMarker = readFileSync(resolve(projectRoot, 'SOURCE_REVISION'), 'utf8')
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
    if (process.env.GITHUB_REPOSITORY === 'tevriqorg/public-ci') {
      throw new Error('P16_SOURCE_REVISION_MARKER_REQUIRED')
    }
    console.log('[P16_SOAK_MATRIX] skipped outside the source-mapped public-ci mirror')
    return
  }
  const sourceSha = sourceMarker.match(/^commit=([0-9a-f]{40})$/m)?.[1]
  if (!sourceSha) throw new Error('P16_SOURCE_REVISION_MARKER_INVALID')

  const repositoryRoot = execFileSync('git', ['rev-parse', '--show-toplevel'], {
    cwd: projectRoot,
  }).toString('utf8').trim()
  const implementationPrefix = `${relative(repositoryRoot, implementationDir).replaceAll('\\', '/')}/`
  const dirtyPaths = execFileSync('git', ['status', '--porcelain=v1', '-z', '--untracked-files=all'], {
    cwd: projectRoot,
  }).toString('utf8').split('\0').filter(Boolean)
    .map((entry) => entry.slice(3))
    .filter((path) => path.startsWith(implementationPrefix))
  console.log(`[P16_SOURCE_STATUS] ${JSON.stringify(dirtyPaths.slice(0, 30))}`)

  const stateDir = resolve(process.env.RUNNER_TEMP || implementationDir, 'p16-soak-matrix-state')
  const scenarioIds = Array.from({ length: 36 }, (_, index) => `S${String(index + 1).padStart(3, '0')}`)
  // These scenarios deliberately remain BLOCKED until their full catalog
  // injection matrices are implemented. Component regressions are not
  // evidence for the omitted system-level subcases.
  const expectedBlocked = new Set([
    'S022', 'S028', 'S029', 'S030', 'S036',
  ])
  for (const scenarioId of scenarioIds) {
    const execution = spawnSync('python', [
      resolve(implementationDir, 'scripts', 'p16_soak.py'),
      'scenario', '--scenario-id', scenarioId,
      '--state', stateDir, '--source-sha', sourceSha,
    ], {
      cwd: projectRoot,
      encoding: 'utf8',
      timeout: 240_000,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        P16_SOAK_WRAPPER_CHILD: '1',
        P16_SOAK_MATRIX_CHILD: '1',
      },
    })
    if (execution.error) {
      const detail = execution.error?.code || execution.stderr?.trim() || `EXIT_${execution.status}`
      throw new Error(`P16_${scenarioId}_WRAPPER_FAILED:${detail}`)
    }

    let result
    try {
      result = JSON.parse(execution.stdout.trim())
    } catch {
      throw new Error(`P16_${scenarioId}_WRAPPER_OUTPUT_INVALID`)
    }
    const records = result.results || []
    const expectedStatus = expectedBlocked.has(scenarioId) ? 'BLOCKED' : 'PASS'
    if ((expectedStatus === 'PASS' && execution.status !== 0) ||
        (expectedStatus === 'BLOCKED' && execution.status !== 1) ||
        result.day_credit !== false ||
        result.gate !== 'G1_CANDIDATE_ONLY' || records.length !== 1 ||
        records[0].scenario_id !== scenarioId || records[0].source_sha !== sourceSha ||
        records[0].status !== expectedStatus ||
        (expectedStatus === 'PASS' && (result.complete !== true ||
          records[0].result?.timed_out !== false || records[0].result?.exit_code !== 0)) ||
        (expectedStatus === 'BLOCKED' && result.complete !== false)) {
      throw new Error(`P16_${scenarioId}_WRAPPER_RESULT_INVALID`)
    }

    const record = records[0]
    const recordBytes = readFileSync(resolve(stateDir, 'runs', `${createHash('sha256')
      .update(JSON.stringify(stable(record))).digest('hex')}.json`))
    const evidenceHash = createHash('sha256').update(recordBytes).digest('hex')
    const evidence = JSON.parse(recordBytes.toString('utf8'))
    if (evidenceHash !== createHash('sha256').update(JSON.stringify(stable(record))).digest('hex') ||
        JSON.stringify(stable(evidence)) !== JSON.stringify(stable(record))) {
      throw new Error(`P16_${scenarioId}_EVIDENCE_HASH_MISMATCH`)
    }
    console.log(`[P16_SOAK_EVIDENCE] ${JSON.stringify({
      scenario_id: scenarioId,
      source_sha: sourceSha,
      status: record.status,
      reason: record.reason || null,
      day_credit: false,
      gate: 'G1_CANDIDATE_ONLY',
      evidence_path: relative(projectRoot, resolve(stateDir, 'runs', `${evidenceHash}.json`)),
      evidence_sha256: evidenceHash,
      record,
    })}`)
  }
}
