import { useEffect, useRef, useState } from 'react'
import { getTicket } from '../api'
import type { Message, Ticket } from '../types'
import { useAudioRecorder } from '../hooks/useAudioRecorder'
import { useTicketSocket } from '../hooks/useTicketSocket'
import { useTtsAudioQueue } from '../hooks/useTtsAudioQueue'
import { uuid } from '../lib'
import { sanitizeText } from '../lib/sanitize'
import { AgentStatusBadge, StatusBadge } from './StatusBadge'

interface ConversationViewProps {
        ticket: Ticket
}

export function ConversationView({ ticket }: ConversationViewProps) {
        const [messages, setMessages] = useState<Message[]>([])
        const [draft, setDraft] = useState('')
        const [loading, setLoading] = useState(true)
        const [error, setError] = useState<string | null>(null)

        const { state: socketState, send, sendAudioFrame } = useTicketSocket(ticket.id)
        const tts = useTtsAudioQueue()
        const scrollRef = useRef<HTMLDivElement>(null)

        const handleAudioFrame = (streamId: string, sequence: number, bytes: Blob) => {
                bytes.arrayBuffer().then((buf) => {
                        sendAudioFrame(streamId, sequence, buf)
                })
        }
        const recorder = useAudioRecorder(handleAudioFrame)

        useEffect(() => {
                let cancelled = false
                setLoading(true)
                setError(null)
                getTicket(ticket.id)
                        .then((detail) => {
                                if (cancelled) return
                                setMessages(detail.messages ?? [])
                        })
                        .catch((err) => {
                                if (cancelled) return
                                setError(err instanceof Error ? err.message : 'Failed to load ticket')
                        })
                        .finally(() => {
                                if (!cancelled) setLoading(false)
                        })
                return () => {
                        cancelled = true
                }
        }, [ticket.id])

        // When a final assistant message arrives, append it to the persisted list.
        useEffect(() => {
                if (socketState.finalMessage) {
                        setMessages((prev) => [
                                ...prev,
                                {
                                        id: socketState.finalMessage!.message_id,
                                        ticket_id: ticket.id,
                                        role: 'assistant',
                                        text: socketState.finalMessage!.text,
                                        created_at: new Date().toISOString(),
                                },
                        ])
                }
        }, [socketState.finalMessage, ticket.id])

        // Auto-scroll to bottom on new content.
        useEffect(() => {
                if (scrollRef.current) {
                        scrollRef.current.scrollTop = scrollRef.current.scrollHeight
                }
        }, [messages, socketState.streamingText, socketState.partialTranscript])

        const handleSubmit = (event: React.FormEvent) => {
                event.preventDefault()
                const text = draft.trim()
                if (!text || socketState.status !== 'open') return
                const clientMessageId = uuid()
                send({ type: 'message:submit', clientMessageId, text })
                setMessages((prev) => [
                        ...prev,
                        {
                                id: clientMessageId,
                                ticket_id: ticket.id,
                                role: 'user',
                                text,
                                created_at: new Date().toISOString(),
                        },
                ])
                setDraft('')
        }

        const handleStartRecording = async () => {
                const ok = await recorder.start()
                if (ok && recorder.state.streamId) {
                        send({
                                type: 'audio:start',
                                codec: recorder.state.mimeType ?? 'audio/webm;codecs=opus',
                                sampleRate: 48000,
                                channels: 1,
                                streamId: recorder.state.streamId,
                        })
                }
        }

        const handleStopRecording = () => {
                if (recorder.state.streamId) {
                        send({ type: 'audio:stop', streamId: recorder.state.streamId })
                }
                recorder.stop()
        }

        return (
                <section className="conversation">
                        <header className="conversation__header">
                                <div>
                                        <h2>{ticket.subject || ticket.id}</h2>
                                        <div className="conversation__badges">
                                                <StatusBadge status={ticket.status} />
                                                <AgentStatusBadge status={socketState.agentStatus} />
                                                <span className="badge badge--ghost">{socketState.status}</span>
                                        </div>
                                </div>
                                <div className="conversation__tts">
                                        <button type="button" onClick={tts.toggleMute} aria-pressed={tts.state.muted}>
                                                {tts.state.muted ? 'Unmute' : 'Mute'}
                                        </button>
                                        <button type="button" onClick={tts.stop}>Stop audio</button>
                                </div>
                        </header>

                        {error && <p className="error">{error}</p>}
                        {socketState.error && (
                                <p className="error">
                                        {socketState.error.code}: {socketState.error.message}
                                </p>
                        )}

                        <div className="conversation__scroll" ref={scrollRef}>
                                {loading && <p className="muted">Loading…</p>}
                                {messages.map((message) => (
                                        <article key={message.id} className={`message message--${message.role}`}>
                                                <header className="message__role">{message.role}</header>
                                                <p
                                                        className="message__text"
                                                        dangerouslySetInnerHTML={{ __html: sanitizeText(message.text) }}
                                                />
                                        </article>
                                ))}
                                {socketState.streamingText && (
                                        <article className="message message--assistant message--streaming">
                                                <header className="message__role">assistant (streaming)</header>
                                                <p
                                                        className="message__text"
                                                        dangerouslySetInnerHTML={{ __html: sanitizeText(socketState.streamingText) }}
                                                />
                                        </article>
                                )}
                                {socketState.partialTranscript && (
                                        <article className="message message--partial">
                                                <header className="message__role">partial transcript</header>
                                                <p
                                                        className="message__text"
                                                        dangerouslySetInnerHTML={{ __html: sanitizeText(socketState.partialTranscript) }}
                                                />
                                        </article>
                                )}
                        </div>

                        <form onSubmit={handleSubmit} className="composer">
                                <input
                                        type="text"
                                        value={draft}
                                        onChange={(e) => setDraft(e.target.value)}
                                        placeholder="Type a message…"
                                        aria-label="Message"
                                        disabled={socketState.status !== 'open'}
                                />
                                <button type="submit" disabled={socketState.status !== 'open' || !draft.trim()}>
                                        Send
                                </button>
                                {recorder.state.status === 'recording' ? (
                                        <button type="button" onClick={handleStopRecording} className="composer__record composer__record--active">
                                                Stop
                                        </button>
                                ) : (
                                        <button
                                                type="button"
                                                onClick={handleStartRecording}
                                                disabled={recorder.state.status === 'denied' || recorder.state.status === 'unsupported'}
                                                className="composer__record"
                                        >
                                                Record
                                        </button>
                                )}
                                {recorder.state.status === 'denied' && <span className="muted">Microphone denied</span>}
                                {recorder.state.status === 'unsupported' && <span className="muted">Recording unsupported</span>}
                        </form>
                </section>
        )
}
