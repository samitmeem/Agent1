import { useEffect, useState } from 'react'
import { createTicket, listTickets } from '../api'
import type { Ticket } from '../types'
import { StatusBadge } from './StatusBadge'

interface TicketListProps {
        selectedId: string | null
        onSelect: (ticket: Ticket) => void
}

export function TicketList({ selectedId, onSelect }: TicketListProps) {
        const [tickets, setTickets] = useState<Ticket[]>([])
        const [loading, setLoading] = useState(true)
        const [error, setError] = useState<string | null>(null)
        const [subject, setSubject] = useState('')

        const refresh = async () => {
                setLoading(true)
                setError(null)
                try {
                        setTickets(await listTickets())
                } catch (err) {
                        setError(err instanceof Error ? err.message : 'Failed to load tickets')
                } finally {
                        setLoading(false)
                }
        }

        useEffect(() => {
                void refresh()
        }, [])

        const handleCreate = async (event: React.FormEvent) => {
                event.preventDefault()
                if (!subject.trim()) return
                try {
                        const ticket = await createTicket(subject.trim())
                        setSubject('')
                        setTickets((prev) => [ticket, ...prev])
                        onSelect(ticket)
                } catch (err) {
                        setError(err instanceof Error ? err.message : 'Failed to create ticket')
                }
        }

        return (
                <aside className="ticket-list">
                        <form onSubmit={handleCreate} className="ticket-list__create">
                                <input
                                        type="text"
                                        placeholder="New ticket subject…"
                                        value={subject}
                                        onChange={(e) => setSubject(e.target.value)}
                                        aria-label="New ticket subject"
                                />
                                <button type="submit" disabled={!subject.trim()}>New</button>
                        </form>
                        {loading && <p className="muted">Loading tickets…</p>}
                        {error && <p className="error">{error}</p>}
                        <ul className="ticket-list__items">
                                {tickets.map((ticket) => (
                                        <li
                                                key={ticket.id}
                                                className={ticket.id === selectedId ? 'ticket-list__item ticket-list__item--active' : 'ticket-list__item'}
                                        >
                                                <button type="button" onClick={() => onSelect(ticket)} className="ticket-list__item-btn">
                                                        <span className="ticket-list__subject">{ticket.subject || ticket.id}</span>
                                                        <StatusBadge status={ticket.status} />
                                                </button>
                                        </li>
                                ))}
                        </ul>
                </aside>
        )
}
