/**
 * First-run onboarding as this window runs it: one chat in the default profile, opened by the
 * backend's `onboarding.start`, ended by the answer to the first-task card or by Skip setup.
 *
 * This module is the one writer of `$onboardingPhase` and of the `onboarding` presence surface. The
 * surface is open while a run is starting or its chat is on screen; in any other chat the app behaves
 * as it does outside onboarding (tips, the sign-in offer). The backend owns whether onboarding is due
 * (`onboarding.state`) and writes the install-level flag when a run ends.
 */

import type {
  OnboardingEndedPayload,
  OnboardingRunStateResult,
  OnboardingStartResult
} from '@hermes/shared'
import { atom, computed } from 'nanostores'

import { getRuntimeI18nLocale } from '@/i18n/runtime'
import type { GatewayRequester } from '@/lib/yolo-session'

import { ackFreeTierNotice } from './free-tier'
import { clearFreeTierIntro } from './onboarding'
import { $onboardingPhase } from './onboarding-phase'
import { markOnboardingDecided, ONBOARDING_DECIDE_DEADLINE_MS, setOnboardingSurfaceActive } from './onboarding-presence'
import { $onboardingSession } from './onboarding-session'
import { ensureGatewayProfile, newSessionInProfile } from './profile'
import { $connection, $gatewayState, $selectedStoredSessionId } from './session'
import { dropSetupBeat } from './setup-beat'

