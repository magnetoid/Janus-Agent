import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { logo, LOGO_WIDTH } from '../banner.js'
import { DEFAULT_THEME } from '../theme.js'

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')

/** The wordmark the classic CLI prints, without its Rich color markup. */
function cliLogoRows(): string[] {
  const source = readFileSync(resolve(REPO_ROOT, 'janus_cli/banner.py'), 'utf8')
  const block = /JANUS_LOGO = """([\s\S]*?)"""/.exec(source)

  if (!block) {
    throw new Error('JANUS_LOGO not found in janus_cli/banner.py')
  }

  return block[1]!.split('\n').map(row => row.replace(/^\[[^\]]*\]/, '').replace(/\[\/\]$/, ''))
}

describe('banner', () => {
  const rows = logo(DEFAULT_THEME.color).map(([, text]) => text)

  // The TUI is also what the dashboard's Chat page embeds. Its wordmark was
  // left behind by the rename and still spelled the upstream project's name.
  it('draws the same wordmark as the classic CLI', () => {
    expect(rows.map(row => row.trimEnd())).toEqual(cliLogoRows().map(row => row.trimEnd()))
  })

  it('keeps every row the same width, so the block letters line up', () => {
    expect(new Set(rows.map(row => row.length)).size).toBe(1)
    expect(LOGO_WIDTH).toBe(rows[0]!.length)
  })
})
