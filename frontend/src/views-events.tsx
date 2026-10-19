import { useEffect, useState } from 'react'
import type { EventItem } from './app-shell'

export function EventList({ call, authed, onOpen, refreshKey }: { call: (p: string, i?: RequestInit) => Promise<unknown>; authed: boolean; onOpen: (e: EventItem) => void; refreshKey: number }) {
  const [items, setItems] = useState<EventItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('')
  const [notice, setNotice] = useState('')

  async function load() {
    setLoading(true); setError('')
    try {
      const q = new URLSearchParams()
      if (search) q.set('search', search)
      if (category) q.set('category', category)
      const body = await call(`/events${q.toString() ? `?${q}` : ''}`) as { data: EventItem[] }
      setItems(body.data || [])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load events')
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [refreshKey])
  const cats = Array.from(new Set(items.map((i) => i.category).filter(Boolean)))

  async function register(id: string) {
    setNotice('')
    try {
      await call(`/events/${id}/register`, { method: 'POST' })
      setNotice('Registered successfully')
      load()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'Registration failed')
    }
  }

  return (
    <section>
      <div className="hero">
        <h1>Campus events, one place</h1>
        <p>Discover talks, fairs, workshops and festivals. Register in seconds.</p>
        <div className="filters">
          <input aria-label="Search events" placeholder="Search title or description" value={search} onChange={(e) => setSearch(e.target.value)} />
          <select aria-label="Filter by category" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {cats.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <button className="primary" onClick={load}>Search</button>
        </div>
      </div>
      {notice && <div className="notice">{notice}</div>}
      {loading && <p role="status">Loading events…</p>}
      {error && <p role="alert" className="error">{error}</p>}
      {!loading && !error && items.length === 0 && <p className="empty">No events found. Try clearing filters.</p>}
      <div className="grid">
        {items.map((e) => (
          <article key={e.id} className="card">
            <div className="chip">{e.category}</div>
            <h2>{e.title}</h2>
            <p className="meta">{e.date} · {e.time} · {e.location}</p>
            <p className="desc">{e.description.slice(0, 140)}{e.description.length > 140 ? '…' : ''}</p>
            <p className="seats">{e.availableSeats <= 0 ? 'Full' : `${e.availableSeats} of ${e.capacity} seats left`} · by {e.organizerName}</p>
            <div className="row">
              <button onClick={() => onOpen(e)}>Details</button>
              {authed && <button className="primary" disabled={e.availableSeats <= 0} onClick={() => register(e.id)}>{e.availableSeats <= 0 ? 'Full' : 'Register'}</button>}
            </div>
          </article>
        ))}
      </div>
    </section>
  )
}

export function EventDetail({ ev, call, authed, onBack }: { ev: EventItem; call: (p: string, i?: RequestInit) => Promise<unknown>; authed: boolean; onBack: () => void }) {
  const [notice, setNotice] = useState('')
  async function register() {
    try { await call(`/events/${ev.id}/register`, { method: 'POST' }); setNotice('Registered successfully') }
    catch (e) { setNotice(e instanceof Error ? e.message : 'Registration failed') }
  }
  return (
    <section className="detail">
      <button onClick={onBack}>← Back</button>
      <h1>{ev.title}</h1>
      <p className="meta">{ev.date} · {ev.time} · {ev.location} · {ev.category}</p>
      <p>{ev.description}</p>
      <p className="seats">{ev.availableSeats} of {ev.capacity} seats available · Organized by {ev.organizerName}</p>
      <p className="meta">Registration deadline: {ev.deadline}</p>
      {notice && <div className="notice">{notice}</div>}
      {authed && <button className="primary" disabled={ev.availableSeats <= 0} onClick={register}>{ev.availableSeats <= 0 ? 'Event full' : 'Register for this event'}</button>}
      {!authed && <p>Sign in to register.</p>}
    </section>
  )
}
