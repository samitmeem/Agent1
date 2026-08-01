/**
 * Runtime type guards for WebSocket envelope payloads.
 *
 * CRITICAL: All WebSocket payloads MUST be validated with these guards
 * before casting to prevent crashes from malformed Gateway responses.
 */
import type {
        AgentStatusUpdatePayload,
        AudioTranscriptPartialPayload,
        MessageAcceptedPayload,
        ResponseAudioStartPayload,
        ResponseChunkPayload,
        ResponseFinalPayload,
        TicketCompletedPayload,
        TicketConnectedPayload,
        TicketErrorPayload,
} from '../types';

function isObject(value: unknown): value is Record<string, unknown> {
        return typeof value === 'object' && value !== null;
}

export function isTicketConnectedPayload(payload: unknown): payload is TicketConnectedPayload {
        return (
                isObject(payload) &&
                'ticket_snapshot' in payload &&
                typeof payload.replay_complete === 'boolean'
        );
}

export function isMessageAcceptedPayload(payload: unknown): payload is MessageAcceptedPayload {
        return (
                isObject(payload) &&
                'message_id' in payload &&
                typeof payload.message_id === 'string'
        );
}

export function isResponseChunkPayload(payload: unknown): payload is ResponseChunkPayload {
        return (
                isObject(payload) &&
                'delta' in payload &&
                typeof payload.delta === 'string'
        );
}

export function isResponseFinalPayload(payload: unknown): payload is ResponseFinalPayload {
        return (
                isObject(payload) &&
                'message_id' in payload &&
                typeof payload.message_id === 'string' &&
                'text' in payload
        );
}

export function isAgentStatusUpdatePayload(payload: unknown): payload is AgentStatusUpdatePayload {
        return (
                isObject(payload) &&
                'status' in payload &&
                typeof payload.status === 'string'
        );
}

export function isAudioTranscriptPartialPayload(
        payload: unknown,
): payload is AudioTranscriptPartialPayload {
        return (
                isObject(payload) &&
                'transcript' in payload &&
                typeof payload.transcript === 'string'
        );
}

export function isResponseAudioStartPayload(
        payload: unknown,
): payload is ResponseAudioStartPayload {
        return (
                isObject(payload) &&
                'stream_id' in payload &&
                typeof payload.stream_id === 'string'
        );
}

export function isTicketErrorPayload(payload: unknown): payload is TicketErrorPayload {
        return (
                isObject(payload) &&
                'code' in payload &&
                typeof payload.code === 'string' &&
                'message' in payload &&
                typeof payload.message === 'string'
        );
}

export function isTicketCompletedPayload(payload: unknown): payload is TicketCompletedPayload {
        return (
                isObject(payload) &&
                'status' in payload &&
                typeof payload.status === 'string'
        );
}
