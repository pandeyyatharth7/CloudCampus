/* Vanilla Cognito Hosted UI + PKCE. No Amplify dependency. */

export interface StoredTokens {
  access_token: string
  id_token: string
  refresh_token?: string
  expires_at: number
}

const KEY = 'cloudcampus.tokens'
const VERIFIER_KEY = 'cloudcampus.pkce'

function cfg() {
  return {
    region: import.meta.env.VITE_COGNITO_REGION as string,
    poolId: import.meta.env.VITE_COGNITO_USER_POOL_ID as string,
    clientId: import.meta.env.VITE_COGNITO_CLIENT_ID as string,
  }
}

function domain(): string {
  return (import.meta.env.VITE_COGNITO_DOMAIN as string | undefined) || ''
}

export function getStoredTokens(): StoredTokens | null {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return null
    const t = JSON.parse(raw) as StoredTokens
    if (t.expires_at && Date.now() > t.expires_at) return null
    return t
  } catch {
    return null
  }
}

export function clearTokens() {
  localStorage.removeItem(KEY)
  localStorage.removeItem(VERIFIER_KEY)
}

function b64url(buf: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function pkcePair(): Promise<{ verifier: string; challenge: string }> {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  const verifier = b64url(bytes.buffer as ArrayBuffer)
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return { verifier, challenge: b64url(digest) }
}

function redirectUri(): string {
  // Must match a registered Cognito CallbackURL exactly. The CloudFront SPA
  // always serves the app at "/", so never append the current pathname.
  return window.location.origin + '/'
}

export async function startLogin() {
  const { clientId } = cfg()
  const dom = domain()
  if (!dom) { alert('Auth not configured yet (VITE_COGNITO_DOMAIN missing).'); return }
  const { verifier, challenge } = await pkcePair()
  localStorage.setItem(VERIFIER_KEY, verifier)
  const url = `https://${dom}/login?client_id=${clientId}&response_type=code&scope=openid+email+profile&redirect_uri=${encodeURIComponent(redirectUri())}&code_challenge_method=S256&code_challenge=${challenge}`
  window.location.href = url
}

export function logoutUrl(): string {
  const { clientId } = cfg()
  const dom = domain()
  if (!dom) return ''
  return `https://${dom}/logout?client_id=${clientId}&logout_uri=${encodeURIComponent(redirectUri())}`
}

export async function startSignup() {
  const { clientId } = cfg()
  const dom = domain()
  if (!dom) { alert('Auth not configured yet (VITE_COGNITO_DOMAIN missing).'); return }
  const { verifier, challenge } = await pkcePair()
  localStorage.setItem(VERIFIER_KEY, verifier)
  const url = `https://${dom}/signup?client_id=${clientId}&response_type=code&scope=openid+email+profile&redirect_uri=${encodeURIComponent(redirectUri())}&code_challenge_method=S256&code_challenge=${challenge}`
  window.location.href = url
}

export async function exchangeCodeForTokens(code: string): Promise<StoredTokens> {
  const { clientId } = cfg()
  const verifier = localStorage.getItem(VERIFIER_KEY) || ''
  const tokens = await tokenRequest({
    grant_type: 'authorization_code',
    client_id: clientId,
    code,
    redirect_uri: redirectUri(),
    code_verifier: verifier,
  })
  localStorage.removeItem(VERIFIER_KEY)
  localStorage.setItem(KEY, JSON.stringify(tokens))
  return tokens
}

/**
 * Access tokens live one hour. Without this the SPA silently drops the user
 * back to the signed-out state, so refresh before it is actually expired.
 */
export async function refreshSession(current: StoredTokens): Promise<StoredTokens | null> {
  const { clientId } = cfg()
  const dom = domain()
  if (!dom || !current.refresh_token) return null
  try {
    const tokens = await tokenRequest({
      grant_type: 'refresh_token',
      client_id: clientId,
      refresh_token: current.refresh_token,
    })
    // Cognito only re-issues a refresh token if the scope requests it.
    tokens.refresh_token = current.refresh_token
    localStorage.setItem(KEY, JSON.stringify(tokens))
    return tokens
  } catch {
    clearTokens()
    return null
  }
}

async function tokenRequest(params: Record<string, string>): Promise<StoredTokens> {
  const dom = domain()
  const res = await fetch(`https://${dom}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(params),
  })
  if (!res.ok) throw new Error('Token request failed')
  const j = await res.json()
  return {
    access_token: j.access_token,
    id_token: j.id_token,
    refresh_token: j.refresh_token,
    expires_at: Date.now() + (j.expires_in || 3600) * 1000,
  }
}

export function decodeJwt(token: string): Record<string, unknown> {
  try {
    const part = token.split('.')[1]
    return JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/')))
  } catch {
    return {}
  }
}

export function getUserInfo(tokens: StoredTokens | null): { email: string; groups: string[] } {
  if (!tokens) return { email: '', groups: [] }
  const claims = decodeJwt(tokens.id_token || tokens.access_token)
  const groups = (claims['cognito:groups'] as string[] | string | undefined) || []
  return {
    email: (claims.email as string) || (claims['cognito:username'] as string) || '',
    groups: Array.isArray(groups) ? groups : String(groups).split(',').filter(Boolean),
  }
}
