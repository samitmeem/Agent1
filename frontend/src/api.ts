/** Thin REST client for the Gateway endpoints defined in Task 4.
 *
 * All requests send the JWT via `credentials: 'include'` (cookie). The base
 * URL defaults to the same origin so the Vite dev-server proxy can forward
 * `/api/*` to the Gateway.
 */
import type { Message, Ticket, User } from './types'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

async function parseJson<T>(response: Response): Promise<T> {
        if (!response.ok) {
                const text = await response.text().catch(() => '')
                throw new ApiError(response.status, text || response.statusText)
        }
        if (response.status === 204) {
                return undefined as T
        }
        return (await response.json()) as T
}

export class ApiError extends Error {
        constructor(public readonly status: number, message: string) {
                super(message)
                this.name = 'ApiError'
        }
}

export async function getCurrentUser(): Promise<User> {
        return parseJson(await fetch(`${API_BASE}/api/me`, { credentials: 'include' }))
}

export async function listTickets(): Promise<Ticket[]> {
        return parseJson(await fetch(`${API_BASE}/api/tickets`, { credentials: 'include' }))
}

export async function getTicket(ticketId: string): Promise<Ticket & { messages: Message[] }> {
        return parseJson(await fetch(`${API_BASE}/api/tickets/${ticketId}`, { credentials: 'include' }))
}

export async function createTicket(subject: string): Promise<Ticket> {
        return parseJson(
                await fetch(`${API_BASE}/api/tickets`, {
                        method: 'POST',
                        credentials: 'include',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ subject }),
                }),
        )
}

export async function startOAuthConnect(provider: 'github' | 'gmail'): Promise<{ authorization_url: string }> {
        return parseJson(
                await fetch(`${API_BASE}/api/oauth/${provider}/connect`, {
                        method: 'POST',
                        credentials: 'include',
                }),
        )
}
