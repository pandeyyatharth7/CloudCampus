import { useCallback, useEffect, useRef, useState } from 'react'
import './auth.css'
import {
  exchangeCodeForTokens,
  getStoredTokens,
  refreshSession,
  startLogin,
  startSignup,
  type StoredTokens,
} from './auth'

export type View = 'events' | 'mine' | 'organizer' | 'detail'

export interface EventItem {
  id: string
  title: string
  description: string
  category: string
  date: string
  time: string
  location: string
  capacity: number
  deadline: string
  organizerName: string
  registrations: number
  status: string
  availableSeats: number
}

const API = import.meta.env.VITE_API_URL as string | undefined
const REFRESH_SKEW_MS = 60_000

export function useApi() {
  const [tokens, setTokens] = useState<StoredTokens | null>(getStoredTokens)
  // `call` closes over `tokens`; keep a ref so the refresher always sees the
  // newest value without making `call` change identity on every render.
  const tokensRef = useRef<StoredTokens | null>(tokens)
  tokensRef.current = tokens

  const signOut = useCallback(() => {
    localStorage.removeItem('cloudcampus.tokens')
    localStorage.removeItem('cloudcampus.pkce')
    setTokens(null)
  }, [])

  async function liveTokens(): Promise<StoredTokens | null> {
    const current = tokensRef.current
    if (!current) return null
    if (!current.expires_at || current.expires_at - Date.now() > REFRESH_SKEW_MS) return current
    const fresh = await refreshSession(current)
    if (fresh) setTokens(fresh)
    else signOut()
    return fresh
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const code = params.get('code')
    if (code) {
      exchangeCodeForTokens(code).then((t) => {
        setTokens(t)
        window.history.replaceState({}, '', window.location.pathname)
      }).catch(() => undefined)
    }
  }, [])

  // Keep the session alive while the tab is open.
  useEffect(() => {
    if (!tokens?.refresh_token) return
    const id = window.setInterval(() => { void liveTokens() }, 5 * 60_000)
    return () => window.clearInterval(id)
  }, [tokens?.refresh_token])

  async function call(path: string, init?: RequestInit, retried = false): Promise<any> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const live = await liveTokens()
    if (live?.access_token) headers.Authorization = `Bearer ${live.access_token}`
    const res = await fetch(`${API}${path}`, { ...init, headers: { ...headers, ...(init?.headers as object) } })
    // An expired token can still slip through between refreshes: try once more.
    if (res.status === 401 && !retried && live?.refresh_token) {
      const fresh = await refreshSession(live)
      if (fresh) {
        setTokens(fresh)
        return call(path, init, true)
      }
    }
    const body = await res.json().catch(() => ({}))
    if (!res.ok) throw Object.assign(new Error(body.message || body.error || `HTTP ${res.status}`), { body, status: res.status })
    return body
  }
  return { tokens, setTokens, signOut, call }
}

export function Header({ view, setView, tokens, onLogout, userEmail }: { view: View; setView: (v: View) => void; tokens: object | null; onLogout: () => void; userEmail: string }) {
  return (
    <header className="topbar">
      <div className="brand" onClick={() => setView('events')}>CloudCampus</div>
      <nav>
        <button className={view === 'events' ? 'active' : ''} onClick={() => setView('events')}>Discover</button>
        {tokens && <button className={view === 'mine' ? 'active' : ''} onClick={() => setView('mine')}>My Registrations</button>}
        {tokens && <button className={view === 'organizer' ? 'active' : ''} onClick={() => setView('organizer')}>Organize</button>}
      </nav>
      <div className="authbox">
        {tokens ? (<><span className="email">{userEmail}</span><button onClick={onLogout}>Sign out</button></>) : (<><button onClick={() => { void startLogin() }}>Sign in</button><button className="primary" onClick={() => { void startSignup() }}>Sign up</button></>)}
      </div>
    </header>
  )
}
