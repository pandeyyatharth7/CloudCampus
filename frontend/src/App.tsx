import { useState } from 'react'
import './app.css'
import { Header, useApi, type EventItem, type View } from './app-shell'
import { EventDetail, EventList } from './views-events'
import { MyRegistrations, Organizer } from './views-manage'
import { getUserInfo, logoutUrl } from './auth'

function App() {
  const { tokens, signOut, call } = useApi()
  const [view, setView] = useState<View>('events')
  const [selected, setSelected] = useState<EventItem | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const info = getUserInfo(tokens)

  function logout() {
    signOut()
    setView('events')
    setSelected(null)
    // Also end the Cognito hosted-UI session, not just the local token copy.
    const url = logoutUrl()
    if (url) window.location.href = url
  }

  return (
    <>
      <Header view={view} setView={(v) => { setSelected(null); setView(v) }} tokens={tokens} onLogout={logout} userEmail={info.email} />
      <main className="wrap">
        {view === 'events' && !selected && (
          <EventList call={call} authed={!!tokens} refreshKey={refreshKey} onOpen={(e) => setSelected(e)} />
        )}
        {view === 'events' && selected && (
          <EventDetail ev={selected} call={call} authed={!!tokens} onBack={() => { setSelected(null); setRefreshKey((k) => k + 1) }} />
        )}
        {view === 'mine' && tokens && <MyRegistrations call={call} />}
        {view === 'organizer' && tokens && <Organizer call={call} onChanged={() => setRefreshKey((k) => k + 1)} />}
        {view === 'organizer' && tokens && !info.groups.includes('organizers') && (
          <p className="warn">Your account is not in the organizers group yet. Ask the project owner to add you, then sign out and back in.</p>
        )}
      </main>
      <footer className="foot">CloudCampus · serverless on AWS · ap-south-1</footer>
    </>
  )
}

export default App
