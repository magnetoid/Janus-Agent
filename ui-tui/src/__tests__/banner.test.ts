import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { emblem, EMBLEM_WIDTH, logo, LOGO_WIDTH, taglineFor, TAGLINES } from '../banner.js'
import { DEFAULT_THEME } from '../theme.js'

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
const CLI_BANNER = readFileSync(resolve(REPO_ROOT, 'janus_cli/banner.py'), 'utf8')

/** A piece of art the classic CLI prints, without its Rich color markup. */
function cliArtRows(name: string): string[] {
  const block = new RegExp(`${name} = """([\\s\\S]*?)"""`).exec(CLI_BANNER)

  if (!block) {
    throw new Error(`${name} not found in janus_cli/banner.py`)
  }

  return block[1]!.split('\n').map(row => row.replace(/^\[[^\]]*\]/, '').replace(/\[\/\]$/, ''))
}

// The TUI is also what the dashboard's Chat page embeds. Its banner was left
// behind by the rename and still showed the upstream project's name, staff
// and tagline, so these hold it to what the classic CLI prints.
describe('banner art', () => {
  const colors = DEFAULT_THEME.color
  const logoRows = logo(colors).map(([, text]) => text)
  const emblemRows = emblem(colors).map(([, text]) => text)

  it('draws the same wordmark as the classic CLI', () => {
    expect(logoRows.map(row => row.trimEnd())).toEqual(cliArtRows('JANUS_LOGO').map(row => row.trimEnd()))
  })

  it('draws the same emblem as the classic CLI', () => {
    expect(emblemRows).toEqual(cliArtRows('JANUS_EMBLEM'))
  })

  it('keeps every row of a piece the same width, so the blocks line up', () => {
    expect(new Set(logoRows.map(row => row.length))).toEqual(new Set([LOGO_WIDTH]))
    expect(new Set(emblemRows.map(row => row.length))).toEqual(new Set([EMBLEM_WIDTH]))
  })

  // A gradient shorter than its art would leave the last rows in the
  // fallback color.
  it('gives every row one of the banner colors', () => {
    const bannerColors = [colors.primary, colors.accent, colors.border]

    for (const [color] of [...logo(colors), ...emblem(colors)]) {
      expect(bannerColors).toContain(color)
    }
  })
})

describe('taglineFor', () => {
  const shortest = TAGLINES[TAGLINES.length - 1]!

  it('lists the taglines longest first', () => {
    const lengths = TAGLINES.map(tag => tag.length)

    expect(lengths).toEqual([...lengths].sort((a, b) => b - a))
    expect(new Set(lengths).size).toBe(lengths.length)
  })

  it('never returns a tagline wider than the space it is given', () => {
    for (let width = shortest.length; width <= 200; width++) {
      expect(taglineFor(width).length).toBeLessThanOrEqual(width)
    }
  })

  it('returns the longest tagline that fits', () => {
    for (const tag of TAGLINES) {
      expect(taglineFor(tag.length)).toBe(tag)
    }
  })

  // The shortest has nothing below it, so it is the fallback instead.
  it('steps down to the next tagline one column short of a fit', () => {
    for (const tag of TAGLINES.slice(0, -1)) {
      expect(taglineFor(tag.length - 1)).not.toBe(tag)
    }
  })

  it('falls back to the shortest tagline when nothing fits', () => {
    expect(taglineFor(0)).toBe(shortest)
  })
})
