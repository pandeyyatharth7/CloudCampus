import { useEffect, useState } from 'react'
import type { EventItem } from './app-shell'

export function MyRegistrations({ call }: { call: (p: string, i?: RequestInit) => Promise<unknown> }) {
  const [rows, setRows] = useState<{ registration: { eventId: string; status: string; registeredAt: string }; event: EventItem | null }[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function load() {
    setLoading(true); setError('')
    try {
      const body = await call('/me/registrations') as { data: typeof rows }
      setRows(body.data || [])
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to load') } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  async function cancel(id: string) {
    try { await call(`/events/${id}/unregister`, { method: 'POST' }); setNotice('Registration cancelled'); load() }
    catch (e) { setNotice(e instanceof Error ? e.message : 'Cancel failed') }
  }

  if (loading) return <p role="status">Loading your registrations…</p>
  if (error) return <p role="alert" className="error">{error}</p>
  if (rows.length === 0) return <p className="empty">You have no registrations yet. Discover events to get started.</p>
  return (
    <section>
      <h1>My Registrations</h1>
      {notice && <div className="notice">{notice}</div>}
      <div className="grid">
        {rows.map((r) => (
          <article key={r.registration.eventId} className="card">
            <h2>{r.event?.title || r.registration.eventId}</h2>
            <p className="meta">{r.event?.date} · {r.event?.location} · {r.registration.status}</p>
            <p className="meta">Registered: {r.registration.registeredAt}</p>
            <button onClick={() => cancel(r.registration.eventId)}>Cancel registration</button>
          </article>
        ))}
      </div>
    </section>
  )
}


export function Organizer({ call, onChanged }: { call: (p: string, i?: RequestInit) => Promise<unknown>; onChanged: () => void }) {
  const [mine, setMine] = useState<EventItem[]>([])
  const [form, setForm] = useState({ title: '', description: '', category: 'Tech', date: '', time: '', location: '', capacity: 50, deadline: '' })
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [seeding, setSeeding] = useState(false)
  const [attendees, setAttendees] = useState<{ event: EventItem; count: number; attendees: { userId: string; status: string; registeredAt: string }[] } | null>(null)

  async function loadMine() {
    try {
      // Owner-scoped: only events this organizer owns, including draft and
      // cancelled ones, which /events (public) deliberately hides.
      const body = await call('/me/events') as { data: EventItem[] }
      setMine(body.data || [])
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to load your events') }
  }
  useEffect(() => { void loadMine() }, [refreshKey])

  async function create() {
    setError(''); setNotice('')
    if (form.title.trim().length < 3) { setError('Title must be at least 3 characters'); return }
    if (form.description.trim().length < 10) { setError('Description must be at least 10 characters'); return }
    try {
      await call('/events', { method: 'POST', body: JSON.stringify(form) })
      setNotice('Event created')
      setForm({ title: '', description: '', category: 'Tech', date: '', time: '', location: '', capacity: 50, deadline: '' })
      onChanged(); reload()
    } catch (e) { setError(e instanceof Error ? e.message : 'Create failed') }
  }

  async function cancel(id: string) {
    setError(''); setNotice('')
    try {
      await call(`/events/${id}/cancel`, { method: 'POST' })
      setNotice('Event cancelled')
      onChanged(); reload()
    } catch (e) { setError(e instanceof Error ? e.message : 'Cancel failed') }
  }

  async function showAttendees(id: string) {
    try {
      const body = await call(`/events/${id}/attendees`) as { data: typeof attendees }
      setAttendees(body.data)
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to load attendees') }
  }

  async function seed() {
    setError(''); setNotice(''); setSeeding(true)
    try {
      const body = await call('/seed', { method: 'POST', body: '{}' }) as { message?: string }
      setNotice(body.message || 'Sample data loaded')
      onChanged(); reload()
    } catch (e) { setError(e instanceof Error ? e.message : 'Seed failed') }
    finally { setSeeding(false) }
  }

  function reload() { setRefreshKey((k) => k + 1) }

  return (
    <section>
      <h1>Organizer dashboard</h1>
      {notice && <div className="notice">{notice}</div>}
      {error && <p role="alert" className="error">{error}</p>}
      <div className="panel">
        <h2>Create event</h2>
        <div className="form">
          <label>Title<input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
          <label>Description<textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
          <label>Category<input value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} /></label>
          <label>Date (YYYY-MM-DD)<input value={form.date} onChange={(e) => setForm({ ...form, date: e.target.value })} placeholder="2026-12-01" /></label>
          <label>Time<input value={form.time} onChange={(e) => setForm({ ...form, time: e.target.value })} placeholder="18:00" /></label>
          <label>Location<input value={form.location} onChange={(e) => setForm({ ...form, location: e.target.value })} /></label>
          <label>Capacity<input type="number" value={form.capacity} onChange={(e) => setForm({ ...form, capacity: Number(e.target.value) })} /></label>
          <label>Deadline (ISO)<input value={form.deadline} onChange={(e) => setForm({ ...form, deadline: e.target.value })} placeholder="2026-11-30T18:00:00Z" /></label>
          <button className="primary" onClick={create}>Create event</button>
        </div>
      </div>
      <h2>My events ({mine.length})</h2>
      {!mine.length && (
        <p className="empty">
          You have not created any events yet.{' '}
          <button onClick={seed} disabled={seeding}>Load sample data</button>
        </p>
      )}
      <div className="grid">
        {mine.map((e) => (
          <article key={e.id} className="card">
            <h2>{e.title}</h2>
            <p className="meta">{e.date} · {e.registrations}/{e.capacity} registered · {e.status}</p>
            <div className="row">
              <button onClick={() => showAttendees(e.id)}>Attendees</button>
              {e.status !== 'cancelled' && <button onClick={() => cancel(e.id)}>Cancel event</button>}
            </div>
          </article>
        ))}
      </div>
      {attendees && (
        <div className="panel">
          <h2>Attendees for {attendees.event.title} ({attendees.count})</h2>
          <ul>{attendees.attendees.map((a) => <li key={a.userId}>{a.userId} · {a.status} · {a.registeredAt}</li>)}</ul>
        </div>
      )}
    </section>
  )
}
