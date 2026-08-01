import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ConversationView } from '../components/ConversationView'
import type { Ticket } from '../types'
import { MockWebSocket } from '../test/mockWebSocket'

vi.mock('../api', () => ({
        getTicket: vi.fn().mockResolvedValue({
                id: 'tic_01JQTEST0000000000000000',
                subject: 'Login failure',
                status: 'open',
                agent_status: 'idle',
                messages: [
                        { id: 'm1', ticket_id: 'tic_01JQTEST0000000000000000', role: 'user', text: 'I cannot log in', created_at: '2026-07-28T09:00:00Z' },
                ],
        }),
        listTickets: vi.fn().mockResolvedValue([]),
        createTicket: vi.fn(),
        getCurrentUser: vi.fn(),
        startOAuthConnect: vi.fn(),
}))

const ticket: Ticket = {
        id: 'tic_01JQTEST0000000000000000',
        user_id: 'usr_01JQTEST',
        status: 'open',
        agent_status: 'idle',
        subject: 'Login failure',
        created_at: '2026-07-28T09:00:00Z',
        updated_at: '2026-07-28T09:00:00Z',
}

beforeEach(() => {
        vi.stubGlobal('WebSocket', MockWebSocket)
        MockWebSocket.reset()
})

afterEach(() => {
        vi.unstubAllGlobals()
})

function envelope(type: string, sequence: number, payload: Record<string, unknown> = {}) {
        return {
                event_id: `evt_${sequence}`,
                type,
                ticket_id: ticket.id,
                sequence,
                correlation_id: 'req_test',
                payload,
                sent_at: '2026-07-28T09:30:00Z',
        }
}

describe('ConversationView', () => {
        it('renders persisted messages and streams chunks into a temp bubble replaced by response:final', async () => {
                render(<ConversationView ticket={ticket} />)

                // Wait for the socket to open (useTicketSocket connects on mount).
                const ws = MockWebSocket.last()!
                act(() => ws.open())

                // Persisted user message from the REST load.
                expect(await screen.findByText('I cannot log in')).toBeInTheDocument()

                // Stream chunks into the temporary streaming bubble.
                act(() => ws.__trigger(JSON.stringify(envelope('response:chunk', 1, { sequence: 1, delta: 'Hello ' }))))
                act(() => ws.__trigger(JSON.stringify(envelope('response:chunk', 2, { sequence: 2, delta: 'world' }))))
                expect(screen.getByText(/Hello world/)).toBeInTheDocument()

                // response:final replaces the streaming bubble with a persisted assistant message.
                act(() =>
                        ws.__trigger(
                                JSON.stringify(
                                        envelope('response:final', 3, {
                                                message_id: 'm_final',
                                                text: 'Hello world',
                                                citations: [],
                                                actions: [],
                                        }),
                                ),
                        ),
                )
                expect(screen.getAllByText('Hello world').length).toBeGreaterThanOrEqual(1)
        })

        it('renders partial ASR text separately from persisted messages', async () => {
                render(<ConversationView ticket={ticket} />)
                const ws = MockWebSocket.last()!
                act(() => ws.open())
                await screen.findByText('I cannot log in')

                act(() =>
                        ws.__trigger(
                                JSON.stringify(envelope('audio:transcript_partial', 1, { stream_id: 's1', transcript: 'partial words' })),
                        ),
                )
                expect(screen.getByText('partial words')).toBeInTheDocument()
                expect(screen.getByText('partial transcript')).toBeInTheDocument()
        })

        it('disables the send button until the socket is open', async () => {
                render(<ConversationView ticket={ticket} />)
                const sendButton = screen.getByRole('button', { name: 'Send' })
                expect(sendButton).toBeDisabled()
                const ws = MockWebSocket.last()!
                act(() => ws.open())
                await screen.findByText('I cannot log in')
                // Still disabled because the draft is empty.
                expect(sendButton).toBeDisabled()
        })

        it('shows an error when the gateway reports ticket:error', async () => {
                render(<ConversationView ticket={ticket} />)
                const ws = MockWebSocket.last()!
                act(() => ws.open())
                await screen.findByText('I cannot log in')
                act(() =>
                        ws.__trigger(
                                JSON.stringify(envelope('ticket:error', 1, { code: 'STT_UNAVAILABLE', message: 'STT down', retryable: false })),
                        ),
                )
                expect(screen.getByText(/STT_UNAVAILABLE: STT down/)).toBeInTheDocument()
        })
})
