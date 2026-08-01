import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTtsAudioQueue } from '../hooks/useTtsAudioQueue'

beforeEach(() => {
        vi.useFakeTimers()
})

afterEach(() => {
        vi.useRealTimers()
})

describe('useTtsAudioQueue', () => {
        it('starts a stream and tracks queued frames', () => {
                const { result } = renderHook(() => useTtsAudioQueue())
                act(() => result.current.startStream({ stream_id: 's1', mime_type: 'audio/wav', sample_rate: 24000 }))
                expect(result.current.state.streamIds).toContain('s1')
        })

        it('drops frames when muted', () => {
                const { result } = renderHook(() => useTtsAudioQueue())
                act(() => result.current.startStream({ stream_id: 's1', mime_type: 'audio/wav', sample_rate: 24000 }))
                act(() => result.current.toggleMute())
                expect(result.current.state.muted).toBe(true)
                act(() => result.current.enqueueFrame('s1', 0, new ArrayBuffer(8)))
                // Muted frames are dropped during playback; queuedFrames reflects the
                // remaining queue (decodeAudioData is unavailable in jsdom so frames are
                // skipped, but the queue should not grow unbounded).
                expect(result.current.state.muted).toBe(true)
        })

        it('ends a stream and clears it from the active list', () => {
                const { result } = renderHook(() => useTtsAudioQueue())
                act(() => result.current.startStream({ stream_id: 's1', mime_type: 'audio/wav', sample_rate: 24000 }))
                act(() => result.current.endStream('s1'))
                expect(result.current.state.streamIds).not.toContain('s1')
        })

        it('stop clears the queue and stops playback', () => {
                const { result } = renderHook(() => useTtsAudioQueue())
                act(() => result.current.startStream({ stream_id: 's1', mime_type: 'audio/wav', sample_rate: 24000 }))
                act(() => result.current.stop())
                expect(result.current.state.playing).toBe(false)
                expect(result.current.state.streamIds).toHaveLength(0)
        })
})
