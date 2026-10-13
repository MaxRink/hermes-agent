/**
 * window-state.json skips a window still at the bounds window sizing gave it,
 * but a user who moves or resizes it afterwards has placed it, and that
 * placement must be saved.
 */
import type { Rectangle } from 'electron'
import { describe, expect, it, vi } from 'vitest'

import { isAppSized, markAppSized, registerWindowSizing } from './window-growth'

const ipc = vi.hoisted(() => new Map<string, (event: unknown, mode: string) => void>())
const WORK_AREA: Rectangle = { height: 1080, width: 1920, x: 0, y: 0 }

vi.mock('electron', () => ({
  ipcMain: { on: (channel: string, listener: (event: unknown, mode: string) => void) => ipc.set(channel, listener) },
  screen: { getDisplayMatching: () => ({ workArea: WORK_AREA }) }
}))

function placedWindow(initial: Rectangle) {
  let bounds = { ...initial }

  return {
    getNormalBounds: () => ({ ...bounds }),
    isMaximized: () => false,
    moveTo: (x: number, y: number) => {
      bounds = { ...bounds, x, y }
    }
  }
}

const GIVEN: Rectangle = { height: 642, width: 602, x: 500, y: 200 }

describe('isAppSized', () => {
  it('is true while the window sits at the bounds sizing gave it', () => {
    const win = placedWindow(GIVEN)

    markAppSized(win, GIVEN)

    expect(isAppSized(win)).toBe(true)
  })

  it('is false once the user moves the window without resizing it', () => {
    const win = placedWindow(GIVEN)

    markAppSized(win, GIVEN)
    win.moveTo(40, 60)

    expect(isAppSized(win)).toBe(false)
  })
})

// The main window as the size IPC sees it: the renderer's webContents, and bounds setBounds moves.
function mainWindow(initial: Rectangle) {
  let bounds = { ...initial }
  const webContents = {}

  return {
    getBounds: () => ({ ...bounds }),
    getNormalBounds: () => ({ ...bounds }),
    isDestroyed: () => false,
    isFullScreen: () => false,
    isMaximized: () => false,
    setBounds: (next: Rectangle) => {
      bounds = { ...next }
    },
    webContents
  }
}

describe('window sizing for the first-run intro', () => {
  it("puts an existing user's window back where it was when the intro ends", () => {
    const placed: Rectangle = { height: 900, width: 1500, x: 40, y: 60 }
    const win = mainWindow(placed)
    // SAFETY: registerWindowSizing reads only the members mainWindow() fakes.
    registerWindowSizing({ enabled: true, mainWindow: () => win as never })
    const size = (mode: string) => ipc.get('hermes:window:size')?.({ sender: win.webContents }, mode)

    size('onboarding')
    expect(win.getBounds()).not.toEqual(placed)

    size('normal')
    expect(win.getBounds()).toEqual(placed)
    expect(isAppSized(win)).toBe(false)
  })
})
