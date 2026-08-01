import { useEffect, useState } from 'react'
import { getCurrentUser } from '../api'
import type { Ticket, User } from '../types'
import { ConversationView } from './ConversationView'
import { TicketList } from './TicketList'

export function Dashboard() {
        const [user, setUser] = useState<User | null>(null)
        const [selected, setSelected] = useState<Ticket | null>(null)

        useEffect(() => {
                getCurrentUser()
                        .then(setUser)
                        .catch(() => setUser(null))
        }, [])

        return (
                <div className="dashboard">
                        <header className="dashboard__header">
                                <h1>Ticket Assistant</h1>
                                {user && <span className="muted">{user.display_name || user.email}</span>}
                        </header>
                        <div className="dashboard__body">
                                <TicketList selectedId={selected?.id ?? null} onSelect={setSelected} />
                                <main className="dashboard__main">
                                        {selected ? (
                                                <ConversationView key={selected.id} ticket={selected} />
                                        ) : (
                                                <p className="muted">Select a ticket or create a new one to begin.</p>
                                        )}
                                </main>
                        </div>
                </div>
        )
}
