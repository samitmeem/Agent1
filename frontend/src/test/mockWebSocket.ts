/** Minimal WebSocket mock for hook tests.
 *
 * Implements the subset of the WebSocket API used by `useTicketSocket`:
 * `readyState`, `send`, `close`, `binaryType`, and the `onopen/onmessage/
 * onclose/onerror` callbacks. `__trigger` lets tests push inbound frames.
 */
export type WsReadyState = number

export class MockWebSocket {
        static instances: MockWebSocket[] = []
        static OPEN = 1
        static CONNECTING = 0
        static CLOSING = 2
        static CLOSED = 3

        static reset(): void {
                MockWebSocket.instances.forEach((ws) => ws.dispose())
                MockWebSocket.instances = []
        }

        static last(): MockWebSocket | undefined {
                return MockWebSocket.instances[MockWebSocket.instances.length - 1]
        }

        url: string
        binaryType: 'arraybuffer' | 'blob' = 'arraybuffer'
        readyState: WsReadyState = MockWebSocket.CONNECTING
        onopen: ((ev: Event) => void) | null = null
        onmessage: ((ev: MessageEvent) => void) | null = null
        onclose: ((ev: CloseEvent) => void) | null = null
        onerror: ((ev: Event) => void) | null = null

        sent: unknown[] = []
        private disposed = false

        constructor(url: string) {
                this.url = url
                MockWebSocket.instances.push(this)
        }

        /** Simulate the server accepting the connection. */
        open(): void {
                this.readyState = MockWebSocket.OPEN
                this.onopen?.(new Event('open'))
        }

        /** Push an inbound JSON envelope or binary frame. */
        __trigger(data: string | ArrayBuffer): void {
                this.onmessage?.({ data } as MessageEvent)
        }

        send(data: unknown): void {
                if (this.disposed) throw new Error('WebSocket is closed')
                this.sent.push(data)
        }

        close(code = 1000, reason = ''): void {
                if (this.readyState === MockWebSocket.CLOSED) return
                this.readyState = MockWebSocket.CLOSED
                this.onclose?.(new CloseEvent('close', { code, reason, wasClean: true }))
        }

        dispose(): void {
                this.disposed = true
        }
}
