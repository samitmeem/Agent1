import { afterEach, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

// Auto-cleanup React DOM between tests.
afterEach(() => {
        cleanup()
})

// Provide a minimal localStorage shim (jsdom has one, but guard anyway).
beforeEach(() => {
        window.localStorage.clear()
})

// Fake timers are opt-in per test; reset here.
vi.useRealTimers()
