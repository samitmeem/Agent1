import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
        plugins: [react()],
        server: {
                port: 5173,
                proxy: {
                        '/api': 'http://localhost:8000',
                        '/ws': {
                                target: 'ws://localhost:8000',
                                ws: true,
                        },
                },
        },
        test: {
                environment: 'jsdom',
                globals: true,
                setupFiles: ['./src/test/setup.ts'],
                include: ['src/**/*.{test,spec}.{ts,tsx}'],
                coverage: {
                        reporter: ['text', 'html'],
                },
        },
})
