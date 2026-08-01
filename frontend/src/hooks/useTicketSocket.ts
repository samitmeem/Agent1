import { useCallback, useEffect, useRef, useState } from 'react'
import type {
        AgentStatusUpdatePayload,
        ClientEnvelope,
        GatewayEnvelope,
        MessageAcceptedPayload,
        ResponseFinalPayload,
        TicketCompletedPayload,
        TicketErrorPayload,
} from '../types'
import { backoffDelay, replayCursorStore, ticketWsUrl, uuid } from '../lib'
import {
        isAgentStatusUpdatePayload,
        isAudioTranscriptPartialPayload,
        isMessageAcceptedPayload,
        isResponseChunkPayload,
        isResponseFinalPayload,
        isTicketCompletedPayload,
        isTicketConnectedPayload,
        isTicketErrorPayload,
} from '../lib/typeGuards'

/** Heartbeat interval (§3: 20s). */
const HEARTBEAT_INTERVAL_MS = 20_000
/** Missed pongs before the socket is considered dead (§3: 2). */
const MAX_MISSED_PONGS = 2
/** Maximum reconnect attempts before giving up. */
const MAX_RECONNECT_ATTEMPTS = 8

/** Outbound events the hook emits to the Gateway. */
export type SocketEvent =
        | { type: 'message:submit'; clientMessageId: string; text: string }
        | { type: 'audio:start'; codec: string; sampleRate: number; channels: number; streamId: string }
        | { type: 'audio:stop'; streamId: string }
        | { type: 'ping' }

export interface TicketSocketState {
        /** `connecting` while the WebSocket is opening/reconnecting, `open` once
         * `ticket:connected` is received, `closed` after a clean close. */
        status: 'connecting' | 'open' | 'closed'
        /** Last replay boundary reported by the Gateway. */
        lastEventId: string | null
        /** Most recent agent status label. */
        agentStatus: string | null
        /** Partial ASR transcript (rendered separately from final messages). */
        partialTranscript: string
        /** Streaming assistant text accumulated from `response:chunk` deltas. */
        streamingText: string
        /** Final assistant message once `response:final` arrives. */
        finalMessage: ResponseFinalPayload | null
        /** Last ticket error, if any. */
        error: TicketErrorPayload | null
        /** Ticket completion payload, set when `ticket:completed` arrives. */
        completed: TicketCompletedPayload | null
        /** Messages accepted by the Gateway (acknowledgements). */
        accepted: MessageAcceptedPayload[]
        /** Agent status updates, in order. */
        statusUpdates: AgentStatusUpdatePayload[]
}

export interface UseTicketSocket {
        state: TicketSocketState
        /** Send a client envelope to the Gateway. Returns false if the socket is
         * not open. */
        send: (event: SocketEvent) => boolean
        /** Send raw binary audio bytes after an `audio:stream` metadata envelope. */
        sendAudioFrame: (streamId: string, sequence: number, bytes: ArrayBuffer | Blob) => boolean
        /** Manually close the socket and stop reconnecting. */
        close: () => void
}

interface SocketRefs {
        ws: WebSocket | null
        /** Monotonic sequence of the last processed outbound event. */
        lastSequence: number
        /** Number of consecutive missed pongs. */
        missedPongs: number
        /** Heartbeat timer id. */
        heartbeatTimer: ReturnType<typeof setInterval> | null
        /** Reconnect timer id. */
        reconnectTimer: ReturnType<typeof setTimeout> | null
        /** Reconnect attempt counter. */
        reconnectAttempts: number
        /** Whether the component is still mounted. */
        mounted: boolean
        /** Whether the user explicitly closed the socket. */
        manualClose: boolean
        /** Pending audio stream id, used to associate binary frames. */
        pendingAudioStreamId: string | null
}

/**
 * `useTicketSocket(ticketId)` — manages the WebSocket lifecycle for a ticket
 * per `AgenticProject.md` §1 steps 2–5 and §3 (reconnect policy).
 *
 * Responsibilities:
 * - Heartbeat ping every 20 s; close after 2 missed pongs.
 * - Reconnect with exponential backoff + jitter (1s, 2s, 4s, 8s, max 20s).
 * - On reconnect, send `ticket:connect` with the most recent durable
 *   `event_id` (replay cursor) persisted to localStorage.
 * - Discard duplicate or older outbound events by `sequence`; request replay
 *   when a sequence gap is detected.
 * - Support both JSON envelopes and binary audio frames.
 */