export type OnboardingRequest = <T = unknown>(
  method: string,
  params?: Parameters<GatewayRequester>[1],
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<T>

// A cold first boot answers onboarding.start late; the intro's way-out offer bounds the wait instead.
// A start with no intro (Settings' Run again) has no way out on screen, so it keeps the request's default deadline.
const NO_DEADLINE = 0

/** Opens a stored session on screen, replacing the route (`useSessionActions().resumeSession`). */
export type OnboardingResume = (storedId: string, replaceRoute?: boolean) => Promise<void>

/** Opens a stored chat on screen the way the launch does (`wiring.tsx::adoptSessionRoute`); the main
 *  window's shell sets it. Settings' Run onboarding again opens its chat through it. */
export const $onboardingChatOpener = atom<null | OnboardingResume>(null)

/** How the last run in this window ended; the end-of-onboarding surfaces wake on it. */
export const $onboardingEnded = atom<'done' | 'skipped' | null>(null)

/** When the end-of-onboarding surfaces (the local-model offer) may start their settle wait: the run
 *  ended, or the user first left its chat mid-run. Null until then. */
export const $onboardingSettleFrom = atom<null | number>(null)

// The backend runs onboarding in its root profile whichever profile the app was on; every ending
// stays there, because the chats the run leaves behind live there.
const ONBOARDING_PROFILE = 'default'

/** The run's chat has been on screen at least once; until then a route still opening is not a leave. */
const $onboardingChatSeen = atom(false)

/** A run is starting (its chat not on screen yet), or its chat is the one on screen. */
const $onboardingOnScreen = computed(
  [$onboardingPhase, $onboardingSession, $selectedStoredSessionId, $onboardingChatSeen],
  (phase, session, selected, seen) => phase === 'shown' && (!session || !seen || session.storedId === selected)
)

computed([$onboardingSession, $selectedStoredSessionId], (session, selected) =>
  Boolean(session && session.storedId === selected)
).subscribe(onChat => {
  if (onChat) {
    $onboardingChatSeen.set(true)
  }
})

$onboardingOnScreen.subscribe(onScreen => {
  setOnboardingSurfaceActive('onboarding', onScreen)

  if (!onScreen && $onboardingPhase.get() === 'shown' && $onboardingSettleFrom.get() === null) {
    $onboardingSettleFrom.set(Date.now())
  }
})

/** Same machine as this app (`local`), or a backend somewhere else (`remote`). Only the local
 *  connection is this machine: a remote backend with the same OS and architecture is still another
 *  computer. */
const factsSource = (): 'local' | 'remote' => ($connection.get()?.mode === 'local' ? 'local' : 'remote')

/** The first-run intro around a start (`components/onboarding/first-run/kickoff.ts`). */
export interface OnboardingIntroHooks {
  /** Before `onboarding.start`: the boot overlay holds. `skip`: the user went on without setup while
   *  the start was slow, which counts as Skip setup. */
  starting: (skip: () => void) => void
  /** The start answered, the chat not open yet. */
  started: (started: OnboardingStartResult) => void
  /** The chat is the one on screen. */
  opened: (started: OnboardingStartResult) => void
  failed: () => void
}

interface StartOptions {
  /** Points the window's gateway at the profile onboarding runs in (`store/profile.ts::ensureGatewayProfile`). */
  ensureProfile?: (profile: string) => Promise<void>
  intro?: OnboardingIntroHooks
  rerun?: boolean
}

/** Open the onboarding chat (a new one for a rerun, else the run in progress or a new one). */
export async function startOnboarding(
  request: OnboardingRequest,
  resume: OnboardingResume,
  { ensureProfile = ensureGatewayProfile, intro, rerun = false }: StartOptions = {}
): Promise<boolean> {
  // A rerun withdraws the old run's card; its skipped_all must not match this window's session.
  $onboardingSession.set(null)
  $onboardingPhase.set('shown')
  $onboardingEnded.set(null)
  $onboardingChatSeen.set(false)

  let skipped = false

  intro?.starting(() => {
    skipped = true
    intro?.failed()
    leaveSkipped()
  })

  try {
    await ensureProfile(ONBOARDING_PROFILE)

    // The request stays alive past the way out: its answer names the run the skip ends.
    const started = await request<OnboardingStartResult>(
      'onboarding.start',
      { facts_source: factsSource(), locale: getRuntimeI18nLocale(), rerun },
      intro ? NO_DEADLINE : undefined
    )

    if (skipped) {
      await request('onboarding.skip', { session_id: started.session_id })

      return false
    }

    intro?.started(started)

    // The chat is on screen before the run names it, so the presence never sees a gap.
    await resume(started.stored_session_id, true)
    $onboardingSession.set({ runId: started.run_id, runtimeId: started.session_id, storedId: started.stored_session_id })
    watchRunEnd(request)

    if (intro) {
      intro.opened(started)
      ackFreeTierIntro(request)
    }

    return true
  } catch (error) {
    console.warn('[onboarding] could not start', error)

    if (!skipped) {
      $onboardingSession.set(null)
      $onboardingPhase.set('failed')
      intro?.failed()
    }

    return false
  }
}

// The onboarding intro is the free-tier introduction, as on main: the ready screen must not follow it.
function ackFreeTierIntro(request: OnboardingRequest): void {
  clearFreeTierIntro()
  void ackFreeTierNotice(request).then(acked => {
    if (acked) {
      clearFreeTierIntro()
    }
  })
}

// Skip setup's ending, like main's: the run is over and a fresh chat opens in the onboarding profile.
function leaveSkipped(): void {
  endOnboarding('skipped')
  newSessionInProfile(ONBOARDING_PROFILE)
}

let decision: null | Promise<void> = null

/**
 * The main window's boot check, once per launch: open onboarding when the backend says it is due on
 * this install and the free tier can drive it. First-run surfaces wait until this has answered; the
 * presence surface is set before the answer is published, so they never see a clear moment in between.
 */
export function decideOnboarding(
  request: OnboardingRequest,
  resume: OnboardingResume,
  options?: StartOptions
): Promise<void> {
  decision ??= readAndStart(request, resume, options)

  return decision
}

async function readAndStart(request: OnboardingRequest, resume: OnboardingResume, options?: StartOptions): Promise<void> {
  try {
    // One deadline for the due check: past it the app is released and a late answer opens nothing.
    const state = await request<OnboardingRunStateResult>('onboarding.state', undefined, ONBOARDING_DECIDE_DEADLINE_MS)

    if (state.run && state.eligible) {
      await startOnboarding(request, resume, options)
    }
  } catch (error) {
    console.warn('[onboarding] state could not be read', error)
  } finally {
    markOnboardingDecided()
  }
}

/** The run ended: the model finished it or the user skipped. Later calls for the same run do nothing. */
export function endOnboarding(outcome: 'done' | 'skipped'): void {
  if ($onboardingPhase.get() !== 'shown') {
    return
  }

  // The chat is let go first: a listener that throws on the ending below must not leave this window
  // treating it as the onboarding chat (Skip setup stayed up after a run ended).
  $onboardingSession.set(null)
  $onboardingPhase.set(outcome)
  $onboardingEnded.set(outcome)
  $onboardingSettleFrom.set($onboardingSettleFrom.get() ?? Date.now())
}

/** Skip setup: the backend ends the run and closes the card that waits, and a new chat opens. A failed
 *  skip leaves the run going, so Skip setup stays on offer; the caller reports the error. */
export async function skipOnboarding(request: OnboardingRequest, runtimeId: string): Promise<void> {
  await request('onboarding.skip', { session_id: runtimeId })
  leaveSkipped()
}

/** `onboarding.ended` from the backend, sent to every window: the run this window shows ended (the
 *  first-task card answered, Skip setup in any window, a rerun, its chat deleted). Another run's end,
 *  like the old run a rerun closed, does nothing here. */
export function noteOnboardingEnded({ outcome, run_id }: OnboardingEndedPayload): void {
  const session = $onboardingSession.get()

  if (!session || session.runId !== run_id) {
    return
  }

  dropSetupBeat(session.runtimeId)
  endOnboarding(outcome === 'done' ? 'done' : 'skipped')
}

/** An end this window missed (a reconnect, a backend restart): the run it shows is no longer the run
 *  in progress. */
export async function reconcileOnboarding(request: OnboardingRequest): Promise<void> {
  const session = $onboardingSession.get()

  if (!session || $onboardingPhase.get() !== 'shown') {
    return
  }

  const state = await request<OnboardingRunStateResult>('onboarding.state')

  if ($onboardingSession.get() === session && state.run_id !== session.runId) {
    endOnboarding('skipped')
  }
}

let stopWatching: (() => void) | null = null

// Re-read the run on every reconnect while this window shows one.
function watchRunEnd(request: OnboardingRequest): void {
  stopWatching?.()

  const check = () => void reconcileOnboarding(request).catch(error => console.warn('[onboarding] reconcile', error))
  stopWatching = $gatewayState.listen(state => state === 'open' && check())

  // An end sent while the chat was opening found no session here to end.
  check()
}
