/**
 * Cognito authentication using Authorization Code flow with PKCE.
 * Secure for SPAs — no client secret needed, tokens not exposed in URL.
 */

const COGNITO_DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN;
const CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID;
const REDIRECT_URI = import.meta.env.VITE_REDIRECT_URI || (typeof window !== 'undefined' ? window.location.origin : '');

/**
 * Only an https Cognito Hosted UI origin (https://<prefix>.auth.<region>.amazoncognito.com)
 * is accepted, so a mis-set env var can't send auth codes / PKCE verifiers elsewhere.
 */
const AUTH_CODE_MAX_LEN = 256;

const isLower = (ch: string) => ch >= 'a' && ch <= 'z';
const isUpper = (ch: string) => ch >= 'A' && ch <= 'Z';
const isDigit = (ch: string) => ch >= '0' && ch <= '9';
const isLabelChar = (ch: string) => isLower(ch) || isDigit(ch) || ch === '-';
const isCodeChar = (ch: string) => isLabelChar(ch) || isUpper(ch);

function onlyChars(value: string, allowed: (ch: string) => boolean): boolean {
  if (!value) return false;
  for (const ch of value) {
    if (ch.length !== 1 || !allowed(ch)) return false;
  }
  return true;
}

// Linear label check (no regex): <prefix>.auth.<region>.amazoncognito.com
function isCognitoHostname(hostname: string): boolean {
  const labels = hostname.split('.');
  return (
    labels.length === 5 &&
    labels[1] === 'auth' &&
    labels[3] === 'amazoncognito' &&
    labels[4] === 'com' &&
    onlyChars(labels[0], isLabelChar) &&
    onlyChars(labels[2], isLabelChar)
  );
}

export function cognitoOrigin(domain: string | undefined): string | null {
  if (!domain) return null;
  try {
    const url = new URL(domain);
    if (url.protocol !== 'https:' || url.username || url.password || url.port || !isCognitoHostname(url.hostname)) {
      return null;
    }
    return url.origin;
  } catch {
    return null;
  }
}

// Resolved once from build-time config — request data never reaches the fetch URL.
const COGNITO_ORIGIN = cognitoOrigin(COGNITO_DOMAIN);
const TOKEN_ENDPOINT: string | null = COGNITO_ORIGIN ? `${COGNITO_ORIGIN}/oauth2/token` : null;

/**
 * Cognito authorization codes are opaque UUID-style tokens. Returns a copy rebuilt from the
 * allowed character codes (no regex, linear), or null if `code` isn't one.
 */
export function toAuthCode(code: string | null): string | null {
  if (!code || code.length > AUTH_CODE_MAX_LEN) return null;
  let out = '';
  for (let i = 0; i < code.length; i++) {
    const c = code.charCodeAt(i);
    if (!isCodeChar(code.charAt(i))) return null;
    out += String.fromCharCode(c);
  }
  return out;
}

export function isAuthCode(code: string): boolean {
  return toAuthCode(code) !== null;
}

function cognitoUrl(path: string): string {
  const origin = COGNITO_ORIGIN;
  if (!origin) throw new Error('VITE_COGNITO_DOMAIN is not a valid Cognito Hosted UI URL');
  return new URL(path, origin).toString();
}

// ── PKCE Helpers ──────────────────────────────────────────────────────

function generateRandomString(length: number): string {
  const array = new Uint8Array(length);
  crypto.getRandomValues(array);
  return Array.from(array, (b) => b.toString(16).padStart(2, '0')).join('').slice(0, length);
}

async function sha256(plain: string): Promise<ArrayBuffer> {
  const encoder = new TextEncoder();
  return crypto.subtle.digest('SHA-256', encoder.encode(plain));
}

function base64UrlEncode(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function generateCodeChallenge(verifier: string): Promise<string> {
  const hashed = await sha256(verifier);
  return base64UrlEncode(hashed);
}

// ── Auth Functions ────────────────────────────────────────────────────

export const login = async () => {
  // Generate PKCE code verifier and challenge
  const codeVerifier = generateRandomString(64);
  const codeChallenge = await generateCodeChallenge(codeVerifier);

  // Store verifier for token exchange
  sessionStorage.setItem('pkce_code_verifier', codeVerifier);

  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: 'code',
    scope: 'openid email profile',
    redirect_uri: REDIRECT_URI,
    code_challenge: codeChallenge,
    code_challenge_method: 'S256',
  });

  window.location.href = `${cognitoUrl('/login')}?${params.toString()}`;
};

export const logout = () => {
  sessionStorage.removeItem('access_token');
  sessionStorage.removeItem('id_token');
  sessionStorage.removeItem('pkce_code_verifier');
  window.location.href = `${cognitoUrl('/logout')}?client_id=${CLIENT_ID}&logout_uri=${encodeURIComponent(REDIRECT_URI)}`;
};

/**
 * POST the token exchange. The request target is the module-level TOKEN_ENDPOINT (validated
 * Cognito origin + constant path); the auth code and verifier are form-body fields only and
 * never take part in building the URL.
 */
function requestTokens(code: string, codeVerifier: string): Promise<Response> {
  const body = new URLSearchParams();
  body.set('grant_type', 'authorization_code');
  body.set('client_id', CLIENT_ID);
  body.set('code', code);
  body.set('redirect_uri', REDIRECT_URI);
  body.set('code_verifier', codeVerifier);

  const request = new Request(TOKEN_ENDPOINT as string, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  return fetch(request);
}

export const handleAuthCallback = async (): Promise<boolean> => {
  const params = new URLSearchParams(window.location.search);
  // The code only goes into the form body, never the URL; refuse anything that isn't an
  // opaque Cognito authorization code.
  const code = toAuthCode(params.get('code'));
  if (!code) return false;

  const codeVerifier = sessionStorage.getItem('pkce_code_verifier');
  if (!codeVerifier) return false;

  // Fixed endpoint: the validated Cognito Hosted UI origin + a constant path.
  if (!TOKEN_ENDPOINT) return false;

  try {
    const response = await requestTokens(code, codeVerifier);

    if (!response.ok) return false;

    const data = await response.json();
    sessionStorage.setItem('access_token', data.access_token);
    if (data.id_token) sessionStorage.setItem('id_token', data.id_token);
    sessionStorage.removeItem('pkce_code_verifier');

    // Clean URL
    window.history.replaceState(null, '', window.location.pathname);
    return true;
  } catch {
    return false;
  }
};

export const getAccessToken = (): string | null => sessionStorage.getItem('access_token');
export const isAuthenticated = (): boolean => !!getAccessToken();
