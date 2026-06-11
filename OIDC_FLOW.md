# OIDC Authentication — Junior Developer Guide

**Goal:** After reading this, you should be able to explain OIDC to someone else AND implement it in your next project from scratch.

This guide uses the **Coresight Market Data Portal** as the real-world example. Every code reference points to our actual files.

---

## Table of Contents

1. [What Problem Does OIDC Solve?](#1-what-problem-does-oidc-solve)
2. [The Three Players](#2-the-three-players)
3. [What Is a Token? (And Why Do We Have Three?)](#3-what-is-a-token-and-why-do-we-have-three)
4. [PKCE — The Security Upgrade](#4-pkce--the-security-upgrade)
5. [The State Parameter — A Security Envelope](#5-the-state-parameter--a-security-envelope)
6. [The Authorize URL — Every Parameter Explained](#6-the-authorize-url--every-parameter-explained)
7. [The Full Login Flow — Every Single Step](#7-the-full-login-flow--every-single-step)
8. [The Cookie — How We Remember You](#8-the-cookie--how-we-remember-you)
9. [The Full Logout Flow — Every Single Step](#9-the-full-logout-flow--every-single-step)
10. [How We Check Auth on Every Page](#10-how-we-check-auth-on-every-page)
11. [Temporary Files — Our Server-Side Memory](#11-temporary-files--our-server-side-memory)
12. [The Database — What and Why](#12-the-database--what-and-why)
13. [Common Mistakes and Why We Avoid Them](#13-common-mistakes-and-why-we-avoid-them)
14. [Visual Summary — Full Sequence Diagram](#14-visual-summary--full-sequence-diagram)
15. [Quick Reference — Key Config Values](#15-quick-reference--key-config-values)

---

## 1. What Problem Does OIDC Solve?

Imagine you build a new app — the Coresight Market Data Portal. You need users to log in. You have two choices:

### Option A — Build your own login system
You store usernames, hashed passwords, handle password resets, deal with brute-force attacks, manage "forgot password" emails... This is months of work and you can still get it wrong.

### Option B — OIDC (OpenID Connect)
You say: **"Hey Coresight's main website, you already know who your users are. When someone wants to use my app, ask them to log in on your site, then just tell me who they are."**

That's OIDC. It lets your app **delegate authentication** to a trusted system (called an **Identity Provider**, or IdP) that already manages users.

**The real-world analogy:**

Think about how hotels work. When you check in, the hotel doesn't verify your entire identity history. They ask for your **government-issued ID** (which a trusted authority already verified). They check it, believe it, and give you a room key.

- **Government = IdP** (Coresight's WordPress site)
- **Passport = id_token** (proof of who you are)
- **Hotel = our app** (Coresight Market Data Portal)
- **Room key = auth_session cookie** (our own session token)

OIDC is just a standardized protocol for this delegation. It's built on top of **OAuth 2.0**.

---

## 2. The Three Players

In every OIDC flow, there are exactly three parties:

```
┌─────────────┐        ┌──────────────────────────┐        ┌──────────────────────────┐
│             │        │                          │        │                          │
│   BROWSER   │◄──────►│   OUR APP (Streamlit)    │◄──────►│   IdP (coresight.com)    │
│             │        │  marketdata-stg.coresight │        │  /csr-idp/               │
│   (User)    │        │                          │        │  (WordPress OIDC plugin) │
└─────────────┘        └──────────────────────────┘        └──────────────────────────┘

   End User                  Relying Party (RP)                 Identity Provider (IdP)
```

| Player | Technical Name | What They Do |
|--------|---------------|--------------|
| Browser | User Agent | Carries tokens and cookies; the user operates it |
| Our App | Relying Party (RP) | Trusts the IdP; manages sessions after login |
| Coresight WordPress | Identity Provider (IdP) | Verifies the user's password; issues tokens |

**Important distinction:** The browser and our app are different. The browser is a thin client — it just carries tokens around and follows redirects. Our Streamlit server is a Python process that does the real work (token exchange, user validation, session creation).

---

## 3. What Is a Token? (And Why Do We Have Three?)

A **token** is just a string of text that carries information or grants access. OIDC produces three distinct ones.

### 3.1 The Authorization Code

**What it is:** A short-lived, one-time-use string (like a numbered ticket at a deli counter). It proves the user successfully authenticated at the IdP.

**What it looks like:** `XXXXXXXXXXXXXXXXXXXXXXXXXXXX` (opaque string, ~30-40 chars)

**Lifetime:** Usually 60 seconds. After that, it expires and cannot be used.

**Who holds it:** The browser carries it in the URL `?code=XXXX` back to our app's callback URL.

**What we do with it:** We immediately exchange it for real tokens. See Step 5 in the login flow.

**Why it's not the real token:** If someone intercepts the URL and steals the code, they still can't use it without the **PKCE verifier** (explained in section 4). The code alone is worthless.

---

### 3.2 The access_token

**What it is:** A JWT (JSON Web Token) that proves our app is authorized to act on behalf of the user.

**What it looks like:**
```
eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMiLCJzY29wZSI6Im9wZW5pZCBwcm9maWxlIGVtYWlsIn0.SIGNATURE
```
Three parts separated by dots: `HEADER.PAYLOAD.SIGNATURE`. The middle part (PAYLOAD) is just base64-encoded JSON:
```json
{
  "sub": "123",
  "scope": "openid profile email",
  "exp": 1749560400
}
```

**Who issues it:** The IdP.

**What we use it for:** Calling the IdP's userinfo endpoint to get the user's email and name if those aren't in the id_token.

**After login:** We store it in the database (`market_data_user_sessions.token`) for audit. We never query it back.

**What we do NOT do with it:**
- We do NOT put it in the browser cookie (too large, sensitive)
- We do NOT use it to validate sessions on every page load
- We do NOT call any API with it after the initial login

---

### 3.3 The id_token

**What it is:** A JWT specifically designed for identity. It's the IdP's signed statement saying "this user is who they say they are."

**What it looks like:** Same three-part JWT structure as access_token.

**The PAYLOAD contains identity claims:**
```json
{
  "iss": "https://coresight.com",
  "sub": "123",
  "aud": "market-data",
  "exp": 1749560400,
  "iat": 1749556800,
  "nonce": "abc123",
  "email": "john.doe@coresight.com",
  "data": {
    "user_id": 456,
    "wp_user": {
      "email": "john.doe@coresight.com",
      "name": "John Doe",
      "username": "johndoe"
    }
  }
}
```

**Who issues it:** The IdP (signed with IdP's private key).

**What we use it for:**
1. At login — extract email and name from the claims
2. At logout — send it back to the IdP as `id_token_hint` so the IdP knows which session to destroy

**How we decode it:**
We decode the PAYLOAD section (middle part) by base64url-decoding it. We do NOT verify the signature — we trust it because we received it directly from the IdP over TLS in a server-to-server call WE initiated.

```python
# From login.py line 119
def _decode_jwt_payload_unverified(jwt_token: str) -> Dict[str, Any]:
    payload_b64 = jwt_token.split(".")[1]
    return json.loads(base64url_decode(payload_b64))
```

**Where we store it:**
- `st.session_state.auth_data["id_token"]` — in memory, same session
- `/tmp/_oidc_idtok_{hash20}.json` — on disk, survives page refreshes, for logout

**What we do NOT do with it:**
- We do NOT put it in the browser cookie (would push it over 4 KB browser limit)
- We do NOT use it to check if someone is logged in on every request
- We do NOT need to verify the signature because we are the only ones who fetched it

---

### 3.4 Our Own Session Token — The auth_session Cookie

This is NOT an OIDC token. This is something **we create ourselves** after a successful OIDC login.

**What it is:** A JSON blob we write into a browser cookie.

```json
{
  "session_id": "a3f7c9b2d1e4f8a0b5c6d7e8f9a0b1c2",
  "user_email": "john.doe@coresight.com",
  "user_display_name": "John Doe",
  "login_at": "2026-06-10T12:00:00+00:00"
}
```

**Why we create our own token instead of reusing the id_token or access_token?**

1. **The OIDC tokens expire in ~1 hour.** We want sessions to last 7 days.
2. **The OIDC tokens are large.** Putting them in a cookie would exceed the 4 KB browser limit.
3. **We control our cookie.** We can invalidate it server-side instantly (blacklist).
4. **Security.** Our cookie has only what we need; no sensitive claims.

**The `session_id` inside our cookie:**
- Generated with `uuid.uuid4().hex` (32 hex chars, cryptographically random)
- Used to key our server-side blacklist (logout invalidation)
- Used to find the id_token on disk for logout

---

## 4. PKCE — The Security Upgrade

PKCE stands for **Proof Key for Code Exchange** (pronounced "pixie"). It was invented to solve a specific attack.

### The Attack It Prevents

Without PKCE:
1. Browser gets redirected back with `?code=XXXX`
2. A malicious app on the same device intercepts that redirect
3. The malicious app exchanges the code for tokens using only the `client_id` (public knowledge)
4. Attacker is now logged in as the victim

### How PKCE Fixes It

Before starting the login, our server generates a secret verifier:

```python
# From login.py line 88
code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
# e.g. "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
```

We compute a challenge from it and include it in the authorize request:
```
code_challenge = code_verifier   (using "plain" method — challenge equals verifier)
```

The IdP stores this challenge. When we exchange the code for tokens, we must prove we know the original verifier:

```python
# From login.py line 514-520
form = {
    "grant_type": "authorization_code",
    "code": code,                    # the code from the callback
    "redirect_uri": redirect_uri,
    "client_id": "market-data",
    "code_verifier": cv,             # MUST match what we sent earlier
}
```

The IdP verifies `code_verifier == stored code_challenge`. If they match, exchange succeeds. If someone stole the code, they don't have the verifier → exchange fails.

### Where Is The Verifier Stored?

In our implementation, the verifier (`cv`) is stored **inside the `state` parameter** sent to the IdP. When the IdP returns `?code=XXXX&state=YYYY`, we decode the state to recover the verifier. The state itself is base64url-encoded JSON — see section 5.

```python
# From login.py line 428-455 (_build_authorize_url)
cv = _random_urlsafe(32)           # the verifier
state_payload = {
    "nonce": nonce,
    "cv": cv,                      # stored in state
    "ru": OIDC_REDIRECT_URI,
    "ts": int(datetime.now(timezone.utc).timestamp()),
}
params = {
    "code_challenge": cv,          # same value sent as challenge (plain method)
    "code_challenge_method": "plain",
    "state": _encode_state_payload(state_payload),
    ...
}
```

**Note on "plain" vs "S256":** The S256 method hashes the verifier with SHA-256 before sending it as the challenge. Plain sends the verifier directly as the challenge. S256 is more secure but both prevent the interception attack. We use plain because our IdP supports it and simplicity is fine here.

---

## 5. The State Parameter — A Security Envelope

The `state` parameter serves two purposes:

### Purpose 1 — CSRF Protection

Without state, an attacker could craft a login URL and trick your browser into completing a login flow that the attacker controls (Cross-Site Request Forgery).

With state: we generate a random value before the redirect, store it, and verify that the state in the callback matches what we sent. If they don't match, we reject the callback.

### Purpose 2 — Carrying Data Across the Redirect

In our implementation, state does more than just CSRF protection — it's a small data envelope that carries everything we need to complete the login on the callback side.

**Our state payload:**
```json
{
  "nonce": "abc123xyz...",
  "cv": "dBjftJeZ4CVP...",
  "ru": "https://marketdata-stg.coresight.com",
  "ts": 1749556800,
  "rk": "optional_return_key"
}
```

| Field | Purpose |
|-------|---------|
| `nonce` | Anti-replay; the IdP may embed it in the id_token |
| `cv` | PKCE code verifier — recovered at callback to exchange the code |
| `ru` | The redirect_uri used — for validation |
| `ts` | Timestamp — state is rejected if older than 900 seconds |
| `rk` | Optional — key for cross-host handoff (when callback lands on different host than initiator) |

**How it's encoded:**
```python
# From login.py line 99
def _encode_state_payload(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")
```

The whole payload is base64url-encoded so it survives URL transmission. On callback, we decode it, check the timestamp, and recover the `cv` for the PKCE exchange.

**Expiry check:**
```python
# From login.py line 107
if (datetime.now(timezone.utc).timestamp() - ts) > 900:
    # reject — state is too old
    return None
```

If the user takes longer than 15 minutes to complete login at the IdP, the callback is rejected and they must start over.

---

## 6. The Authorize URL — Every Parameter Explained

This is the URL the browser gets sent to when you click "Sign in with Coresight":

```
GET https://coresight.com/csr-idp/authorize
  ?client_id=market-data
  &redirect_uri=https://marketdata-stg.coresight.com
  &response_type=code
  &scope=openid profile email
  &state=eyJub25jZSI6Ii4uLiIsImN2IjoiLi4uIn0
  &nonce=abc123xyz...
  &code_challenge=dBjftJeZ4CVP...
  &code_challenge_method=plain
```

Let's break down every single piece — where the value comes from, what it means, and what the IdP does with it.

---

### The Base URL: `https://coresight.com/csr-idp/authorize`

**What it is:** The IdP's "authorization endpoint." This is where every OIDC login flow begins.

**Who owns it:** The Coresight WordPress site. They installed a custom WordPress OIDC plugin that registers this URL.

**What happens when the browser hits it:**
The WordPress plugin intercepts the request, reads all the parameters, validates them, and then decides: "Does this browser have an active WordPress session? If yes, issue a code silently. If no, show the login form."

**Where we get this URL from — our code:**
```python
# login.py line 31
IDP_AUTHORIZE_URL = os.getenv("IDP_AUTHORIZE_URL") or f"{IDP_BASE_URL}/csr-idp/authorize"
```

We hardcode it to the Coresight IdP. In your next project, the IdP would give you this URL in their documentation or in their OIDC discovery document (usually found at `/.well-known/openid-configuration`).

---

### `client_id=market-data`

**What it is:** Our app's registered name at the IdP. Like a username for our application.

**Where it comes from:** The Coresight WordPress admin registered our app (the Market Data Portal) as a "client" in the OIDC plugin and gave it the name `market-data`. They told us this name and we hardcoded it.

```python
# login.py line 34
OIDC_CLIENT_ID = "market-data"
```

**What the IdP does with it:**
1. Looks up `market-data` in its client registry
2. Checks that the `redirect_uri` in this request matches the one registered for `market-data`
3. Applies any per-client settings (allowed scopes, token lifetime, etc.)

**What would happen if you sent a wrong `client_id`?**
The IdP would reject the request with an error: `invalid_client`. It won't even show the login form.

**Real-world analogy:** Think of it like showing your building ID badge at the front desk. The building (IdP) checks if your company (client) is registered as an allowed tenant before letting anyone through.

---

### `redirect_uri=https://marketdata-stg.coresight.com`

**What it is:** The URL where the IdP will send the browser back after authentication — with the authorization code attached.

**Where it comes from:**
```python
# login.py line 36
OIDC_REDIRECT_URI = (
    "https://marketdata.coresight.com" if is_production_deploy()
    else "https://marketdata-stg.coresight.com"
)
```

**What the IdP does with it:**
The IdP does NOT just blindly redirect to whatever URL you send. It checks this value against a **pre-registered whitelist**. When we registered `market-data` as a client, we told the Coresight WordPress admin "our callback URL is `https://marketdata-stg.coresight.com`". That URL is stored in the IdP's database.

If the `redirect_uri` in the request doesn't **exactly** match the registered one, the IdP rejects the request. Even a trailing slash difference (`/` vs no `/`) would fail.

**Why this security check matters:**
If there was no whitelist check, an attacker could craft a URL like:
```
?redirect_uri=https://evil.com/steal
```
...and the IdP would send the authorization code to the attacker's site. The whitelist prevents this.

**What happens after login:**
The IdP sends the browser to:
```
https://marketdata-stg.coresight.com/?code=XXXX&state=YYYY
```
Our Streamlit app is running at that URL. It sees the `?code=` and processes the login.

---

### `response_type=code`

**What it is:** Tells the IdP which OAuth 2.0 flow to use.

**The value `code`** means: "Use the Authorization Code Flow. Don't give me the tokens directly — give me a short-lived code that I will exchange server-side."

**What if this was `token` instead?**
That's the old "Implicit Flow" — the IdP would put the tokens directly in the URL fragment (`#access_token=...`). This is now considered insecure and deprecated because:
- Tokens in URLs end up in browser history and server logs
- There's no way to verify the tokens came from the real IdP
- PKCE doesn't work with it

**Always use `response_type=code`.** It's the correct modern choice.

**Where it comes from — our code:**
```python
# login.py line 441
params = {
    ...
    "response_type": "code",
    ...
}
```
This is always hardcoded. There's no reason to make it dynamic.

---

### `scope=openid profile email`

**What it is:** A space-separated list of what information you're requesting about the user.

**Where it comes from:**
```python
# login.py line 37
OIDC_SCOPE = os.getenv("OIDC_SCOPE", "openid profile email").strip()
```

**Breaking down each scope:**

| Scope | What it unlocks in the id_token |
|-------|--------------------------------|
| `openid` | **Required for OIDC.** Without this, you're doing plain OAuth 2.0, not OIDC. Enables the `sub` claim (a unique user identifier). |
| `profile` | Adds `name`, `given_name`, `family_name`, `preferred_username`, `picture`, `updated_at` to the id_token. We use `name` for display name. |
| `email` | Adds `email` and `email_verified` to the id_token. This is how we get `mohdsaeedafri@coresight.com`. |

**What the IdP does with it:**
The IdP checks that the client (`market-data`) is allowed to request these scopes. Then when it builds the id_token, it includes only the claims associated with the requested scopes.

**If you only sent `scope=openid`** — the id_token would contain `sub` (an opaque user ID like `"123"`) but NOT the email. We'd have no idea who the user is. We need `email` to know they're `mohdsaeedafri@coresight.com`.

---

### `state=eyJub25jZSI6Ii4uLiIsImN2IjoiLi4uIn0`

**What it is:** A base64url-encoded JSON payload that we generate on our server and pass to the IdP. The IdP doesn't read it — it just echoes it back to us in the callback URL unchanged.

**Where it comes from — our code:**
```python
# login.py line 429-446
nonce = _random_urlsafe(24)    # e.g. "k9mXp2qR7vLwNcBtYsAj..."
cv    = _random_urlsafe(32)    # e.g. "dBjftJeZ4CVP-mB92K27uhbUJU1p1r..."

state_payload = {
    "nonce": nonce,
    "cv": cv,           # PKCE verifier
    "ru": OIDC_REDIRECT_URI,
    "ts": int(datetime.now(timezone.utc).timestamp()),  # e.g. 1749556800
}

# Then base64url-encode it:
state = base64.urlsafe_b64encode(
    json.dumps(state_payload).encode()
).decode().rstrip("=")
# → "eyJub25jZSI6Ii4uLiIsImN2IjoiLi4uIn0"
```

**Decode that example value to see what's inside:**
```
eyJub25jZSI6Ii4uLiIsImN2IjoiLi4uIn0
→ base64url decode →
{"nonce":"...","cv":"..."}
```

**The `state` carries three critical pieces:**

**1 — CSRF protection (the nonce inside):**
When the IdP sends the browser back to our callback with `?state=eyJ...`, we decode it and compare the nonce against what we stored in session_state. If they don't match, someone is trying to trick us into accepting a fake callback.

**2 — The PKCE verifier (`cv`):**
This is the most important thing stored in state. When we exchange the `code` for tokens, we need to prove we have the original PKCE verifier. But at callback time, we're in a completely new Streamlit render — we don't have the verifier in memory anymore. By embedding `cv` in the state parameter, the IdP carries it back to us.

```python
# At callback time (login.py line 507-508):
state_payload = _decode_state_payload(_cb_state)
cv = state_payload.get("cv")   # We recover the verifier from state
```

**3 — Timestamp (`ts`):**
We check if the state is older than 900 seconds. If someone bookmarks a login URL and tries to use it 30 minutes later, we reject it:
```python
# login.py line 107
if (datetime.now(timezone.utc).timestamp() - ts) > 900:
    return None  # Expired — reject
```

**What the IdP does with state:**
Absolutely nothing. The IdP treats `state` as an opaque blob. It just stores it and returns it in the redirect:
```
https://marketdata-stg.coresight.com/?code=XXXX&state=eyJub25jZSI6Ii4uLiJ9
```
We are the only ones who know how to decode it.

---

### `nonce=abc123xyz...`

**What it is:** A random one-time value included in the authorize request. The IdP embeds it inside the id_token payload as a claim.

**Where it comes from:**
```python
# login.py line 429
nonce = _random_urlsafe(24)  # 24 bytes of randomness → base64url string
```

Note that `nonce` appears **twice** in the authorize request:
1. As a query parameter: `&nonce=abc123...` → the IdP puts this in the id_token
2. Inside the state payload: `state_payload["nonce"] = nonce` → we carry it ourselves

**What the IdP does with it:**
Embeds the nonce value as a claim in the id_token:
```json
{
  "sub": "123",
  "email": "mohdsaeedafri@coresight.com",
  "nonce": "abc123xyz...",    ← IdP puts this here
  "exp": 1749560400,
  ...
}
```

**What we do with it:**
Strictly speaking, you should decode the id_token, read the `nonce` claim, and verify it matches what you sent. This prevents **replay attacks** — if an attacker captures your id_token and tries to reuse it for a different login session, the nonce won't match.

In our current implementation we store the nonce in the state payload but our codebase does not explicitly re-verify it from the id_token claims. The PKCE flow already covers the main attack vector. Full nonce verification is a best practice for systems that need it.

---

### `code_challenge=dBjftJeZ4CVP...`

**What it is:** The PKCE "challenge" — a value derived from our secret PKCE verifier. The IdP stores this and uses it to verify us at token exchange time.

**Where it comes from:**
```python
# login.py line 429
cv = _random_urlsafe(32)   # 32 bytes → base64url → "dBjftJeZ4CVP-mB92K27..."
```

Because we use `code_challenge_method=plain`, the challenge equals the verifier directly:
```python
# login.py line 447-448
"code_challenge": cv,              # same value as the verifier
"code_challenge_method": "plain",
```

If we were using `S256` (more secure method), it would be:
```python
code_challenge = base64url(sha256(cv))   # hash it first
```

**What the IdP does with it:**
The IdP stores `dBjftJeZ4CVP...` alongside the authorization code. When we later come to exchange the code, we must send the original `cv` again. The IdP re-computes the challenge from what we send and checks it matches what was stored. If they match — we are the same party that initiated the request. If not — someone stole the code and we reject them.

**The full challenge verification flow:**

```
Login time:
  We generate cv = "dBjftJeZ4CVP..."
  We send code_challenge = "dBjftJeZ4CVP..." to IdP
  IdP stores: code → "XXXX", challenge → "dBjftJeZ4CVP..."

Callback time:
  IdP sends us code = "XXXX"

Token exchange time:
  We send: code = "XXXX", code_verifier = "dBjftJeZ4CVP..."
  IdP checks: plain(code_verifier) == stored_challenge?
              "dBjftJeZ4CVP..." == "dBjftJeZ4CVP..." → YES
  IdP issues tokens.

Attacker who stole the code but NOT the verifier:
  Attacker sends: code = "XXXX", code_verifier = ??? (they don't know this)
  IdP checks: plain(???) == "dBjftJeZ4CVP..." → NO
  IdP rejects with: error=invalid_grant
```

---

### `code_challenge_method=plain`

**What it is:** Tells the IdP how the `code_challenge` was computed from the `code_verifier`.

**Two possible values:**

| Method | How challenge is computed | Security level |
|--------|--------------------------|----------------|
| `plain` | `challenge = verifier` (identical) | Good — verifier is never in the URL, only the challenge |
| `S256` | `challenge = BASE64URL(SHA256(verifier))` | Better — even if someone intercepts the challenge, they can't reverse-engineer the verifier |

**We use `plain`** — why?
- Our IdP (Coresight WordPress plugin) supports it
- The token exchange is server-to-server over TLS — the verifier is only ever sent over an encrypted connection
- `plain` is simpler and there's no practical attack against it in our setup

**In your next project:** Always use `S256` if your IdP supports it. It's the recommended choice per the PKCE RFC (RFC 7636).

---

### How All Of This Proves You Are `mohdsaeedafri@coresight.com`

Let's trace the chain of trust end to end:

```
Step 1 — You click "Sign in with Coresight"
  → Browser navigates to https://coresight.com/csr-idp/authorize?...

Step 2 — WordPress IdP sees the request
  → Checks: does this browser have a wordpress_logged_in_* cookie?
  → If not: shows the Coresight login form

Step 3 — You type your Coresight credentials
  → Username: mohdsaeedafri@coresight.com
  → Password: (your password)
  → WordPress verifies these against its own user database
  → WordPress KNOWS you are mohdsaeedafri@coresight.com because you just proved it with your password

Step 4 — IdP issues the authorization code
  → Generates a random code: "XXXX"
  → Stores in its database: code="XXXX" → user="mohdsaeedafri@coresight.com"
  → Redirects browser to: https://marketdata-stg.coresight.com/?code=XXXX&state=eyJ...

Step 5 — Our server exchanges the code
  → POST /csr-idp/token with code="XXXX" and code_verifier="dBjftJeZ4CVP..."
  → IdP looks up code "XXXX" → finds "this code belongs to mohdsaeedafri@coresight.com"
  → IdP builds an id_token JWT with:
     {
       "email": "mohdsaeedafri@coresight.com",   ← HERE is the identity
       "sub": "42",
       "aud": "market-data",
       ...
     }
  → IdP signs the JWT with its private key
  → Sends id_token + access_token to our server

Step 6 — Our server reads the id_token
  → Decodes the base64url payload
  → Reads claims["email"] → "mohdsaeedafri@coresight.com"
  → We now KNOW it's you because:
       a) The code came from the IdP's redirect (PKCE verifier matched)
       b) The id_token was signed by the IdP (TLS + server-to-server call)
       c) The IdP only put your email in it because YOU authenticated with your password

Step 7 — Domain check
  → "mohdsaeedafri@coresight.com".endswith("@coresight.com") → True
  → Allowed to continue

Step 8 — We create OUR session
  → session_id = uuid4().hex
  → Write cookie: auth_session = {"session_id": "a3f7...", "user_email": "mohdsaeedafri@coresight.com", ...}
  → From this point on, every page just checks this cookie
  → No more OIDC involved — that all happened once at login time
```

**The key insight:** You only prove your identity once — at Step 3 when you type your password to WordPress. Everything else is just our system passing that proof around in a secure, tamper-proof way.

**Why we trust the id_token without verifying the signature:**
We received it in Step 5 via a direct HTTPS call from our Python server to the IdP. We initiated that call. TLS ensures nobody tampered with the response in transit. If someone wanted to forge an id_token saying "this is mohdsaeedafri@coresight.com", they would need the IdP's private key, which they don't have.

**Visual chain of trust:**

```
Your password → WordPress database check → WordPress says "yes, this is mohdsaeedafri@coresight.com"
     ↓
WordPress issues code "XXXX" → linked to your email in IdP's database
     ↓
Our server exchanges code + PKCE verifier → IdP confirms the verifier matches
     ↓
IdP builds id_token with email claim → "mohdsaeedafri@coresight.com"
     ↓
Our server reads email from id_token → validates @coresight.com domain
     ↓
We create session_id, write auth_session cookie with your email
     ↓
Every future page: read cookie → find "mohdsaeedafri@coresight.com" → allow access
```

The password never leaves the WordPress site. Our app never sees it. We only ever see the email, display name, and the OIDC tokens — and we get those only after the IdP has already verified the password on its end.

---

## 6.1 The Biggest Misconception — Where Does Your Password Actually Go?

> **You asked:** "In the Authorization URL I don't see where you're sending my email and password. How does the IdP know it's me?"

This question means you've understood something important — and you've hit the most confusing part of OIDC. Let's clear it up completely before anything else.

---

### The Short Answer

**Your password never goes to our app. Not once. Ever.**

Our Streamlit app never asks for your password. Our Python server never sees it. It doesn't go in the authorize URL. It doesn't go in the token exchange. It doesn't go anywhere in our system.

**Your password goes only to `coresight.com` — the Coresight WordPress site. Directly. In your browser.**

---

### What Actually Happens — The Browser Location Bar Is The Key

The authorize URL is not an **API call**. It is a **browser navigation** — like typing a website address.

When our app redirects you to:
```
https://coresight.com/csr-idp/authorize?client_id=market-data&...
```

Your browser **leaves our website entirely** and goes to `coresight.com`. Look at your browser's address bar at that moment:

```
Before clicking "Sign in":
┌─────────────────────────────────────────────────────────────────────┐
│ 🔒 marketdata-stg.coresight.com/login                               │  ← OUR SITE
└─────────────────────────────────────────────────────────────────────┘

After clicking "Sign in" (browser navigated away):
┌─────────────────────────────────────────────────────────────────────┐
│ 🔒 coresight.com/csr-idp/authorize?client_id=market-data&...        │  ← THEIR SITE
└─────────────────────────────────────────────────────────────────────┘

You type email + password here (on coresight.com's login form):
┌─────────────────────────────────────────────────────────────────────┐
│ 🔒 coresight.com/wp-login.php  (or similar WordPress login page)     │  ← STILL THEIR SITE
└─────────────────────────────────────────────────────────────────────┘

After successful login, browser redirected back:
┌─────────────────────────────────────────────────────────────────────┐
│ 🔒 marketdata-stg.coresight.com/?code=XXXX&state=eyJ...             │  ← OUR SITE AGAIN
└─────────────────────────────────────────────────────────────────────┘
```

**Our server is offline the entire time you're on `coresight.com`.**
We have zero visibility into what happens there. We're just waiting.

---

### The Analogy That Makes It Click

Think of Google Sign-In. When a website says "Continue with Google" and you click it:

1. Your browser goes to **Google's** login page (`accounts.google.com`)
2. You type your Google password on **Google's page**
3. Google verifies your password against **Google's database**
4. Google redirects you back to the original website with a code
5. The original website never saw your Google password

That's exactly what's happening here:
- "Continue with Google" → "Sign in with Coresight"
- Google → Coresight WordPress site
- Google's database → WordPress user database
- Your Google password → Your Coresight WordPress password

---

### So What Does the Authorize URL Actually Do?

The authorize URL is not "send credentials here." It is "**go here, so the IdP can verify you itself**."

```
Our app builds this URL:
https://coresight.com/csr-idp/authorize?client_id=market-data&...

This URL says to the IdP:
  "Hey coresight.com, I'm the market-data app.
   I need you to figure out who this person is.
   After you verify them, send them back to marketdata-stg.coresight.com with a code.
   The code should be scoped for openid, profile, and email claims."

That's it. The authorize URL is an INSTRUCTION to the IdP.
It doesn't send credentials — it asks the IdP to COLLECT credentials.
```

The IdP then:
1. Shows its own login form
2. Collects your email and password
3. Verifies them against its own user database
4. After successful verification, generates the authorization code
5. Redirects your browser back to us

---

### The Two Calls — What Each One Actually Is

```
CALL 1 — "Authorization" (NOT an API call)
─────────────────────────────────────────
Type:     Browser navigation (the user's browser physically goes there)
Who:      Browser
To:       https://coresight.com/csr-idp/authorize
Purpose:  Get the user to prove their identity TO THE IDP
          Our app does nothing during this. We're just waiting.
Result:   Browser comes back to us with ?code=XXXX in the URL

CALL 2 — "Token Exchange" (a real API call)
──────────────────────────────────────────
Type:     HTTP POST (our Python server makes this, browser doesn't know)
Who:      Our Streamlit server
To:       https://coresight.com/csr-idp/token
Purpose:  Exchange the code for actual tokens
          This is where we prove WE ARE A LEGIT APP (client_secret + PKCE)
Result:   We get access_token + id_token
          id_token contains: "email": "mohdsaeedafri@coresight.com"
```

---

### Where Is Your Password During All Of This?

```
Timeline of where your password exists:

You type password    → Lives in your browser's password field (on coresight.com)
                                            │
                                            ▼
Browser sends it     → Goes to coresight.com/wp-login.php
                       (standard HTTPS POST, same as any website login)
                                            │
                                            ▼
WordPress verifies   → Checks against wp_users table in WordPress database
                       ONLY WordPress ever touches your password.
                                            │
                     ← Password is DONE. Never mentioned again. →
                                            │
                                            ▼
WordPress generates  → An authorization code "XXXX"
                       This code is NOT your password.
                       It's a temporary receipt that says
                       "someone authenticated successfully"
                                            │
                                            ▼
Browser carries code → ?code=XXXX back to our app
                       Our app sees the CODE, never the password.
                                            │
                                            ▼
Our server exchanges → Code + client_secret + PKCE verifier → tokens
                       id_token contains: "email": "mohdsaeedafri@coresight.com"
                                            │
                                            ▼
We know who you are  → From the email claim in the id_token
```

---

### Why Is This Design Good?

**1 — We never handle your password.**
If our app gets hacked, the attacker gets nothing — we never stored your password.

**2 — You can use the same Coresight password everywhere.**
Any app that integrates with Coresight's IdP works. You don't need separate credentials for each tool.

**3 — Coresight's security team controls authentication.**
They can enforce 2FA, password complexity rules, rate limiting, account lockouts — and all apps that use their IdP benefit automatically. Our app doesn't have to implement any of that.

**4 — We can't leak what we don't have.**
The most common security breach is a stolen password database. We have no password database. Nothing to steal.

---

## 6.5 The Token Exchange Request — Where client_secret Actually Lives

> **Most common confusion:** People think `client_secret` goes into the authorize URL (the browser-facing one). It does NOT. The authorize URL goes to the browser — and the browser is public, insecure, visible in browser history and network logs. Putting the secret there would expose it to anyone.
>
> `client_secret` lives in the **token exchange POST** — a direct server-to-server call from our Python process to the IdP. The browser never sees it. The browser never even knows it happened.

Here is the exact HTTP request our Python server makes:

```
POST https://coresight.com/csr-idp/token
Accept: application/json
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&code=XXXXXXXXXXXXXXXXXXXXXXXXXXXX
&redirect_uri=https://marketdata-stg.coresight.com
&client_id=market-data
&client_secret=IwtYEUtc9nsi)j8g!LGliVsV!OkVn%dQuv0IZfu9hiy(ZOpr
&code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
```

Let's break down every single field — where it came from, how it was built, why it's here, and what the IdP does with it.

---

### The URL: `POST https://coresight.com/csr-idp/token`

**What it is:** The IdP's "token endpoint." This is where authorization codes get exchanged for real tokens.

**Who calls it:** Our **Python server** — not the browser. This is a direct server-to-server HTTP call using the `requests` library.

```python
# login.py line 527-530
resp = requests.post(
    IDP_TOKEN_URL,                           # "https://coresight.com/csr-idp/token"
    headers={"Accept": "application/json",
             "Content-Type": "application/x-www-form-urlencoded"},
    data=form,
    timeout=20,
    allow_redirects=False
)
```

**What `Content-Type: application/x-www-form-urlencoded` means:**
The body is sent as key=value pairs joined by `&`, like a classic HTML form submission. NOT JSON. The OIDC spec requires this format for the token endpoint.

**`allow_redirects=False`:** We never follow redirects on this call. If the IdP tries to redirect us, something is wrong. A proper token response is always HTTP 200 with a JSON body — never a redirect.

**`timeout=20`:** We wait up to 20 seconds. If the IdP is slow or unreachable, we fail cleanly rather than hanging forever.

---

### `grant_type=authorization_code`

**What it is:** Tells the IdP which OAuth 2.0 grant type we're using.

**How it's built:** Hardcoded string. Always `authorization_code` for Authorization Code Flow.

```python
# login.py line 515
form = {
    "grant_type": "authorization_code",
    ...
}
```

**What the IdP does with it:**
The token endpoint handles multiple grant types (`authorization_code`, `refresh_token`, `client_credentials`, etc.). This field tells the IdP which code path to execute. Without this field, the IdP doesn't know what kind of request it's receiving.

**In plain English:**
You're saying: "I have an authorization code. I want to exchange it for tokens. That's why I'm here."

**What would happen with a wrong value:**
The IdP would return `error=unsupported_grant_type`.

---

### `code=XXXXXXXXXXXXXXXXXXXXXXXXXXXX`

**What it is:** The authorization code the browser received from the IdP in the callback URL `?code=XXXX`.

**How it arrived:**
```
Step 1: Browser authenticates at IdP
Step 2: IdP redirects browser to:
        https://marketdata-stg.coresight.com/?code=XXXX&state=eyJ...
Step 3: Our login.py captured it at module load:
        _cb_code = _get_query_params().get("code")
        st.session_state["__oidc_cb_code"] = _cb_code
Step 4: Now we include it in this POST
```

**What the IdP does with it:**
Looks up the code in its internal storage. The IdP stored this when it generated it:
```
code "XXXX" → {
    user: "mohdsaeedafri@coresight.com",
    client_id: "market-data",
    redirect_uri: "https://marketdata-stg.coresight.com",
    code_challenge: "dBjftJeZ4CVP...",
    expires_at: <now + 60 seconds>,
    used: false
}
```

After the exchange, the IdP marks `used: true`. If you try to exchange the same code again, you get `error=invalid_grant`.

**Lifetime:** ~60 seconds from when it was issued. If we take longer than that to exchange it, we get `error=invalid_grant` and the user must start login again.

**What the code looks like:**
It's an opaque random string. We cannot decode it — it has no internal structure for us. Only the IdP knows what it maps to.

---

### `redirect_uri=https://marketdata-stg.coresight.com`

**What it is:** The same redirect URI we used in the authorize request.

**How it's built:**
```python
# login.py line 508
redirect_uri = state_payload.get("ru") or OIDC_REDIRECT_URI
```

We stored the redirect_uri inside the state payload at login time:
```python
# login.py line 432
state_payload = {
    "ru": OIDC_REDIRECT_URI,   # stored in state so we recover it exactly
    ...
}
```

At callback time, we decode the state and read `"ru"` back out. This ensures the value used in the token exchange exactly matches what was used in the authorize request — even if `OIDC_REDIRECT_URI` changes between deployments.

**What the IdP does with it:**
Compares this against what was stored when the code was generated. They must match **exactly**. This is a SECURITY CHECK — it prevents an attacker from generating a code on one redirect_uri and exchanging it on a different one.

**Why send it again? The code already exists at the IdP:**
The OIDC spec requires it as an additional security binding. The redirect_uri is part of what uniquely identifies the authorization request. Sending it again confirms you are the same party who initiated the request. The IdP rejects the exchange if it doesn't match.

**What happens if it doesn't match:**
`error=invalid_grant` — same error as a bad code. The IdP deliberately gives you no information about WHY it failed (security practice: don't leak information).

---

### `client_id=market-data`

**What it is:** Our app's registered name at the IdP. Same value sent in the authorize URL.

**How it's built:**
```python
# login.py line 34
OIDC_CLIENT_ID = "market-data"

# login.py line 518
form["client_id"] = OIDC_CLIENT_ID
```

**What the IdP does with it:**
Finds the registered client `market-data` in its database. Retrieves the associated `client_secret` to verify against what we're sending. Also verifies that the code was issued to `market-data` specifically — a code issued to one client cannot be exchanged by another.

**Why send it again? It was in the authorize URL too:**
The authorize URL goes through the browser. The browser could have modified it (malicious browser extension, man-in-the-middle). Sending `client_id` again in the server-to-server call establishes a clean, trustworthy identity claim that wasn't routed through the browser.

---

### `client_secret=IwtYEUtc9nsi)j8g!LGliVsV!OkVn%dQuv0IZfu9hiy(ZOpr`

**This is the big one. Here's everything about it.**

**What it is:** A shared secret between our app and the IdP. Like a password for our application.

**Where it came from:**
When Coresight's IT admin registered `market-data` as a client in the WordPress OIDC plugin, the plugin generated this secret. The admin then gave it to us (out of band — via Slack, email, or a secrets manager). We hardcode it in our source:

```python
# login.py line 35
OIDC_CLIENT_SECRET = "IwtYEUtc9nsi)j8g!LGliVsV!OkVn%dQuv0IZfu9hiy(ZOpr"
```

**How it's added to the form:**
```python
# login.py line 521-522
if OIDC_CLIENT_SECRET:
    form["client_secret"] = OIDC_CLIENT_SECRET
```

Notice the `if OIDC_CLIENT_SECRET:` check. Some IdP setups use "public clients" (no secret — just PKCE). Ours uses a confidential client (secret + PKCE). Both options are valid; using both is more secure.

**What the IdP does with it:**
Computes something like:
```
stored_secret = "IwtYEUtc9nsi)j8g!LGliVsV!OkVn%dQuv0IZfu9hiy(ZOpr"
                (or a hash of it, depending on how the plugin stores it)

if received_secret == stored_secret:
    "This is genuinely the market-data app"
    → proceed with token issuance
else:
    "This is NOT the real market-data app"
    → return error=invalid_client
```

**Why does this matter? PKCE already protects us:**
PKCE (the `code_verifier` we'll see in a moment) already proves we're the party who initiated the login. So why do we also need a `client_secret`?

Think of it this way:
- **PKCE** proves: "I am the SAME PROCESS that built the authorize URL"
- **client_secret** proves: "I am the LEGITIMATE REGISTERED APPLICATION called `market-data`"

These are two different claims. PKCE prevents code interception. The client_secret prevents someone from registering a fake app called `market-data` on a different IdP and trying to impersonate us. Together they give defense in depth.

**The critical security rule: client_secret NEVER goes to the browser.**

Look at the two requests side by side:

```
Authorize URL (BROWSER-FACING — visible to user, browser history, proxies):
https://coresight.com/csr-idp/authorize
  ?client_id=market-data          ✅ Safe to expose
  &redirect_uri=...               ✅ Safe to expose
  &response_type=code             ✅ Safe to expose
  &scope=openid profile email     ✅ Safe to expose
  &state=eyJ...                   ✅ Safe to expose (we control the encoding)
  &nonce=abc123                   ✅ Safe to expose
  &code_challenge=dBjftJeZ...     ✅ Safe to expose (challenge, not verifier)
  &code_challenge_method=plain    ✅ Safe to expose
  ← NO client_secret here ←

Token Exchange (SERVER-TO-SERVER — Python → IdP, browser never sees this):
POST https://coresight.com/csr-idp/token
  grant_type=authorization_code   ✅ Fine
  code=XXXX                       ✅ Fine (one-time use)
  redirect_uri=...                ✅ Fine
  client_id=market-data           ✅ Fine
  client_secret=IwtYEUtc9nsi...   🔐 SECRET — only here, only server-to-server
  code_verifier=dBjftJeZ...       🔐 SECRET — only here, only server-to-server
```

If `client_secret` was in the authorize URL, anyone who clicks "View Source" or opens Browser DevTools could steal it.

---

### `code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk`

**What it is:** The original PKCE secret we generated before starting the login. This is the proof that we are the same process that built the authorize URL.

**How it was built (at the START of login, in Step 1):**
```python
# login.py line 429
cv = _random_urlsafe(32)
# os.urandom(32) → 32 random bytes → base64url encode → strip "="
# Result: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
```

**How it survived the redirect:**
The verifier was embedded in the `state` payload:
```python
state_payload = {
    "cv": cv,    # stored here
    ...
}
# → base64url encoded → sent as &state=eyJ... to IdP
# → IdP echoes state back in the callback URL unchanged
# → We decode state at callback time:
state_payload = _decode_state_payload(_cb_state)
cv = state_payload.get("cv")   # recovered here
```

The verifier travelled from our server → embedded in state → to IdP → echoed back in URL → to our server. The browser carried the state but could not decode it (it's just base64 to the browser). The verifier was never exposed in a form the browser can use.

**What the IdP does with it:**
```
At authorize time, we sent:
  code_challenge = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  (plain method)

At token exchange time, IdP checks:
  plain(code_verifier) == stored code_challenge?
  "dBjftJeZ4CVP..." == "dBjftJeZ4CVP..." → MATCH → proceed

If an attacker stole the code but NOT the verifier:
  plain(attacker_guess) == "dBjftJeZ4CVP..." → NO MATCH → error=invalid_grant
```

**The verifier is 32 bytes of randomness.** There are 2^256 possible values. Brute-forcing it is computationally impossible.

---

### How All Six Fields Work Together — The IdP's Verification Checklist

When our POST arrives at the IdP, here's the full checklist it runs:

```
Receive POST /token with:
  grant_type=authorization_code
  code=XXXX
  redirect_uri=https://marketdata-stg.coresight.com
  client_id=market-data
  client_secret=IwtYEUtc9nsi...
  code_verifier=dBjftJeZ4CVP...

IdP checks (ALL must pass):

  ① Does code "XXXX" exist in our database?
    → YES (we generated it when the user authenticated)
    → FAIL: error=invalid_grant ("code not found")

  ② Has code "XXXX" already been used?
    → NO (first exchange)
    → FAIL: error=invalid_grant ("code already used") — also invalidates all tokens
             issued from this code (replay detection)

  ③ Has code "XXXX" expired? (usually 60s)
    → NO (we exchanged it within seconds)
    → FAIL: error=invalid_grant ("code expired")

  ④ Does client_id "market-data" match the client the code was issued to?
    → YES
    → FAIL: error=invalid_grant ("code was not issued to this client")

  ⑤ Does client_secret match what we have stored for "market-data"?
    → YES
    → FAIL: error=invalid_client ("bad client credentials")

  ⑥ Does redirect_uri match what was used in the authorize request?
    → YES (both are "https://marketdata-stg.coresight.com")
    → FAIL: error=invalid_grant ("redirect_uri mismatch")

  ⑦ Does plain(code_verifier) match the stored code_challenge?
    → plain("dBjftJeZ4CVP...") == "dBjftJeZ4CVP..." → YES
    → FAIL: error=invalid_grant ("PKCE verification failed")

ALL 7 CHECKS PASSED → issue access_token + id_token → HTTP 200

{
  "access_token": "eyJhbGciOiJSUzI1NiJ9...",
  "id_token": "eyJhbGciOiJSUzI1NiJ9...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

Every single check is there for a reason:
- ① ② ③ — Code validity: ensures you have a real, fresh, unused code
- ④ — Client binding: code cannot be stolen by another client
- ⑤ — Client authentication: only the real `market-data` app can exchange
- ⑥ — Request binding: ties the exchange to the original authorize request
- ⑦ — PKCE: proves same process initiated AND completed the flow

**Only when ALL seven pass does the IdP know:** "This is genuinely the `market-data` application, it has a real unused code, the user who authenticated is `mohdsaeedafri@coresight.com`, and the same process that started the login is completing it."

---

### Where Does the client_secret Live in Our Codebase?

```python
# login.py line 35 (hardcoded — should be in an env var in production)
OIDC_CLIENT_SECRET = "IwtYEUtc9nsi)j8g!LGliVsV!OkVn%dQuv0IZfu9hiy(ZOpr"

# login.py line 521-522 — added to the POST body:
if OIDC_CLIENT_SECRET:
    form["client_secret"] = OIDC_CLIENT_SECRET
```

**Best practice for your next project:** Never hardcode secrets. Store in environment variables:
```python
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET")
```
Then set it in your deployment platform (Azure App Service → Configuration → Application Settings, or a `.env` file locally that is gitignored).

---

### Complete Picture — What Travels Where

```
                        WHAT MOVES            WHO CARRIES IT      VISIBLE TO BROWSER?
────────────────────────────────────────────────────────────────────────────────────────
Authorize URL           client_id             Browser URL               YES
                        redirect_uri          Browser URL               YES
                        response_type         Browser URL               YES
                        scope                 Browser URL               YES
                        state (encoded)       Browser URL               YES (but opaque)
                        nonce                 Browser URL               YES
                        code_challenge        Browser URL               YES (but useless alone)
                        code_challenge_method Browser URL               YES

IdP Callback            code (auth code)      Browser URL               YES (one-time, useless alone)
                        state (echoed back)   Browser URL               YES

Token Exchange POST     grant_type            Server → IdP directly     NO ✅
                        code                  Server → IdP directly     NO ✅
                        redirect_uri          Server → IdP directly     NO ✅
                        client_id             Server → IdP directly     NO ✅
                        client_secret 🔐      Server → IdP directly     NO ✅
                        code_verifier 🔐      Server → IdP directly     NO ✅

Token Response          access_token 🔐       IdP → Server directly     NO ✅
                        id_token 🔐           IdP → Server directly     NO ✅

Our Cookie              session_id            Browser cookie            YES (encrypted by HTTPS)
                        user_email            Browser cookie            YES
                        user_display_name     Browser cookie            YES
                        login_at              Browser cookie            YES
```

The dividing line is the **token exchange**. Everything above it (authorize URL, callback) goes through the browser. Everything from the token exchange down is server-only. That's why `client_secret` and the actual tokens are safe.

---

## 7. The Full Login Flow — Every Single Step

Let's walk through exactly what happens from the moment a user hits our site to the moment they see the dashboard.

### Step 0 — User Arrives at /login

The page (`app/pages/login.py`) starts loading. Before any UI is rendered, the **very first thing Python does** is check if the user is already logged in:

```python
# From login.py line 928
_fast_raw = st.context.cookies.get("auth_session")
```

`st.context.cookies` reads the **HTTP request headers** synchronously. If the browser sent an `auth_session` cookie with this request (meaning the user is already logged in), we parse it and redirect straight to `/home`. No login form is shown at all.

```
Already logged in?  ──YES──►  Show "Loading your workspace..." overlay ──► redirect to /home
                      │
                     NO
                      │
                      ▼
            Render login page
```

### Step 1 — Render the Login Page

The Python server builds the authorize URL **before rendering the button**:

```python
# From login.py line 427-455
_md_sso_url = _build_authorize_url()
```

This URL is embedded as a JavaScript string in the page HTML. When the button is clicked, JavaScript navigates the browser to the IdP — no extra server round-trip needed.

The page renders:
- A "Sign in with Coresight" button
- A loading overlay (hidden initially)

### Step 2 — User Clicks "Sign in with Coresight"

JavaScript runs in the parent Streamlit frame (we inject it via `components.v1.html` because Streamlit runs inside an iframe):

```javascript
// From login.py line 1301-1321
var url = "https://coresight.com/csr-idp/authorize?client_id=market-data&...";

// Show loading overlay
document.getElementById('csr-sso-overlay').style.display = 'flex';

// Navigate to IdP
setTimeout(function() {
    window.location.href = url;
}, 35);
```

The browser now fully leaves our app and goes to the Coresight WordPress site.

**What the browser sends to the IdP:**
```
GET https://coresight.com/csr-idp/authorize
  ?client_id=market-data
  &redirect_uri=https://marketdata-stg.coresight.com
  &response_type=code
  &scope=openid profile email
  &state=eyJub25jZSI6Ii4uLiIsImN2IjoiLi4uIn0
  &nonce=abc123
  &code_challenge=dBjftJeZ4CVP...
  &code_challenge_method=plain
```

### Step 3 — IdP Authenticates the User

The WordPress IdP checks if the user already has an active WordPress session cookie (`wordpress_logged_in_*`):

- **If yes:** Silently skips the login form and goes straight to issuing the code.
- **If no:** Shows the WordPress login form. User enters their Coresight credentials.

After successful authentication, the IdP redirects the browser back to our `redirect_uri` with the authorization code:

```
HTTP 302 Found
Location: https://marketdata-stg.coresight.com/?code=XXXXXXXXXXXX&state=eyJub25jZSI6Ii4uLiJ9
```

### Step 4 — Callback: Code Capture (The Critical Moment)

The browser follows the redirect and arrives at our app again. Streamlit loads `login.py` again, but this time the URL has `?code=XXXX&state=YYYY`.

**The very first thing the module does** is capture these params:

```python
# From login.py line 857-878
if not st.session_state.get("__oidc_cb_captured"):
    _cb_qp    = _get_query_params()
    _cb_code  = _cb_qp.get("code")
    _cb_state = _cb_qp.get("state")
    if _cb_code:
        st.session_state["__oidc_cb_code"]  = _cb_code
        st.session_state["__oidc_cb_state"] = _cb_state
        st.session_state["__oidc_cb_captured"] = True
```

**Why do this at module level (before any function)?**

Streamlit re-renders the page multiple times during a session. The `?code=` query parameter is only present on the **first render** — on subsequent reruns, the URL might be cleaned up. By stashing the code in `session_state` immediately at import time, all subsequent reruns can still access it.

Immediately after capturing, a loading overlay is displayed:

```python
# From login.py line 1054-1059
if st.session_state.get("__oidc_cb_captured"):
    st.markdown(_make_loading_overlay("Completing sign-in…"), unsafe_allow_html=True)
```

This overlay covers the blank page that would otherwise show during the ~2–5 second token exchange.

### Step 5 — Token Exchange (Server-to-Server, Never Browser)

```python
# From login.py line 469-563
_token_payload = _exchange_code_for_tokens(_cb_code, _state_payload)
```

Our Python server makes an HTTP POST directly to the IdP:

```
POST https://coresight.com/csr-idp/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&code=XXXXXXXXXXXX
&redirect_uri=https://marketdata-stg.coresight.com
&client_id=market-data
&client_secret=IwtYEUtc9nsi...
&code_verifier=dBjftJeZ4CVP...   ← recovered from the state payload
```

The IdP verifies:
1. The `code` is valid and not expired
2. The `redirect_uri` matches what was used in the authorize request
3. The `code_verifier` matches the `code_challenge` stored from the authorize request
4. The `client_secret` is correct

If all checks pass, IdP responds with:

```json
{
  "access_token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SIGNATURE",
  "id_token": "eyJhbGciOiJSUzI1NiJ9.eyJlbWFpbCI6Im5hbWVAY29yZXNpZ2h0LmNvbSJ9.SIGNATURE",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

**Deduplication (important in Streamlit):**

Streamlit can trigger multiple rerenders simultaneously. If two rerenders both try to exchange the same code, the second one would get `invalid_grant` (code already used). We prevent this with:

1. A **threading lock** — only one goroutine exchanges at a time
2. Three cache layers checked before making the HTTP call:
   - `st.session_state["__oidc_exchange_result"]` — same Streamlit session memory
   - `_oidc_exchange_cache[code]` — module-level dict, same Python process
   - `/tmp/_oidc_xch_{sha256(code)[:16]}.json` — file, survives worker restart

```python
# From login.py line 486-488
if not _oidc_exchange_lock.acquire(timeout=25):
    st.error("Login timed out. Please try again.")
    return None
```

### Step 6 — Extract User Identity From Tokens

```python
# From login.py line 589-645
_token_data = _build_token_data(_token_payload)
```

**Step 6a — Decode the id_token JWT:**

```python
claims = json.loads(base64url_decode(id_token.split(".")[1]))
```

We look for:
- `claims["email"]` or `claims["user_email"]`
- `claims["data"]["wp_user"]["name"]` (display name)
- `claims["data"]["wp_user"]["username"]` (nicename)
- `claims["data"]["user_id"]` (WordPress user ID)

**Step 6b — Fallback: userinfo endpoint**

If any of those are missing from the id_token claims, we make an API call:

```
GET https://coresight.com/wp-json/csr-idp/v1/userinfo
Authorization: Bearer <access_token>
```

This returns a JSON object with user profile data. We try with `access_token` first, then fall back to `id_token` as the Bearer token.

**Step 6c — Build the token_data dict:**

```python
return {
    "user_email": "john.doe@coresight.com",
    "user_display_name": "John Doe",
    "user_nicename": "johndoe",
    "token": access_token,
    "id_token": id_token,
    "wp_user_id": 456,
}
```

### Step 7 — Domain Restriction Check

```python
# From login.py line 688-691
if not user_email.lower().endswith("@coresight.com"):
    st.error("Access restricted to Coresight employees only.")
    return
```

This check happens in our Python code, not at the IdP. The IdP would happily issue tokens for any Coresight WordPress user. We restrict access to `@coresight.com` emails in our layer.

### Step 8 — Session Creation

```python
# From login.py line 694-696
result = login_user(
    user_email="john.doe@coresight.com",
    user_nicename="johndoe",
    user_display_name="John Doe",
    token=access_token,
    id_token=id_token,
    wp_user_id=456
)
```

Inside `auth_manager.py`, `AuthManager.login()` does four things:

**8a — Generate session ID:**
```python
session_id = uuid.uuid4().hex  # e.g. "a3f7c9b2d1e4f8a0b5c6d7e8f9a0b1c2"
```

**8b — Write audit row to database** (non-blocking — login succeeds even if DB is down):
```sql
INSERT INTO market_data_user_sessions
  (user_email, user_nicename, user_display_name, token, login_at, last_activity)
VALUES
  ('john.doe@coresight.com', 'johndoe', 'John Doe', '<access_token>', NOW(), NOW())
```

**8c — Persist id_token to disk** (needed for logout, even after browser refresh):
```python
# /tmp/_oidc_idtok_{sha256(session_id)[:20]}.json
{
  "id_token": "eyJhbGciOiJSUzI1NiJ9...",
  "user_email": "john.doe@coresight.com",
  "ts": 1749556800
}
```

**8d — Set Streamlit session state:**
```python
st.session_state.auth_data = {
    "session_id": "a3f7c9b2d1e4f8a0b5c6d7e8f9a0b1c2",
    "user_email": "john.doe@coresight.com",
    "user_display_name": "John Doe",
    "login_at": "2026-06-10T12:00:00+00:00",
    "id_token": "eyJ...",     # in memory only — NOT in cookie
    "wp_user_id": 456,
}
st.session_state.authenticated = True
```

### Step 9 — Write the Browser Cookie (The Hardest Part)

This is where Streamlit makes things complicated.

**The problem:** Streamlit runs inside an iframe sandbox. The iframe cannot write cookies to the parent document directly. Normal `document.cookie = ...` in the iframe is sandboxed away.

**The solution:** Inject a `<script>` tag into the **parent document** using `components.v1.html`. The script runs in the parent frame context, which CAN write cookies.

```python
# From auth_manager.py line 347-429 (render_auth_cookie_handoff_redirect)
def render_auth_cookie_handoff_redirect(target_path="/home", message="Completing sign-in…"):
    slim_payload = {
        "session_id": session_id,
        "user_email": user_email,
        "user_display_name": user_display_name,
        "login_at": login_at,
    }
    # Notice: id_token is NOT included — cookie stays small
    cookie_value_json = json.dumps(slim_payload)
```

The injected JavaScript does this in one atomic sequence:

```javascript
// 1. Clear any stale cookies (both domain variants)
document.cookie = "auth_session=; expires=<past>; path=/; SameSite=Lax; Secure";
document.cookie = "auth_session=; expires=<past>; path=/; domain=.coresight.com; SameSite=Lax; Secure";

// 2. Set the new cookie (host-only — no Domain= attribute)
document.cookie = "auth_session=%7B%22session_id%22%3A%22a3f7...%22%7D; expires=Wed, 17 Jun 2026 12:00:00 GMT; path=/; SameSite=Lax; Secure";

// 3. Poll to confirm cookie was written (up to 1 second, checks every 50ms)
var ok = false, n = 0;
function poll() {
    n++;
    ok = document.cookie.indexOf('auth_session=') !== -1;
    if (ok || n >= 20) {
        // Redirect to /home if cookie written, or back to /login with error
        window.location.replace(ok ? "/home?login_handoff=1" : "/login?auth_error=cookie_write_failed");
        return;
    }
    setTimeout(poll, 50);
}
poll();
```

**Why poll?** Writing `document.cookie` is synchronous in JavaScript but reading it back can have a tiny delay in some browsers. The poll confirms the cookie is visible before redirecting.

**Why "host-only"?** We don't set a `Domain=` attribute, so the cookie is scoped to exactly `marketdata-stg.coresight.com`. It won't be sent to other coresight.com subdomains. More secure.

### Step 10 — Landing on /home

The browser redirects to `/home`. The browser sends the `auth_session` cookie in the HTTP request headers. Streamlit loads `home.py`, which calls `require_auth()`. `require_auth()` reads the cookie from `st.context.cookies`, finds the session, and allows the page to load.

**Login is complete.**

---

## 8. The Cookie — How We Remember You

The `auth_session` cookie is our session token. It's the only thing checked on every page load.

```
Cookie name:  auth_session
Cookie value: %7B%22session_id%22%3A%22a3f7...%22%2C%22user_email%22%3A%22...%22%7D
              (URL-encoded JSON)
Expires:      7 days from login
Path:         /
Domain:       .coresight.com (or host-only in some cases)
SameSite:     Lax
Secure:       Yes (on HTTPS)
HttpOnly:     No (we need JS to write it)
```

**When the browser sends it:**

On every HTTP request to our app, the browser automatically includes the cookie in the `Cookie:` header. We read it with:

```python
raw = st.context.cookies.get("auth_session")
```

`st.context.cookies` reads the HTTP request headers — it's synchronous and zero-latency. No waiting for JavaScript to mount.

**The blacklist check:**

Before accepting a cookie, we always check if that session was invalidated (logged out):

```python
# From auth_manager.py line 1029-1037
session_id = parsed.get("session_id", "")
if _is_session_invalidated(session_id):
    return None  # Reject — this session was logged out
```

---

## 9. The Full Logout Flow — Every Single Step

Logout is more complex than login because we need to:
1. Tell the IdP to destroy the WordPress session (so the user can't silently re-authenticate)
2. Clear our browser cookie
3. Invalidate our server-side session

The challenge: these three things happen on different systems and the browser is an unreliable messenger.

### Step 1 — User Clicks Logout

Any page calls `AuthManager.logout()`:

```python
# From auth_manager.py line 1184-1209
def logout(self):
    if is_oidc_enabled():
        self.stash_logout_context()
        st.switch_page("pages/logout_bridge.py")
```

### Step 2 — Stash Logout Context

Before navigating away, we must preserve the `id_token`. Why? Because:
- `logout_bridge.py` is a new page load — it won't have the OIDC tokens in memory
- The `auth_session` cookie doesn't contain the `id_token` (it was excluded to keep cookie small)
- We need `id_token` to tell the IdP which session to destroy

```python
# From auth_manager.py line 635-718 (stash_logout_context)

# 1. Find the id_token (try session_state first, then temp file)
id_token = auth_data.get("id_token") or _load_id_token(session_id)

# 2. Blacklist the session NOW (before any cookie clearing)
invalidate_session(session_id, user_email)
# Writes: /tmp/_oidc_invalid_{sha256(session_id)[:20]}.json
# Effect: Even if browser still sends auth_session cookie, _get_cookie() will reject it

# 3. Write a bridge cookie with the id_token (2-minute TTL)
bridge_data = {"id_token": id_token, "user_email": user_email, ...}
# JS injection writes: auth_logout_bridge=<url-encoded JSON>
```

**Why a separate bridge cookie?** We need to pass the `id_token` to `logout_bridge.py`, which runs as a fresh page. The Streamlit session_state may survive (same WebSocket connection) but we can't rely on it. A cookie is a reliable cross-page carrier.

**Why blacklist before clearing the cookie?**

If we clear the cookie first, then blacklist, there's a window where:
- The cookie is gone (user appears logged out to us)
- But the blacklist isn't written yet
- If the browser makes another request in that window with the stale cookie, we'd let them in

By blacklisting first, any request with the old session_id is rejected from that point forward, regardless of the cookie state.

### Step 3 — logout_bridge.py

This page shows "Logging out..." and does everything silently.

**Step 3a — Recover id_token** from multiple sources (in order of preference):

```
1. st.session_state["__logout_bridge_data"]   ← set by stash_logout_context
2. st.context.cookies.get("auth_logout_bridge")  ← the bridge cookie
3. st.context.cookies.get("auth_session")         ← auth cookie (fallback)
4. CookieController.get("auth_logout_bridge")     ← async cookie fallback
5. /tmp/_oidc_idtok_{hash}.json                   ← disk file (most reliable fallback)
6. st.session_state.auth_data["id_token"]         ← same-session memory
```

**Why so many fallbacks?** Cookies are unreliable in async environments. Streamlit's CookieController can lag. The temp file is the most reliable source.

**Step 3b — Clear session state:**
```python
for key in ["auth_data", "authenticated", "__completed_oidc_code", ...]:
    st.session_state.pop(key, None)
st.session_state["__just_logged_out"] = True  # prevents auto-restore on login page
```

**Step 3c — Single atomic JS: clear cookies + redirect to IdP:**

```javascript
// In one script, atomically:

// 1. Clear auth_session (both host-only and domain variants)
document.cookie = "auth_session=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax; Secure";
document.cookie = "auth_session=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; domain=.coresight.com; SameSite=Lax; Secure";

// 2. Clear auth_logout_bridge
document.cookie = "auth_logout_bridge=; expires=...; path=/; SameSite=Lax; Secure";

// 3. Navigate to IdP logout endpoint
window.location.href = "https://coresight.com/csr-idp/logout/?id_token_hint=eyJ...&post_logout_redirect_uri=https://marketdata-stg.coresight.com/";
```

**Why single atomic script?** An earlier version used separate cookie-clear JS + `<meta http-equiv="refresh">` for the redirect. The race condition: the meta-refresh fired before the iframe's JS had loaded/run → cookies were never cleared → user arrived at IdP logout still authenticated → IdP silently re-issued credentials → user was immediately logged back in. Combining everything into one script executed in the parent frame eliminates this race.

### Step 4 — IdP Logout

The browser hits:
```
GET https://coresight.com/csr-idp/logout/
  ?id_token_hint=eyJhbGciOiJSUzI1NiJ9...
  &post_logout_redirect_uri=https://marketdata-stg.coresight.com/
```

The WordPress plugin:
1. Identifies the WordPress session from the `id_token_hint`
2. Destroys the WordPress session cookie (`wordpress_logged_in_*`)
3. Redirects the browser back to `post_logout_redirect_uri`

### Step 5 — Return to Login Page

Browser arrives at `/login`. No cookie. `__logout_guard` in session_state prevents auto-restore. Login form is shown.

---

## 10. How We Check Auth on Every Page

Every protected page starts with this call:

```python
# e.g., from home.py
auth_data = require_auth(page="home")
```

`require_auth()` goes through these checks **in order**. It stops as soon as it finds a valid session or decides to reject.

```
Check 1: Was auth explicitly invalidated (logout)?
  st.session_state.get("_auth_invalidated") == True
  → YES: redirect to /login immediately. No further checks.
         [This is the logout guard — even if cookie still present, reject.]

Check 2: Is there already a session in memory?
  st.session_state.get("authenticated") AND st.session_state.get("auth_data")
  → YES: return auth_data. (Fastest path — no I/O at all, ~0ms)
         [Same Streamlit WebSocket session — session_state persists between reruns]

Check 3: Read cookie from HTTP request headers (synchronous, ~0ms)
  st.context.cookies.get("auth_session")
  → Parse JSON → URL-decode → check blacklist → restore session_state
  → VALID: return auth_data.

Check 4: CookieController fallback (asynchronous — React component)
  controller.get("auth_session")
  → Same parse + blacklist check
  → VALID: return auth_data.

Check 5: Retry budget
  If checks 3+4 failed, sleep [0.15s, 0.35s] and retry
  [Handles race where cookie was JUST set via JS and hasn't propagated yet]

Check 6: All retries exhausted → redirect to /login
  st.switch_page("pages/login.py")
  st.stop()  ← hard stop: execution CANNOT continue past this line
```

**There is NO database call in this path.** Auth on every page is purely cookie-based.

---

## 11. Temporary Files — Our Server-Side Memory

We use `/tmp` files because:
- Streamlit session_state only persists within one WebSocket session
- Browser refreshes or new tabs start fresh session_state
- We need certain data (id_token for logout, blacklist) to survive across page navigations

All temp files use `mode 0o600` (only the process owner can read/write them).

```
/tmp/
  ├── _oidc_xch_{sha256(code)[:16]}.json          # Token exchange cache
  │   Content: {access_token, id_token, token_type, expires_in}
  │   TTL: 5 minutes (dedup window for Streamlit rerenders)
  │
  ├── _oidc_idtok_{sha256(session_id)[:20]}.json   # id_token for logout
  │   Content: {id_token, user_email, ts}
  │   TTL: 7 days (same as cookie)
  │
  ├── _oidc_invalid_{sha256(session_id)[:20]}.json # Session blacklist
  │   Content: {session_id, user_email, ts}
  │   TTL: 7 days
  │
  ├── _oidc_return_{sha256("return:key")[:24]}.json # Cross-host handoff
  │   Content: {origin: "https://localhost:8501", ts}
  │   TTL: 15 minutes
  │
  ├── _oidc_handoff_{sha256("handoff:key")[:24]}.json # Cross-host token pass
  │   Content: {token_data: {...}, ts}
  │   TTL: 3 minutes
  │
  └── _oidc_logout_return_{...}.json               # Post-logout redirect
      Content: {origin: "https://...", ts}
      TTL: 15 minutes
```

**How the hash works:**
```python
h = hashlib.sha256(session_id.encode()).hexdigest()[:20]
path = f"/tmp/_oidc_idtok_{h}.json"
```

The session_id is never in the filename. You can't reverse-engineer the session_id from the filename. The hash is used purely for deterministic lookup.

---

## 12. The Database — What and Why

Table: `market_data_user_sessions`

```sql
INSERT INTO market_data_user_sessions
  (user_email, user_nicename, user_display_name, token, login_at, last_activity)
VALUES
  ('john.doe@coresight.com', 'johndoe', 'John Doe', '<access_token>', NOW(), NOW())
```

**This is audit-only. Auth is NEVER validated against the database.**

Why?
- A DB call on every page load would add ~250ms latency (Azure cross-region RTT)
- The cookie + blacklist file approach is faster and more reliable
- If the DB is down, users can still browse the app (login still works, DB write failure is non-blocking)

**`token` column stores `access_token`** — not `session_id`, not `id_token`. This is for audit — you can see which OIDC tokens were used. The `session_id` (our own UUID) is NOT stored in the DB.

---

## 13. Common Mistakes and Why We Avoid Them

### Mistake 1 — Storing id_token in the Cookie

**Why people do it:** It seems convenient — everything in one place.

**Why it's wrong:** The id_token JWT is typically 1–2 KB. Our slim JSON cookie payload is ~150 bytes. The browser has a 4 KB limit per cookie. Storing the id_token in the cookie leaves almost no room for other fields and risks the cookie being silently dropped by the browser.

**Our solution:** id_token stays in `/tmp`. Cookie only holds session_id + email + display_name + login_at.

---

### Mistake 2 — Using a Single Redirect for Cookie-Then-Navigate

**Why people do it:** `st.switch_page("/home")` seems straightforward.

**Why it's wrong:** The cookie is written by JavaScript. The redirect happens on the server. If you redirect before the JavaScript has confirmed the cookie is written, the browser arrives at `/home` without the cookie — immediately bounced back to `/login`.

**Our solution:** JavaScript polls `document.cookie.indexOf('auth_session=')` every 50ms (up to 1 second) before calling `window.location.replace()`. The redirect only happens after the cookie is confirmed present.

---

### Mistake 3 — Clearing the Cookie Before Blacklisting the Session

**Why people do it:** "I deleted the cookie, the user is logged out."

**Why it's wrong:** There's a window between cookie deletion and server-side invalidation. Any request in that window with the stale cookie would still be accepted. Also, JS cookie deletion is asynchronous — the cookie may still be sent by the browser for a brief moment.

**Our solution:** Write the blacklist file FIRST (`invalidate_session()`), then clear the cookie. Now even if the browser still sends the stale cookie, `_get_cookie()` rejects it.

---

### Mistake 4 — Exchanging the Code More Than Once

**Why it happens:** Streamlit rerenders the page multiple times. Each rerender sees the `?code=` in the URL and tries to exchange it. The IdP rejects the second exchange with `invalid_grant`.

**Our solution:**
- Capture the code at module level into `session_state` immediately
- Add a threading lock + three-layer cache before making the HTTP call
- Once exchanged, the result is cached and subsequent renders use the cache

---

### Mistake 5 — Not Checking If You're Already Logged In Before Showing Login

**Why it matters:** Without the fast restore check, every page load of `/login` would show the login form briefly before redirecting — a jarring flash.

**Our solution:** At the very top of `login.py`, before any UI rendering, check `st.context.cookies.get("auth_session")`. If valid, redirect immediately.

---

### Mistake 6 — Verifying the id_token Signature

**Why people think they need to:** "A JWT should always be verified."

**Why we don't:** We received the id_token directly from the IdP's token endpoint over a TLS connection that WE established. The chain of custody is intact. Signature verification requires fetching the IdP's public keys (another network call). It's extra complexity with no security benefit in this scenario. Signature verification is important when a third party gives you a JWT — here we fetched it ourselves.

---

## 14. Visual Summary — Full Sequence Diagram

```
  Browser                  Streamlit Server             Coresight IdP           MySQL DB
     │                          │                             │                     │
     │── GET /login ───────────►│                             │                     │
     │                          │ check auth_session cookie   │                     │
     │                          │ (no cookie found)           │                     │
     │◄── render login page ────│                             │                     │
     │    (SSO button embedded) │                             │                     │
     │                          │                             │                     │
     │ [user clicks button]     │                             │                     │
     │ JS: window.location =    │                             │                     │
     │  IDP_AUTHORIZE_URL       │                             │                     │
     │     ?code_challenge=CV   │                             │                     │
     │     &state=BASE64(...)   │                             │                     │
     │─────────────────────────────────────────────────────► │                     │
     │                          │                             │ check WP session    │
     │                          │                             │ show login form     │
     │ [user logs in at IdP]    │                             │                     │
     │                          │                             │                     │
     │◄── HTTP 302 ─────────────────────────────────────────── │                    │
     │ Location: /              │                             │                     │
     │ ?code=XXXX&state=YYYY    │                             │                     │
     │                          │                             │                     │
     │── GET /?code=XXXX ──────►│                             │                     │
     │                          │ capture code+state in       │                     │
     │                          │ session_state               │                     │
     │◄── loading overlay ──────│                             │                     │
     │                          │                             │                     │
     │                          │── POST /csr-idp/token ─────►│                     │
     │                          │   grant_type=auth_code      │                     │
     │                          │   code=XXXX                 │                     │
     │                          │   code_verifier=CV          │                     │
     │                          │   client_secret=...         │                     │
     │                          │◄── {access_token,id_token} ─│                     │
     │                          │                             │                     │
     │                          │ decode id_token (no sig)    │                     │
     │                          │ extract email, name         │                     │
     │                          │ [if missing: GET /userinfo] │                     │
     │                          │                             │                     │
     │                          │ validate @coresight.com     │                     │
     │                          │ generate session_id         │                     │
     │                          │ store id_token → /tmp/      │                     │
     │                          │                             │                     │
     │                          │── INSERT user_sessions ─────────────────────────►│
     │                          │◄─────────────────────────────────────────────────│
     │                          │                             │                     │
     │◄─────────────────────────│ inject JS:                  │                     │
     │ JS clears stale cookies  │  set auth_session cookie    │                     │
     │ JS sets auth_session=... │  poll → redirect /home      │                     │
     │ JS polls cookie visible  │                             │                     │
     │ JS: window.location=/home│                             │                     │
     │                          │                             │                     │
     │── GET /home ────────────►│                             │                     │
     │                          │ require_auth():             │                     │
     │                          │  read auth_session cookie   │                     │
     │                          │  check blacklist → OK       │                     │
     │◄── render /home ─────────│                             │                     │
     │                          │                             │                     │
     │                          │                             │                     │
     │ ─ ─ ─ ─ LOGOUT ─ ─ ─ ─  │                             │                     │
     │                          │                             │                     │
     │── [user clicks logout] ─►│                             │                     │
     │                          │ stash_logout_context():     │                     │
     │                          │  load id_token from /tmp/   │                     │
     │                          │  BLACKLIST session_id       │                     │
     │                          │  write bridge cookie (JS)   │                     │
     │◄── bridge cookie set ────│                             │                     │
     │                          │ switch to logout_bridge.py  │                     │
     │                          │                             │                     │
     │── GET /logout_bridge ───►│                             │                     │
     │                          │ read id_token from bridge   │                     │
     │                          │ clear session_state         │                     │
     │◄── inject JS: ───────────│                             │                     │
     │  clear auth_session      │                             │                     │
     │  clear bridge cookie     │                             │                     │
     │  navigate to IdP logout  │                             │                     │
     │── GET /csr-idp/logout/ ─────────────────────────────►  │                     │
     │   ?id_token_hint=eyJ...  │                             │ destroy WP session  │
     │                          │                             │                     │
     │◄── HTTP 302 ─────────────────────────────────────────── │                    │
     │ Location: /login         │                             │                     │
     │                          │                             │                     │
     │── GET /login ───────────►│                             │                     │
     │                          │ no cookie                   │                     │
     │                          │ __logout_guard = True       │                     │
     │◄── render login form ────│                             │                     │
```

---

## 15. Quick Reference — Key Config Values

| What | Value | Defined In |
|------|-------|-----------|
| IdP authorize URL | `https://coresight.com/csr-idp/authorize` | `login.py` line 31 |
| IdP token URL | `https://coresight.com/csr-idp/token` | `login.py` line 32 |
| IdP userinfo URL | `https://coresight.com/wp-json/csr-idp/v1/userinfo` | `login.py` line 33 |
| Client ID | `market-data` | `login.py` line 34 |
| Redirect URI (STG) | `https://marketdata-stg.coresight.com` | `login.py` line 36 |
| Redirect URI (PROD) | `https://marketdata.coresight.com` | `login.py` line 36 |
| Scopes requested | `openid profile email` | `login.py` line 37 |
| PKCE method | `plain` | `login.py` line 448 |
| State max age | 900 seconds | `login.py` line 38 |
| Cookie name | `auth_session` | `auth_manager.py` line 51 |
| Cookie TTL | 7 days | `auth_manager.py` line 52 |
| Cookie domain | `.coresight.com` | `auth_manager.py` line 53 |
| Auth retry delays | `[0.15s, 0.35s]` | `auth_manager.py` line 143 |
| id_token temp file | `/tmp/_oidc_idtok_{sha256(session_id)[:20]}.json` | `auth_manager.py` line 157 |
| Session blacklist | `/tmp/_oidc_invalid_{sha256(session_id)[:20]}.json` | `auth_manager.py` line 204 |
| DB session table | `market_data_user_sessions` | `auth_manager.py` line 568 |

---

## If You're Implementing OIDC in Your Next Project — Checklist

```
□ Register your app with the IdP (get client_id, client_secret, set redirect_uri)
□ Use Authorization Code Flow + PKCE (never Implicit Flow)
□ Generate state as a signed/encoded payload, not just a random string
□ Store the PKCE verifier in the state param (survives the redirect)
□ Capture query params at the earliest possible point (before page rerenders)
□ Make the token exchange server-to-server (never expose client_secret to browser)
□ Decode id_token claims (base64url decode — no signature verification needed
  since you fetched it yourself)
□ Fall back to /userinfo if claims are missing from id_token
□ Create your own slim session token (don't store OIDC tokens in cookies)
□ Generate a random session_id and use it to key your session data
□ Blacklist sessions on logout BEFORE clearing the cookie
□ Store id_token separately for logout (you need it for id_token_hint)
□ Use RP-Initiated Logout (send id_token_hint to IdP) so IdP session is destroyed
□ Validate email domain in your app layer (IdP just proves identity, not access)
□ Never put id_token in the browser cookie (too large)
□ Test the case where cookie write fails (the JS polling approach handles this)
```
