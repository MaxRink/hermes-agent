import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'

import { $accentOverride, setAccentOverride } from './accent-override'
import { skinPref, ThemeProvider, useTheme } from './context'
import { persistAccent } from './persist-accent'
import { nousTheme } from './presets'
import type { DesktopTheme } from './types'
import { $userThemes, installUserTheme, resolveTheme } from './user-themes'

let theme: ReturnType<typeof useTheme>

function ThemeProbe() {
  theme = useTheme()

  return null
}

beforeEach(() => {
  window.localStorage.clear()
  $userThemes.set({})
  $activeGatewayProfile.set('default')
})

afterEach(() => {
  cleanup()
  setAccentOverride(null)
})

describe('persistAccent', () => {
  it('saves an accent as the profile theme and returns to Nous on null', () => {
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>
    )

    act(() => {
      setAccentOverride('#8a2be2')
      persistAccent('#8a2be2')
    })
    const saved = theme.themeName

    expect(saved).not.toBe(nousTheme.name)
    expect(skinPref.resolve('default')).toBe(saved)
    expect($accentOverride.get()).toBeNull()

    act(() => persistAccent('not a colour'))
    expect(theme.themeName).toBe(saved)

    act(() => persistAccent(null))
    expect(theme.themeName).toBe(nousTheme.name)
    expect(skinPref.resolve('default')).toBe(nousTheme.name)
  })

  it('retints the active theme, keeping its own colours and CSS, exactly as the preview painted it', () => {
    const ember: DesktopTheme = {
      ...nousTheme,
      name: 'my-campfire',
      label: 'My campfire',
      description: 'A custom skin',
      colors: { ...nousTheme.colors, background: '#fff4e8', foreground: '#2b1a0e', card: '#fffaf3', border: '#e8cfb5' },
      darkColors: { ...nousTheme.darkColors!, background: '#1c120b', foreground: '#f5e6d6', card: '#24170e' },
      customCSS: '.campfire-only { letter-spacing: 0.02em; }'
    }

    installUserTheme(ember)
    skinPref.assign('default', ember.name)

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>
    )

    expect(theme.themeName).toBe(ember.name)

    act(() => setAccentOverride('#e0218a'))
    const previewed = theme.theme

    act(() => persistAccent('#e0218a'))
    const saved = resolveTheme(theme.themeName)!

    expect(saved.name).not.toBe(ember.name)
    expect(saved.customCSS).toBe(ember.customCSS)
    expect(saved.colors.background).toBe(ember.colors.background)
    expect(saved.colors.foreground).toBe(ember.colors.foreground)
    expect(saved.darkColors?.background).toBe(ember.darkColors!.background)
    expect(theme.theme.colors).toEqual(previewed.colors)
    expect(theme.theme.customCSS).toBe(previewed.customCSS)

    // A second accent on the same base replaces the copy instead of adding one.
    act(() => persistAccent('#2ea043'))
    expect(Object.keys($userThemes.get()).sort()).toEqual([ember.name, theme.themeName].sort())
    expect(resolveTheme(theme.themeName)!.colors.background).toBe(ember.colors.background)

    // The theme's own accent puts the theme itself back.
    act(() => persistAccent(null))
    expect(theme.themeName).toBe(ember.name)
  })
})
