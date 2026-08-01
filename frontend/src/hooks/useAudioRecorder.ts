import { useCallback, useEffect, useRef, useState } from 'react'
import { uuid } from '../lib'

/** Preferred codec per §3: `audio/webm;codecs=opus`. */
const PREFERRED_MIME = 'audio/webm;codecs=opus'
const FALLBACK_MIME = 'audio/webm'
/** Chunk interval for streaming frames to the Gateway. */
const CHUNK_INTERVAL_MS = 250

export interface AudioRecorderState {
        /** `idle` before permission, `recording` while capturing, `paused` on pause,
         * `denied` when the user refused permission, `unsupported` when no API. */
        status: 'idle' | 'recording' | 'paused' | 'denied' | 'unsupported' | 'error'
        /** Detected MIME type used for the current stream. */
        mimeType: string | null
        /** Current stream id (UUID), set when recording starts. */
        streamId: string | null
        /** Monotonic frame sequence counter. */
        sequence: number
        /** Last error message, if any. */
        error: string | null
}

export interface UseAudioRecorder {
        state: AudioRecorderState
        /** Request permission and start recording. Returns false on denial. */
        start: () => Promise<boolean>
        /** Pause recording (keeps the stream alive). */
        pause: () => void
        /** Resume a paused recording. */
        resume: () => void
        /** Stop recording and release all resources cleanly. */
        stop: () => void
}

/**
 * `useAudioRecorder(onFrame)` — microphone capture with permission handling,
 * codec detection, chunked recording, pause/resume, and clean teardown per
 * `AgenticProject.md` §4 (Frontend).
 *
 * `onFrame(streamId, sequence, bytes)` is invoked for each chunked frame so
 * the caller can forward it to the Gateway via `audio:stream` + binary frame.
 */
export function useAudioRecorder(
        onFrame: (streamId: string, sequence: number, bytes: Blob) => void,
): UseAudioRecorder {
        const [state, setState] = useState<AudioRecorderState>({
                status: 'idle',
                mimeType: null,
                streamId: null,
                sequence: 0,
                error: null,
        })

        const mediaRecorderRef = useRef<MediaRecorder | null>(null)
        const streamRef = useRef<MediaStream | null>(null)
        const chunkTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
        const streamIdRef = useRef<string | null>(null)
        const sequenceRef = useRef(0)
        const mountedRef = useRef(true)

        const pickMimeType = useCallback((): string => {
                const candidates = [PREFERRED_MIME, FALLBACK_MIME, 'audio/ogg;codecs=opus', 'audio/mp4']
                for (const candidate of candidates) {
                        if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(candidate)) {
                                return candidate
                        }
                }
                return ''
        }, [])

        const clearChunkTimer = useCallback(() => {
                if (chunkTimerRef.current !== null) {
                        clearInterval(chunkTimerRef.current)
                        chunkTimerRef.current = null
                }
        }, [])

        const teardown = useCallback(() => {
                clearChunkTimer()
                const recorder = mediaRecorderRef.current
                if (recorder && recorder.state !== 'inactive') {
                        try {
                                recorder.stop()
                        } catch {
                                /* ignore */
                        }
                }
                mediaRecorderRef.current = null
                if (streamRef.current) {
                        streamRef.current.getTracks().forEach((t) => t.stop())
                        streamRef.current = null
                }
                streamIdRef.current = null
                sequenceRef.current = 0
        }, [clearChunkTimer])

        const start = useCallback(async (): Promise<boolean> => {
                if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
                        setState((prev) => ({ ...prev, status: 'unsupported', error: 'MediaRecorder API unavailable' }))
                        return false
                }
                if (typeof MediaRecorder === 'undefined') {
                        setState((prev) => ({ ...prev, status: 'unsupported', error: 'MediaRecorder unavailable' }))
                        return false
                }

                let stream: MediaStream
                try {
                        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
                } catch (err) {
                        const denied = err instanceof DOMException && err.name === 'NotAllowedError'
                        setState((prev) => ({
                                ...prev,
                                status: denied ? 'denied' : 'error',
                                error: denied ? 'Microphone permission denied' : String(err),
                        }))
                        return false
                }

                if (!mountedRef.current) {
                        stream.getTracks().forEach((t) => t.stop())
                        return false
                }

                const mimeType = pickMimeType()
                let recorder: MediaRecorder
                try {
                        recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
                } catch (err) {
                        stream.getTracks().forEach((t) => t.stop())
                        setState((prev) => ({ ...prev, status: 'error', error: String(err) }))
                        return false
                }

                streamRef.current = stream
                mediaRecorderRef.current = recorder
                const streamId = uuid()
                streamIdRef.current = streamId
                sequenceRef.current = 0

                recorder.ondataavailable = (event) => {
                        if (event.data && event.data.size > 0 && streamIdRef.current) {
                                const seq = sequenceRef.current++
                                onFrame(streamIdRef.current, seq, event.data)
                        }
                }

                recorder.onerror = () => {
                        setState((prev) => ({ ...prev, status: 'error', error: 'MediaRecorder error' }))
                }

                // Chunked recording: request data every CHUNK_INTERVAL_MS.
                recorder.start(CHUNK_INTERVAL_MS)

                setState({
                        status: 'recording',
                        mimeType: recorder.mimeType || mimeType || null,
                        streamId,
                        sequence: 0,
                        error: null,
                })
                return true
        }, [onFrame, pickMimeType])

        const pause = useCallback(() => {
                const recorder = mediaRecorderRef.current
                if (recorder && recorder.state === 'recording') {
                        recorder.pause()
                        setState((prev) => ({ ...prev, status: 'paused' }))
                }
        }, [])

        const resume = useCallback(() => {
                const recorder = mediaRecorderRef.current
                if (recorder && recorder.state === 'paused') {
                        recorder.resume()
                        setState((prev) => ({ ...prev, status: 'recording' }))
                }
        }, [])

        const stop = useCallback(() => {
                teardown()
                setState((prev) => ({ ...prev, status: 'idle', streamId: null, sequence: 0 }))
        }, [teardown])

        useEffect(() => {
                mountedRef.current = true
                return () => {
                        mountedRef.current = false
                        teardown()
                }
        }, [teardown])

        return { state, start, pause, resume, stop }
}
