import { useCallback, useEffect, useRef, useState } from 'react'
import type { ResponseAudioStartPayload } from '../types'

interface QueuedFrame {
        streamId: string
        sequence: number
        bytes: ArrayBuffer
}

interface ActiveStream {
        payload: ResponseAudioStartPayload
        frames: QueuedFrame[]
}

export interface TtsAudioQueueState {
        /** Whether audio is currently playing. */
        playing: boolean
        /** Whether the queue is muted. */
        muted: boolean
        /** Number of queued frames across all active streams. */
        queuedFrames: number
        /** Active stream ids. */
        streamIds: string[]
}

export interface UseTtsAudioQueue {
        state: TtsAudioQueueState
        /** Enqueue a `response:audio_start` envelope to begin a new TTS stream. */
        startStream: (payload: ResponseAudioStartPayload) => void
        /** Enqueue a binary audio frame for the given stream. */
        enqueueFrame: (streamId: string, sequence: number, bytes: ArrayBuffer) => void
        /** Mark a stream complete (`response:audio_end`). */
        endStream: (streamId: string) => void
        /** Toggle mute. When muted, frames are dropped. */
        toggleMute: () => void
        /** Stop playback, clear the queue, and release the audio context. */
        stop: () => void
}

/**
 * Ordered TTS audio queue with mute/stop controls per `AgenticProject.md` §4
 * (Frontend). Frames are played strictly in arrival order per stream. Binary
 * frames arrive from the WebSocket after a `response:audio_start` envelope.
 *
 * The hook decodes each frame via the Web Audio API and schedules playback
 * sequentially so sentences are not interleaved.
 */
export function useTtsAudioQueue(): UseTtsAudioQueue {
        const [state, setState] = useState<TtsAudioQueueState>({
                playing: false,
                muted: false,
                queuedFrames: 0,
                streamIds: [],
        })

        const audioCtxRef = useRef<AudioContext | null>(null)
        const streamsRef = useRef<Map<string, ActiveStream>>(new Map())
        const nextStartTimeRef = useRef(0)
        const mutedRef = useRef(false)
        const mountedRef = useRef(true)

        const ensureContext = useCallback((): AudioContext | null => {
                if (audioCtxRef.current) return audioCtxRef.current
                const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
                if (!Ctor) return null
                try {
                        audioCtxRef.current = new Ctor()
                } catch {
                        return null
                }
                return audioCtxRef.current
        }, [])

        const playStream = useCallback(async (streamId: string) => {
                const ctx = ensureContext()
                if (!ctx) return
                const stream = streamsRef.current.get(streamId)
                if (!stream) return

                while (stream.frames.length > 0) {
                        const frame = stream.frames.shift()!
                        if (mutedRef.current) continue
                        try {
                                const audioBuffer = await ctx.decodeAudioData(frame.bytes.slice(0))
                                const source = ctx.createBufferSource()
                                source.buffer = audioBuffer
                                source.connect(ctx.destination)
                                const startAt = Math.max(ctx.currentTime, nextStartTimeRef.current)
                                source.start(startAt)
                                nextStartTimeRef.current = startAt + audioBuffer.duration
                        } catch {
                                // Skip undecodable frames.
                        }
                }
                setState((prev) => ({
                        ...prev,
                        playing: streamsRef.current.size > 0,
                        queuedFrames: Array.from(streamsRef.current.values()).reduce((n, s) => n + s.frames.length, 0),
                }))
        }, [ensureContext])

        const startStream = useCallback((payload: ResponseAudioStartPayload) => {
                streamsRef.current.set(payload.stream_id, { payload, frames: [] })
                setState((prev) => ({ ...prev, streamIds: [...prev.streamIds, payload.stream_id] }))
        }, [])

        const enqueueFrame = useCallback((streamId: string, sequence: number, bytes: ArrayBuffer) => {
                const stream = streamsRef.current.get(streamId)
                if (!stream) return
                // Insert in sequence order.
                stream.frames.push({ streamId, sequence, bytes })
                stream.frames.sort((a, b) => a.sequence - b.sequence)
                setState((prev) => ({
                        ...prev,
                        playing: true,
                        queuedFrames: Array.from(streamsRef.current.values()).reduce((n, s) => n + s.frames.length, 0),
                }))
                void playStream(streamId)
        }, [playStream])

        const endStream = useCallback((streamId: string) => {
                streamsRef.current.delete(streamId)
                setState((prev) => ({
                        ...prev,
                        streamIds: prev.streamIds.filter((id) => id !== streamId),
                        playing: streamsRef.current.size > 0,
                        queuedFrames: Array.from(streamsRef.current.values()).reduce((n, s) => n + s.frames.length, 0),
                }))
        }, [])

        const toggleMute = useCallback(() => {
                mutedRef.current = !mutedRef.current
                setState((prev) => ({ ...prev, muted: mutedRef.current }))
        }, [])

        const stop = useCallback(() => {
                streamsRef.current.clear()
                nextStartTimeRef.current = 0
                if (audioCtxRef.current) {
                        try {
                                audioCtxRef.current.close()
                        } catch {
                                /* ignore */
                        }
                        audioCtxRef.current = null
                }
                setState({ playing: false, muted: mutedRef.current, queuedFrames: 0, streamIds: [] })
        }, [])

        useEffect(() => {
                mountedRef.current = true
                return () => {
                        mountedRef.current = false
                        streamsRef.current.clear()
                        if (audioCtxRef.current) {
                                try {
                                        audioCtxRef.current.close()
                                } catch {
                                        /* ignore */
                                }
                                audioCtxRef.current = null
                        }
                }
        }, [])

        return { state, startStream, enqueueFrame, endStream, toggleMute, stop }
}
