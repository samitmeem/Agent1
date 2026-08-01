/** Shared TypeScript types mirroring the WebSocket envelope and event payloads
 * defined in `AgenticProject.md` §2 and `contracts/events.py`.
 *
 * The Gateway emits one JSON envelope per event. Binary TTS audio frames are
 * preceded by a `response:audio_start` envelope and delivered as binary
 * WebSocket frames.
 */

export type TicketStatus =
        | 'open'
        | 'queued'
        | 'running'
        | 'waiting_for_user'
        | 'resolved'
        | 'failed'

export type AgentStatus =
        | 'idle'
        | 'planning'
        | 'generating'
        | 'calling_tool'
        | 'awaiting_authorization'
        | 'synthesizing_voice'
        | 'completed'

/** Server-generated envelope for outbound events. */
export interface GatewayEnvelope<TPayload = Record<string, unknown>> {
        event_id: string
        type: EventType
        ticket_id: string
        sequence: number
        correlation_id?: string
        payload: TPayload
        sent_at: string
}

/** Client -> Gateway envelopes do not carry `event_id` or `sequence`. */
export interface ClientEnvelope<TPayload = Record<string, unknown>> {
        type: EventType
        ticket_id: string
        correlation_id: string
        payload: TPayload
}

export type EventType =
        | 'ticket:connect'
        | 'ticket:connected'
        | 'message:submit'
        | 'message:accepted'
        | 'audio:start'
        | 'audio:stream'
        | 'audio:stop'
        | 'audio:transcript_partial'
        | 'audio:transcript_final'
        | 'agent:status_update'
        | 'response:chunk'
        | 'response:audio_start'
        | 'response:audio_end'
        | 'response:final'
        | 'ticket:completed'
        | 'ticket:error'
        | 'ping'
        | 'pong'

// ---------------------------------------------------------------------------
// Payload shapes (subset used by the client)
// ---------------------------------------------------------------------------
export interface TicketConnectPayload {
        last_event_id: string | null
        voice_enabled: boolean
}

export interface TicketConnectedPayload {
        ticket_snapshot: {
                id: string
                status: TicketStatus
                agent_status: AgentStatus
                messages: Message[]
        }
        replay_complete: boolean
        last_event_id: string | null
}

export interface MessageSubmitPayload {
        client_message_id: string
        text: string
}

export interface MessageAcceptedPayload {
        message_id: string
        client_message_id: string
        queued: boolean
}

export interface AudioStartPayload {
        codec: string
        sample_rate: number
        channels: number
        stream_id: string
}

export interface AudioStreamPayload {
        stream_id: string
        sequence: number
}

export interface AudioStopPayload {
        stream_id: string
}

export interface AudioTranscriptPartialPayload {
        stream_id: string
        transcript: string
}

export interface AudioTranscriptFinalPayload {
        stream_id: string
        transcript: string
        message_id: string
}

export interface AgentStatusUpdatePayload {
        status: AgentStatus
        label?: string
        tool_name?: string
        detail?: string
}

export interface ResponseChunkPayload {
        sequence: number
        delta: string
}

export interface ResponseAudioStartPayload {
        stream_id: string
        mime_type: string
        sample_rate: number
}

export interface ResponseAudioEndPayload {
        stream_id: string
}

export interface ResponseFinalPayload {
        message_id: string
        text: string
        citations: Array<Record<string, unknown>>
        actions: Array<Record<string, unknown>>
}

export interface TicketCompletedPayload {
        status: TicketStatus
        selected_model?: string
        fallback_reason?: string
}

export interface TicketErrorPayload {
        code: string
        message: string
        retryable: boolean
        correlation_id?: string
}

export interface PingPongPayload {
        timestamp: string
}

// ---------------------------------------------------------------------------
// REST models
// ---------------------------------------------------------------------------
export type MessageRole = 'user' | 'assistant' | 'system'

export interface Message {
        id: string
        ticket_id: string
        role: MessageRole
        text: string
        created_at: string
}

export interface Ticket {
        id: string
        user_id: string
        status: TicketStatus
        agent_status: AgentStatus
        subject: string
        created_at: string
        updated_at: string
}

export interface User {
        id: string
        email: string
        display_name: string
}

/** Narrow an unknown envelope to a typed one by `type`. */
export function isEnvelopeOfType<T extends EventType>(
        envelope: GatewayEnvelope,
        type: T,
): boolean {
        return envelope.type === type
}

