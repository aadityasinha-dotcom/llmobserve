# Google sign-in

The dashboard signs people in with Google. Each user sees only the projects they
belong to; a new account gets its own personal project on first sign-in.

```
browser ─▶ dashboard /auth/google/start ─▶ Google consent ─▶ dashboard /auth/google/callback
                                                                   │  code + PKCE verifier + nonce
                                                                   ▼
                                                 API POST /v1/auth/google
                                                   exchanges code with Google (client secret)
                                                   verifies the ID token (signature, aud, iss, nonce)
                                                   creates user + personal project on first sign-in
                                                   returns a session token
                                                                   │
dashboard stores it in an httpOnly cookie ◀────────────────────────┘
and forwards it as `Authorization: Bearer` on every API call
```

Google's client **secret** and the session-signing secret live only in the API.
The dashboard holds neither, and no longer holds an API key either.

---

## 1. Create the OAuth client (Google Cloud Console)

1. **APIs & Services → OAuth consent screen.** User type **External**. App
   name, support email, developer email. Scopes: `openid`, `email`, `profile`
   (the defaults). While the app is in **Testing**, only the test users you list
   can sign in - add yourself. Publish it when others should be able to sign up.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
   Application type **Web application**.
3. **Authorized redirect URIs** - add every dashboard origin, exactly:
   ```
   http://localhost:3000/auth/google/callback
   https://web-six-blue-66.vercel.app/auth/google/callback
   ```
   No trailing slash. Scheme, host and path must match character for character.
   **Authorized JavaScript origins** can stay empty; the browser never calls
   Google's APIs directly.
4. Copy the **Client ID** and **Client secret**.

---

## 2. Configure the API

| Variable | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | the client ID |
| `GOOGLE_CLIENT_SECRET` | the client secret - **API only** |
| `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` - **API only** |
| `AUTH_REDIRECT_URIS` | the same URIs as step 1.3, comma-separated |
| `SIGNUP_ALLOWED_DOMAINS` | optional, e.g. `yourcompany.com` |
| `SIGNUP_ALLOWED_EMAILS` | optional, e.g. `you@gmail.com,friend@gmail.com` |

Without the first three, `/v1/auth/google` answers 503 and ingest keeps working.

**Decide who can sign up.** With both allowlists empty, *anyone* with a verified
Google account can create an account and a project on your database. That is
fine behind the consent screen's Testing mode, where Google itself limits
sign-in to listed test users - but once the consent screen is published, set an
allowlist unless you mean to offer open sign-up. The allowlist gates account
creation only: someone admitted once keeps access if the list later narrows.

`SESSION_SECRET` rotation signs everyone out. That is also the emergency lever.

---

## 3. Configure the dashboard

| Variable | Value |
|---|---|
| `API_BASE_URL` | the API's URL |
| `GOOGLE_CLIENT_ID` | the same client ID (public; builds the sign-in redirect) |
| `APP_URL` | optional - only if the dashboard sits behind a proxy |

Remove `LLMOBSERVE_API_KEY`. It is no longer read.

On Vercel: **turn Deployment Protection off** for the dashboard project, or set
it to Standard Protection. Vercel Authentication in front of the dashboard would
demand a Vercel login before anyone reached the Google sign-in page. The
dashboard protects itself now.

---

## 4. Claim your existing data

Projects created before sign-in existed (like `local-dev`) have no members, so
nobody sees them. Sign in once, then grant yourself access:

```bash
make key NAME=local-dev ARGS="--grant you@gmail.com"
```

It appears in the project switcher on the next page load.

---

## What a session can and cannot do

| | Session (dashboard) | API key |
|---|---|---|
| Read traces | own projects only | its project, with `read` scope |
| Write traces | **never** | its project, with `ingest` scope |
| Create / revoke keys | own projects only | **never** - a leaked key cannot mint more |
| Lifetime | 7 days (`SESSION_TTL_HOURS`) | until revoked |

Signing out invalidates **every** session the user holds, on every device: the
tokens are stateless, so revocation works by moving the user's
`session_version` on, which every older token then fails to match.

## Where the rules are enforced

- **Which rows a request can read**: Postgres row-level security, keyed on the
  selected project - unchanged.
- **Which project a user may select**: membership, checked by the API on every
  request. A forged `X-Project-Id` gets 404, never another user's data.
- **Creating users, projects, keys**: `SECURITY DEFINER` functions that check
  membership themselves. The application's database role has no `INSERT` on
  those tables at all.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Google: `redirect_uri_mismatch` | The callback URI is not registered exactly in step 1.3 |
| Login page: "not configured" | Dashboard lacks `GOOGLE_CLIENT_ID`, or API lacks the step 2 secrets |
| Login page: "cannot sign in here" | Sign-up allowlist, unverified email, or not a test user while the consent screen is in Testing |
| API 400 on `/v1/auth/google` | The callback URI is missing from `AUTH_REDIRECT_URIS` |
| Signed in, but no traces | Your personal project is empty. Create a key under API keys, or `--grant` yourself an existing project |
| Vercel login page before the app | Deployment Protection is still on for the dashboard project |
