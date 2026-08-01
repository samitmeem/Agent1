/** Small helpers shared by hooks and components. */

/** Generate a UUID v4 string. Falls back to a Math.random-based shim if
 * `crypto.randomUUID` is unavailable (older jsdom). */
export function uuid(): string {
        if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
                return crypto.randomUUID()
        }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
                const r = (Math.random() * 16) | 0
                const v = c === 'x' ? r : (r & 0x3) | 0x8
                return v.toString(16)
        })
}

/** Build a WebSocket URL for a ticket, honoring `VITE_WS_URL` when set. */
export function ticketWsUrl(ticketId: string): string {
        const base = import.meta.env.VITE_WS_URL
        if (base) {
                const trimmed = base.replace(/\/$/, '')
                const scheme = trimmed.startsWith('http') ? trimmed.replace(/^http/, 'ws') : trimmed
                return `${scheme}/ws/tickets/${ticketId}`
        }
        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        return `${proto}//${window.location.host}/ws/tickets/${ticketId}`
}

/** Backoff schedule from §3: 1s, 2s, 4s, 8s, max 20s, plus jitter. */
export function backoffDelay(attempt: number): number {
        const base = Math.min(1000 * 2 ** attempt, 20000)
        const jitter = Math.random() * 250
        return base + jitter
}

/** Promise-based sleep. */
export function sleep(ms: number): Promise<void> {
        return new Promise((resolve) => setTimeout(resolve, ms))
}

/** Stable localStorage access guarded against SSR/throwing. */
export const replayCursorStore = {
        get(ticketId: string): string | null {
                try {
                        return window.localStorage.getItem(`replay:${ticketId}`)
                } catch {
                        return null
                }
        },
        set(ticketId: string, eventId: string): void {
                try {
                        window.localStorage.setItem(`replay:${ticketId}`, eventId)
                } catch {
                        /* ignore quota / privacy mode */
                }
        },
        clear(ticketId: string): void {
                try {
                        window.localStorage.removeItem(`replay:${ticketId}`)
                } catch {
                        /* ignore */
                }
        },
}