export function useTicketSocket(ticketId: string | null): UseTicketSocket {
        const [state, setState] = useState<TicketSocketState>({
                status: 'connecting',
                lastEventId: replayCursorStore.get(ticketId ?? ''),
                agentStatus: null,
                partialTranscript: '',
                streamingText: '',
                finalMessage: null,
                error: null,
                completed: null,
                accepted: [],
                statusUpdates: [],
        })

        const refs = useRef<SocketRefs>({
                ws: null,
                lastSequence: 0,
                missedPongs: 0,
                heartbeatTimer: null,
                reconnectTimer: null,
                reconnectAttempts: 0,
                mounted: true,
                manualClose: false,
                pendingAudioStreamId: null,
        })

        const clearHeartbeat = useCallback(() => {
                const r = refs.current
                if (r.heartbeatTimer !== null) {
                        clearInterval(r.heartbeatTimer)
                        r.heartbeatTimer = null
                }
        }, [])

        const clearReconnect = useCallback(() => {
                const r = refs.current
                if (r.reconnectTimer !== null) {
                        clearTimeout(r.reconnectTimer)
                        r.reconnectTimer = null
                }
        }, [])

        const sendEnvelope = useCallback(
                (envelope: ClientEnvelope): boolean => {
                        const r = refs.current
                        if (!r.ws || r.ws.readyState !== WebSocket.OPEN) return false
                        r.ws.send(JSON.stringify(envelope))
                        return true
                },
                [],
        )

        const sendConnect = useCallback(
                (ws: WebSocket, lastEventId: string | null, voiceEnabled: boolean) => {
                        const envelope: ClientEnvelope = {
                                type: 'ticket:connect',
                                ticket_id: ticketId ?? '',
                                correlation_id: uuid(),
                                payload: { last_event_id: lastEventId, voice_enabled: voiceEnabled },
                        }
                        ws.send(JSON.stringify(envelope))
                },
                [ticketId],
        )

        const startHeartbeat = useCallback((ws: WebSocket) => {
                const r = refs.current
                clearHeartbeat()
                r.missedPongs = 0
                r.heartbeatTimer = setInterval(() => {
                        if (ws.readyState !== WebSocket.OPEN) return
                        r.missedPongs += 1
                        ws.send(JSON.stringify({ type: 'ping', ticket_id: ticketId ?? '', correlation_id: uuid(), payload: { timestamp: new Date().toISOString() } }))
                        if (r.missedPongs >= MAX_MISSED_PONGS) {
                                // Consider the socket dead and force a reconnect.
                                ws.close(4001, 'heartbeat timeout')
                        }
                }, HEARTBEAT_INTERVAL_MS)
        }, [clearHeartbeat, ticketId])

        const handleEnvelope = useCallback((envelope: GatewayEnvelope) => {
                const r = refs.current
                // Heartbeat pongs reset the missed-pong counter and are not subject to
                // sequence ordering (they may carry sequence 0 or repeat).
                if (envelope.type === 'pong') {
                        console.log('PONG HANDLED, resetting from', r.missedPongs)
                        r.missedPongs = 0
                        return
                }
                // Sequence handling: discard duplicates and older events.
                if (typeof envelope.sequence === 'number') {
                        if (envelope.sequence <= r.lastSequence) {
                                return // duplicate or older event
                        }
                        if (envelope.sequence > r.lastSequence + 1 && r.lastSequence > 0) {
                                // Gap detected — request replay by reconnecting with the last known
                                // event id. We trigger a controlled close; the reconnect logic will
                                // send `ticket:connect` with the persisted cursor.
                                if (r.ws) r.ws.close(4002, 'sequence gap')
                                return
                        }
                        r.lastSequence = envelope.sequence
                        if (envelope.event_id) {
                                replayCursorStore.set(ticketId ?? '', envelope.event_id)
                        }
                }

                setState((prev) => {
                        switch (envelope.type) {
                                case 'ticket:connected': {
                                        if (!isTicketConnectedPayload(envelope.payload)) {
                                                console.error('[useTicketSocket] Invalid ticket:connected payload', envelope)
                                                return prev
                                        }
                                        const payload = envelope.payload
                                        return {
                                                ...prev,
                                                status: 'open',
                                                lastEventId: payload.last_event_id ?? prev.lastEventId,
                                                agentStatus: payload.ticket_snapshot?.agent_status ?? prev.agentStatus,
                                        }
                                }
                                case 'message:accepted': {
                                        if (!isMessageAcceptedPayload(envelope.payload)) {
                                                console.error('[useTicketSocket] Invalid message:accepted payload', envelope)
                                                return prev
                                        }
                                        const payload = envelope.payload
                                        return { ...prev, accepted: [...prev.accepted, payload] }
                                }
                                case 'agent:status_update': {
                                        if (!isAgentStatusUpdatePayload(envelope.payload)) {
                                                console.error('[useTicketSocket] Invalid agent:status_update payload', envelope)
                                                return prev
                                        }
                                        const payload = envelope.payload
                                        return {
                                                ...prev,
                                                agentStatus: payload.status,
                                                statusUpdates: [...prev.statusUpdates, payload],
                                        }
                                }
                                case 'audio:transcript_partial': {
                                        if (!isAudioTranscriptPartialPayload(envelope.payload)) {
                                                console.error('[useTicketSocket] Invalid audio:transcript_partial payload', envelope)
                                                return prev
                                        }
                                        const payload = envelope.payload
                                        return { ...prev, partialTranscript: payload.transcript }
                                }
                                case 'audio:transcript_final': {
                                        return { ...prev, partialTranscript: '' }
                                }
                                case 'response:chunk': {
                                        if (!isResponseChunkPayload(envelope.payload)) {
                                                console.error('[useTicketSocket] Invalid response:chunk payload', envelope)
                                                return prev
                                        }
                                        const payload = envelope.payload
                                        return { ...prev, streamingText: prev.streamingText + payload.delta }
                                }
                                case 'response:final': {
                                        if (!isResponseFinalPayload(envelope.payload)) {
                                                console.error('[useTicketSocket] Invalid response:final payload', envelope)
                                                return prev
                                        }
                                        const payload = envelope.payload
                                        return { ...prev, streamingText: '', finalMessage: payload }
                                }
                                case 'ticket:error': {
                                        if (!isTicketErrorPayload(envelope.payload)) {
                                                console.error('[useTicketSocket] Invalid ticket:error payload', envelope)
                                                return prev
                                        }
                                        const payload = envelope.payload
                                        return { ...prev, error: payload }
                                }
                                case 'ticket:completed': {
                                        if (!isTicketCompletedPayload(envelope.payload)) {
                                                console.error('[useTicketSocket] Invalid ticket:completed payload', envelope)
                                                return prev
                                        }
                                        const payload = envelope.payload
                                        return { ...prev, completed: payload }
                                }
                                default:
                                        return prev
                        }
                })
        }, [ticketId])

        const connect = useCallback(() => {
                if (!ticketId) return
                const r = refs.current
                if (!r.mounted || r.manualClose) return

                setState((prev) => ({ ...prev, status: 'connecting', error: null }))

                const url = ticketWsUrl(ticketId)
                let ws: WebSocket
                try {
                        ws = new WebSocket(url)
                } catch {
                        scheduleReconnectRef.current()
                        return
                }
                r.ws = ws
                ws.binaryType = 'arraybuffer'

                ws.onopen = () => {
                        // Memory-leak guard: if the component unmounted between socket
                        // creation and onopen, close the socket immediately to avoid a leak.
                        if (!r.mounted) {
                                console.warn('[useTicketSocket] Component unmounted before onopen, closing socket')
                                ws.close()
                                return
                        }
                        r.reconnectAttempts = 0
                        const cursor = replayCursorStore.get(ticketId)
                        sendConnect(ws, cursor, false)
                        startHeartbeat(ws)
                }

                ws.onmessage = (event) => {
                        // Binary frames are TTS audio; handled by the TTS queue hook via a
                        // separate listener. Here we only parse JSON envelopes.
                        if (typeof event.data === 'string') {
                                try {
                                        const envelope = JSON.parse(event.data) as GatewayEnvelope
                                        handleEnvelope(envelope)
                                } catch {
                                        // Ignore malformed JSON.
                                }
                        }
                }

                ws.onclose = () => {
                        clearHeartbeat()
                        if (r.manualClose || !r.mounted) {
                                setState((prev) => ({ ...prev, status: 'closed' }))
                                return
                        }
                        scheduleReconnectRef.current()
                }

                ws.onerror = () => {
                        // The close handler will trigger reconnect.
                        try {
                                ws.close()
                        } catch {
                                /* ignore */
                        }
                }
                // eslint-disable-next-line react-hooks/exhaustive-deps
        }, [ticketId, clearHeartbeat, handleEnvelope, sendConnect, startHeartbeat])

        const scheduleReconnect = useCallback(() => {
                const r = refs.current
                if (!r.mounted || r.manualClose) return
                if (r.reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
                        setState((prev) => ({
                                ...prev,
                                status: 'closed',
                                error: {
                                        code: 'RECONNECT_EXHAUSTED',
                                        message: 'Unable to reconnect to the ticket socket.',
                                        retryable: false,
                                },
                        }))
                        return
                }
                const delay = backoffDelay(r.reconnectAttempts)
                r.reconnectAttempts += 1
                clearReconnect()
                r.reconnectTimer = setTimeout(() => {
                        connectRef.current()
                }, delay)
        }, [clearReconnect])

        const connectRef = useRef(connect)
        const scheduleReconnectRef = useRef(scheduleReconnect)

        useEffect(() => {
                connectRef.current = connect
        }, [connect])

        useEffect(() => {
                scheduleReconnectRef.current = scheduleReconnect
        }, [scheduleReconnect])

        useEffect(() => {
                if (!ticketId) return
                const r = refs.current
                r.mounted = true
                r.manualClose = false
                r.lastSequence = 0
                r.reconnectAttempts = 0
                setState((prev) => ({
                        ...prev,
                        status: 'connecting',
                        lastEventId: replayCursorStore.get(ticketId),
                        agentStatus: null,
                        partialTranscript: '',
                        streamingText: '',
                        finalMessage: null,
                        error: null,
                        completed: null,
                        accepted: [],
                        statusUpdates: [],
                }))
                connect()

                return () => {
                        r.mounted = false
                        r.manualClose = true
                        clearHeartbeat()
                        clearReconnect()
                        if (r.ws) {
                                try {
                                        r.ws.close(1000, 'component unmount')
                                } catch {
                                        /* ignore */
                                }
                        }
                        r.ws = null
                }
                // eslint-disable-next-line react-hooks/exhaustive-deps
        }, [ticketId])

        const send = useCallback(
                (event: SocketEvent): boolean => {
                        const r = refs.current
                        if (!r.ws || r.ws.readyState !== WebSocket.OPEN) return false
                        const correlationId = uuid()
                        switch (event.type) {
                                case 'message:submit':
                                        return sendEnvelope({
                                                type: 'message:submit',
                                                ticket_id: ticketId ?? '',
                                                correlation_id: correlationId,
                                                payload: { client_message_id: event.clientMessageId, text: event.text },
                                        })
                                case 'audio:start':
                                        return sendEnvelope({
                                                type: 'audio:start',
                                                ticket_id: ticketId ?? '',
                                                correlation_id: correlationId,
                                                payload: {
                                                        codec: event.codec,
                                                        sample_rate: event.sampleRate,
                                                        channels: event.channels,
                                                        stream_id: event.streamId,
                                                },
                                        })
                                case 'audio:stop':
                                        return sendEnvelope({
                                                type: 'audio:stop',
                                                ticket_id: ticketId ?? '',
                                                correlation_id: correlationId,
                                                payload: { stream_id: event.streamId },
                                        })
                                case 'ping':
                                        return sendEnvelope({
                                                type: 'ping',
                                                ticket_id: ticketId ?? '',
                                                correlation_id: correlationId,
                                                payload: { timestamp: new Date().toISOString() },
                                        })
                                default:
                                        return false
                        }
                },
                [sendEnvelope, ticketId],
        )

        const sendAudioFrame = useCallback(
                (streamId: string, sequence: number, bytes: ArrayBuffer | Blob): boolean => {
                        const r = refs.current
                        if (!r.ws || r.ws.readyState !== WebSocket.OPEN) return false
                        // Send the metadata envelope first, then the binary frame.
                        const ok = sendEnvelope({
                                type: 'audio:stream',
                                ticket_id: ticketId ?? '',
                                correlation_id: uuid(),
                                payload: { stream_id: streamId, sequence },
                        })
                        if (!ok) return false
                        r.ws.send(bytes)
                        return true
                },
                [sendEnvelope, ticketId],
        )

        const close = useCallback(() => {
                const r = refs.current
                r.manualClose = true
                clearHeartbeat()
                clearReconnect()
                if (r.ws) {
                        try {
                                r.ws.close(1000, 'manual close')
                        } catch {
                                /* ignore */
                        }
                }
                setState((prev) => ({ ...prev, status: 'closed' }))
        }, [clearHeartbeat, clearReconnect])

        return { state, send, sendAudioFrame, close }
}
