import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTicketSocket } from '../hooks/useTicketSocket'
import { MockWebSocket } from '../test/mockWebSocket'

const TICKET_ID = 'tic_01JQTEST0000000000000000'

function envelope(type: string, sequence: number, payload: Record<string, unknown> = {}, eventId = `evt_${sequence}`) {
        return {
                event_id: eventId,
                type,
                ticket_id: TICKET_ID,
                sequence,
                correlation_id: 'req_test',
                payload,
                sent_at: '2026-07-28T09:30:00Z',
        }
}

beforeEach(() => {
        vi.stubGlobal('WebSocket', MockWebSocket)
        MockWebSocket.reset()
})

afterEach(() => {
        vi.unstubAllGlobals()
        vi.useRealTimers()
})

describe('useTicketSocket', () => {
        it('sends ticket:connect with the replay cursor on open', async () => {
                vi.useFakeTimers()
                const { result } = renderHook(() => useTicketSocket(TICKET_ID))
                await act(async () => { await vi.advanceTimersByTimeAsync(0) })
                const ws = MockWebSocket.last()!
                act(() => ws.open())

                // Gateway acknowledges the connection.
                act(() => ws.__trigger(JSON.stringify(envelope('ticket:connected', 1, {
                        ticket_snapshot: { id: TICKET_ID, status: 'open', agent_status: 'idle', messages: [] },
                        replay_complete: true,
                        last_event_id: null,
                }))))

                expect(result.current.state.status).toBe('open')
                const connect = ws.sent[0] as string
                const parsed = JSON.parse(connect)
                expect(parsed.type).toBe('ticket:connect')
                expect(parsed.ticket_id).toBe(TICKET_ID)
                expect(parsed.payload.last_event_id).toBeNull()
        })

        it('reconnects with exponential backoff after a close', async () => {
                vi.useFakeTimers()
                renderHook(() => useTicketSocket(TICKET_ID))
                await act(async () => { await vi.advanceTimersByTimeAsync(0) })
                const first = MockWebSocket.last()!
                act(() => first.open())
                act(() => first.close(4001, 'heartbeat timeout'))

                // First reconnect attempt ~1s + jitter.
                await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
                const second = MockWebSocket.last()!
                expect(second).not.toBe(first)
                expect(second.url).toBe(first.url)
        })

        it('discards duplicate or older outbound events by sequence', async () => {
                vi.useFakeTimers()
                const { result } = renderHook(() => useTicketSocket(TICKET_ID))
                await act(async () => { await vi.advanceTimersByTimeAsync(0) })
                const ws = MockWebSocket.last()!
                act(() => ws.open())

                act(() => ws.__trigger(JSON.stringify(envelope('response:chunk', 5, { sequence: 5, delta: 'Hello' }))))
                expect(result.current.state.streamingText).toBe('Hello')

                // Older sequence — discarded.
                act(() => ws.__trigger(JSON.stringify(envelope('response:chunk', 4, { sequence: 4, delta: 'OLD' }))))
                expect(result.current.state.streamingText).toBe('Hello')

                // Duplicate sequence — discarded.
                act(() => ws.__trigger(JSON.stringify(envelope('response:chunk', 5, { sequence: 5, delta: 'DUP' }))))
                expect(result.current.state.streamingText).toBe('Hello')
        })

        it('requests replay by closing the socket when a sequence gap is detected', async () => {
                vi.useFakeTimers()
                const { result } = renderHook(() => useTicketSocket(TICKET_ID))
                await act(async () => { await vi.advanceTimersByTimeAsync(0) })
                const ws = MockWebSocket.last()!
                act(() => ws.open())

                act(() => ws.__trigger(JSON.stringify(envelope('response:chunk', 2, { sequence: 2, delta: 'A' }))))
                expect(result.current.state.streamingText).toBe('A')

                // Gap: sequence jumps from 2 to 5 — should trigger a close (replay request).
                const closeSpy = vi.spyOn(ws, 'close')
                act(() => ws.__trigger(JSON.stringify(envelope('response:chunk', 5, { sequence: 5, delta: 'B' }))))
                expect(closeSpy).toHaveBeenCalled()
        })

        it('persists the replay cursor to localStorage', async () => {
                vi.useFakeTimers()
                renderHook(() => useTicketSocket(TICKET_ID))
                await act(async () => { await vi.advanceTimersByTimeAsync(0) })
                const ws = MockWebSocket.last()!
                act(() => ws.open())

                act(() => ws.__trigger(JSON.stringify(envelope('response:chunk', 1, { sequence: 1, delta: 'x' }, 'evt_abc'))))
                expect(window.localStorage.getItem(`replay:${TICKET_ID}`)).toBe('evt_abc')
        })

        it('sends heartbeat pings every 20s and closes after 2 missed pongs', async () => {
                vi.useFakeTimers()
                renderHook(() => useTicketSocket(TICKET_ID))
                await act(async () => { await vi.advanceTimersByTimeAsync(0) })
                const ws = MockWebSocket.last()!
                act(() => ws.open())
                const closeSpy = vi.spyOn(ws, 'close')

                // First heartbeat: missedPongs -> 1.
                await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
                expect(ws.sent.some((m: unknown) => String(m).includes('"ping"'))).toBe(true)

                // Second heartbeat: missedPongs -> 2 -> close.
                await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
                expect(closeSpy).toHaveBeenCalled()
        })

        it('resets missed pongs on pong', async () => {
                vi.useFakeTimers()
                renderHook(() => useTicketSocket(TICKET_ID))
                await act(async () => { await vi.advanceTimersByTimeAsync(0) })
                const ws = MockWebSocket.last()!
                act(() => ws.open())
                const closeSpy = vi.spyOn(ws, 'close')

                await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
                act(() => ws.__trigger(JSON.stringify(envelope('pong', 0, { timestamp: 'now' }))))
                await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
                expect(closeSpy).not.toHaveBeenCalled()
        })

        it('sends message:submit with a client_message_id', async () => {
                vi.useFakeTimers()
                const { result } = renderHook(() => useTicketSocket(TICKET_ID))
                await act(async () => { await vi.advanceTimersByTimeAsync(0) })
                const ws = MockWebSocket.last()!
                act(() => ws.open())

                let ok = false
                act(() => {
                        ok = result.current.send({ type: 'message:submit', clientMessageId: 'cid-1', text: 'hi' })
                })
                expect(ok).toBe(true)
                const sent = ws.sent[ws.sent.length - 1] as string
                const parsed = JSON.parse(sent)
                expect(parsed.type).toBe('message:submit')
                expect(parsed.payload.client_message_id).toBe('cid-1')
                expect(parsed.payload.text).toBe('hi')
        })

        it('returns false when sending while the socket is not open', async () => {
                const { result } = renderHook(() => useTicketSocket(TICKET_ID))
                let ok = true
                act(() => {
                        ok = result.current.send({ type: 'message:submit', clientMessageId: 'cid-2', text: 'hi' })
                })
                expect(ok).toBe(false)
        })
})
