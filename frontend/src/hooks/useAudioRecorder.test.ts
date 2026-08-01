import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAudioRecorder } from '../hooks/useAudioRecorder'

class MockMediaRecorder {
        static instances: MockMediaRecorder[] = []
        static supportedMime = 'audio/webm;codecs=opus'

        stream: MediaStream
        mimeType: string
        state: 'inactive' | 'recording' | 'paused' = 'inactive'
        ondataavailable: ((ev: { data: Blob }) => void) | null = null
        onerror: ((ev: Event) => void) | null = null

        constructor(stream: MediaStream, options?: { mimeType?: string }) {
                this.stream = stream
                this.mimeType = options?.mimeType ?? MockMediaRecorder.supportedMime
                MockMediaRecorder.instances.push(this)
        }

        start(interval: number): void {
                this.state = 'recording'
                void interval
        }

        pause(): void {
                this.state = 'paused'
        }

        resume(): void {
                this.state = 'recording'
        }

        stop(): void {
                this.state = 'inactive'
        }

        __emitChunk(data: Blob): void {
                this.ondataavailable?.({ data })
        }

        static isTypeSupported(mime: string): boolean {
                return mime === MockMediaRecorder.supportedMime || mime === 'audio/webm'
        }

        static reset(): void {
                MockMediaRecorder.instances = []
        }
}

function makeStream(): MediaStream {
        return {
                getTracks: () => [{ stop: vi.fn() } as unknown as MediaStreamTrack],
        } as unknown as MediaStream
}

beforeEach(() => {
        vi.stubGlobal('MediaRecorder', MockMediaRecorder)
        MockMediaRecorder.reset()
        vi.stubGlobal('navigator', {
                mediaDevices: {
                        getUserMedia: vi.fn().mockResolvedValue(makeStream()),
                },
        })
})

afterEach(() => {
        vi.unstubAllGlobals()
})

describe('useAudioRecorder', () => {
        it('starts recording and reports the detected codec', async () => {
                const onFrame = vi.fn()
                const { result } = renderHook(() => useAudioRecorder(onFrame))
                let ok = false
                await act(async () => {
                        ok = await result.current.start()
                })
                expect(ok).toBe(true)
                expect(result.current.state.status).toBe('recording')
                expect(result.current.state.mimeType).toBe('audio/webm;codecs=opus')
                expect(result.current.state.streamId).toBeTruthy()
        })

        it('reports denied status when permission is refused', async () => {
                vi.stubGlobal('navigator', {
                        mediaDevices: {
                                getUserMedia: vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError')),
                        },
                })
                const { result } = renderHook(() => useAudioRecorder(vi.fn()))
                let ok = true
                await act(async () => {
                        ok = await result.current.start()
                })
                expect(ok).toBe(false)
                expect(result.current.state.status).toBe('denied')
        })

        it('reports unsupported when MediaRecorder is unavailable', async () => {
                vi.stubGlobal('MediaRecorder', undefined)
                const { result } = renderHook(() => useAudioRecorder(vi.fn()))
                let ok = true
                await act(async () => {
                        ok = await result.current.start()
                })
                expect(ok).toBe(false)
                expect(result.current.state.status).toBe('unsupported')
        })

        it('invokes onFrame for each chunked frame with monotonic sequence', async () => {
                const onFrame = vi.fn()
                const { result } = renderHook(() => useAudioRecorder(onFrame))
                await act(async () => {
                        await result.current.start()
                })
                const recorder = MockMediaRecorder.instances[0]!
                act(() => recorder.__emitChunk(new Blob([new Uint8Array([1, 2, 3])])))
                act(() => recorder.__emitChunk(new Blob([new Uint8Array([4, 5, 6])])))
                expect(onFrame).toHaveBeenCalledTimes(2)
                expect(onFrame.mock.calls[0]![1]).toBe(0)
                expect(onFrame.mock.calls[1]![1]).toBe(1)
        })

        it('pauses and resumes recording', async () => {
                const { result } = renderHook(() => useAudioRecorder(vi.fn()))
                await act(async () => {
                        await result.current.start()
                })
                act(() => result.current.pause())
                expect(result.current.state.status).toBe('paused')
                act(() => result.current.resume())
                expect(result.current.state.status).toBe('recording')
        })

        it('stops cleanly and releases tracks', async () => {
                const { result } = renderHook(() => useAudioRecorder(vi.fn()))
                await act(async () => {
                        await result.current.start()
                })
                const recorder = MockMediaRecorder.instances[0]!
                act(() => result.current.stop())
                expect(recorder.state).toBe('inactive')
                expect(result.current.state.status).toBe('idle')
                expect(result.current.state.streamId).toBeNull()
        })

        it('tears down on unmount', async () => {
                const { result, unmount } = renderHook(() => useAudioRecorder(vi.fn()))
                await act(async () => {
                        await result.current.start()
                })
                const recorder = MockMediaRecorder.instances[0]!
                unmount()
                expect(recorder.state).toBe('inactive')
        })
})
