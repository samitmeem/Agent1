import type { AgentStatus, TicketStatus } from '../types'

const STATUS_LABELS: Record<TicketStatus, string> = {
        open: 'Open',
        queued: 'Queued',
        running: 'Running',
        waiting_for_user: 'Waiting for you',
        resolved: 'Resolved',
        failed: 'Failed',
}

const AGENT_LABELS: Record<AgentStatus, string> = {
        idle: 'Idle',
        planning: 'Planning',
        generating: 'Generating',
        calling_tool: 'Calling tool',
        awaiting_authorization: 'Awaiting authorization',
        synthesizing_voice: 'Synthesizing voice',
        completed: 'Completed',
}

const STATUS_TONES: Record<TicketStatus, string> = {
        open: 'badge--neutral',
        queued: 'badge--info',
        running: 'badge--info',
        waiting_for_user: 'badge--warn',
        resolved: 'badge--ok',
        failed: 'badge--err',
}

export function StatusBadge({ status }: { status: TicketStatus }) {
        return <span className={`badge ${STATUS_TONES[status] ?? 'badge--neutral'}`}>{STATUS_LABELS[status] ?? status}</span>
}

export function AgentStatusBadge({ status }: { status: AgentStatus | string | null }) {
        if (!status) return null
        const label = AGENT_LABELS[status as AgentStatus] ?? status
        return <span className="badge badge--ghost">{label}</span>
}
