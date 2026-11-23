---
id: ADR-0031
title: Open self-service sign-up with owner approval
status: proposed
date: 2026-11-23
verified_by: "SRV-AUTH FR-AUTH-23..32 and INV-AUTH-10..14 ([db], [browser] and the [live] `signup` step); WEB-ACCOUNTS FR-ACCOUNTS-1..4"
revisit_when: "The stack listens on anything other than 127.0.0.1 (LAN name, tunnel or VPS); more than 20 pending sign-ups in a week; SRV-SHARE (stage 8) gives approved learners access to shared courses; SRV-NOTIFY exists"
supersedes: []
---

> Amends, if accepted: ADR-0017 "v1 foundation" ("Self-service sign-up exists behind a disabled flag; v1 stays
> invitation-only on the owner's Mac") and ADR-0012 "Authentication" ("Others join by invitation link"). Both are
> `proposed`. No accepted ADR is contradicted: ADR-0024 (localhost, RP ID) and ADR-0026 stay as they are.

## Context

Owner decision, 2026-11-23: a "Sign up" page reachable from the sign-in screen. Of the options offered, the owner chose
**open sign-up**: anyone who can reach the server can create an account with a passkey. The owner accepted that this
needs rate limiting and owner approval of new accounts.

What exists today (SRV-AUTH, approved; V/auth-page-style):

- Accounts come only from `users[]` in reader.yaml. At every api start, `sync_users` inserts each configured handle and
  **disables every user whose handle is not in `users[]`** (FR-AUTH-1, INV-AUTH-9). An account created any other way
  would be disabled at the next restart unless this rule changes.
- A passkey is registered only through a one-time bootstrap link (`reader bootstrap`, FR-AUTH-4/-5) or by a signed-in
  user (FR-AUTH-6). Sign-in is usernameless (empty `allowCredentials`, FR-AUTH-3); handles are never typed at sign-in.
- `POST /api/auth/signup/{options,verify}` answer 404 and `sharing.signup` is accepted only absent or `false`
  (FR-AUTH-13, FR-AUTH-16, config error `signup_not_supported`). The key was reserved for this feature.
- Rate limiting (FR-AUTH-11) is **one global counter** of failed ceremonies (100 per hour) in `auth_failures`. There is
  no per-client limit, and the api runs with `proxy_headers=False`, so it does not see client addresses today.
- Roles are `owner` and `learner`. Owner-only routes check `role == "owner"` in their handlers (`403 owner_only`).
- Model spend is per user. The agent proxy loads a provider token **by handle from `users[]`** (SRV-AGENT FR-AGENT-3;
  `provider_missing` otherwise). The tutor has a per-user daily cap (`daily_cap_usd` $3, FR-AGENT-12) and every user
  has `budgets.monthly_usd`. Kernel services are generated per configured user by `reader up` (ADR-0022).
- No spec has a server-side notification path: SRV-NOTIFY (stage 2, Telegram) is not written.
  `tools/agent/notify.mjs` belongs to agents and is not a product path.

Reachability today: Caddy publishes `127.0.0.1:443` only (OPS-DEPLOY compose.yaml), the RP ID is `localhost`
(ADR-0024), and every non-GET request needs `Origin: https://localhost` (FR-AUTH-12).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **A. Open sign-up, pending until the owner approves (chosen by the owner)** | Anyone at the server can ask for an account without the owner's terminal; nothing is granted until approval | A public ceremony that writes rows; rate limits, a pending cap and handle rules to keep it bounded; one owner screen |
| B. Invitation links (ADR-0016's model) | Only invited people can register; no public write path; role and expiry carried by the link | The owner makes a link per person and sends it; rejected by the owner on 2026-11-23 |
| C. CLI-only bootstrap (today: `users[]` plus `reader bootstrap --handle`) | No public write path at all | The owner edits reader.yaml, restarts and runs a command per person |
| D. Open sign-up without approval | No owner step | Any local process or person gets a session at once; refused by the owner's own condition |

## Decision

1. **Switch.** `sharing.signup` in reader.yaml takes `off` (default) or `open`; `false` is still read as `off`, so
   existing configs stay valid. Any other value is the startup error `signup_not_supported` (FR-AUTH-16, kept). The
   brief named `auth.signup`; `sharing.signup` is the key SRV-AUTH already reserves and validates, and SRV-CONFIG
   rejects an unknown top-level `auth` key, so reusing it needs no SRV-CONFIG change. While `off`, every sign-up route
   answers 404 exactly as today (FR-AUTH-13) and the sign-in page shows no link.
2. **Page and ceremony.** A public page `GET /api/auth/signup`, linked from the sign-in page ("New here? Create an
   account") only while `open`. The visitor chooses a **handle** and a **display name**, then creates a passkey:
   `POST /api/auth/signup/options {handle, display_name}` checks the rules and the limits and returns registration
   options for a fresh `webauthn_user_id`; `POST /api/auth/signup/verify` verifies through the same seam as bootstrap
   (FR-AUTH-15) and creates the user and its credential in one transaction. **No session is started.** The page
   shares passkey.js and its state table (FR-AUTH-20) and adds the sign-up states.
3. **Pending.** The new user has `role = learner` (fixed in code; the request carries no role and no route can create
   an owner), `source = signup` and `state = pending`. A pending user cannot sign in: sign-in verifies the passkey
   first and then answers 403 `pending_approval` without a session, so only the passkey's holder learns the state.
   No session row can exist for a pending user, and `session_user` also refuses any user whose state is not `active`.
4. **Approval.** An owner-only screen in the app, "Sign-ups" (WEB-ACCOUNTS), lists pending accounts with handle,
   display name and request time, and approves or rejects each. Approve sets `state = active`; reject deletes the
   pending user and its credential, so the handle is free again. Approved sign-up accounts can later be disabled
   from the same screen (sessions revoked, rows kept). Approve, reject and disable need a recent owner session
   (`recent_session`, 10 minutes, the rule add-passkey uses), because each changes who can use the instance.
   Notification is in-app only: the header shows "Sign-ups (n)" to the owner when n > 0. A Telegram notice waits for
   SRV-NOTIFY (an event `signup.pending`), because no spec has a server-side notification path today.
5. **Sync with reader.yaml.** `users.source` is `config` or `signup`. `sync_users` disables only `source = config`
   users missing from `users[]`; a configured handle never takes over a sign-up account: a `users[]` handle equal to a
   `signup` user's handle stops the api with `handle_taken_by_signup`. Without that rule, adding such a handle with a
   `providers` entry would hand the owner's provider token to a stranger's passkey (the proxy matches by handle).
6. **Abuse controls.**
   - Rate limits on `signup/options` (each call adds one row to `signup_attempts`, refused calls excepted):
     **global 30 per hour** and **per client 5 per hour**, answered 429 `rate_limited` with `Retry-After`, like
     FR-AUTH-11. Failed sign-up verifications add `auth_failures` rows and fall under FR-AUTH-11's global limit too.
   - The client key is the address Caddy saw, read from the last `X-Forwarded-For` entry only when the peer is in
     the `front` network (Caddy's), stored as SHA-256 with a per-process random salt, never logged.
     **Hypothesis, unmeasured:** behind Docker Desktop on macOS every host client reaches Caddy from one gateway
     address, so on the Mac the per-client limit acts as a second global limit. A spike (`spikes/signup-client-ip`,
     Caddy's `remote_ip` for two host clients) measures it before the per-client limit is relied on.
   - **Pending cap: 20.** When 20 accounts are pending, `signup/options` answers 429 `signup_full` (no Retry-After)
     until the owner decides. Unapproved accounts expire after **7 days** (deleted by the FR-AUTH-17 cleanup).
   - **Handle rules:** `^[a-z][a-z0-9-]{2,31}$` (3–32 ASCII characters, lower case only), no `--`, no trailing `-`.
     ASCII-only removes cross-script confusables; within ASCII a skeleton (`0`→`o`, `1`→`l`, `rn`→`m`, `vv`→`w`,
     hyphens removed; after UTS #39's confusable idea) must not equal the skeleton of any existing user's or reserved
     handle. A reserved list (owner, admin, root, system, support, reader, reader-ai, api, auth, signin, signup,
     bootstrap, mcp, live-check, …) and every `users[]` handle are unavailable.
   - **Display name:** 1–64 characters after NFC and trimming, no control or format characters (Unicode Cc, Cf:
     bidi overrides included). It is shown only to the owner, as text.
   - **Challenge expiry:** the sign-up ceremony uses the existing 300 s challenge (`challenge_ttl_s`), single use
     (INV-AUTH-1).
   - **Enumeration:** handles must be unique, so a sign-up form necessarily tells a visitor whether a handle is free.
     Trade-off taken: `signup/options` answers the same 409 `handle_unavailable` for taken, pending, reserved,
     confusable and invalid handles (the page checks the format before sending, so a real user sees the format
     message from the page, never from the server), and every probe costs one row under both rate limits (at most 5
     per client and 30 overall per hour). Display names, roles, states and request times are never revealed. Sign-in
     stays usernameless, so a known handle gives nothing to sign in with.
7. **Cost.** Approval grants an account, not spend. A sign-up user has no `users[]` entry, so the agent proxy has no
   provider token for it and every model call answers `provider_missing`; it has no kernel service either. If a
   provider or a kernel is ever granted to such a user, the existing per-user caps apply unchanged (`daily_cap_usd`
   for the tutor, `budgets.monthly_usd`, FR-AGENT-9/-12). Until SRV-SHARE (stage 8) shares courses, an approved
   learner signs in to an empty lesson list.

## Reachability and ADR-0012 (single Mac)

With `open`, anyone who can reach `https://localhost` can ask for an account. On the v1 stack that means:

- **Only the Mac itself.** The port is bound to `127.0.0.1`; other machines on the LAN cannot connect. "Anyone" is
  every person using a browser on this Mac (another macOS account included) and every local process.
- **Browsers on other sites cannot drive it.** A page on another origin sends its own `Origin` and gets 403 (FR-AUTH-12);
  DNS rebinding arrives with the attacker's origin and fails the same way; WebAuthn binds the passkey to RP ID
  `localhost`.
- **Local processes can.** Any process on the Mac can script the ceremony with a software authenticator, as the live
  checks do. The pending state and the owner's approval are the control; the limits keep the pending list readable.
- **Beyond localhost is out of scope here.** Serving a LAN name, a tunnel or a VPS changes the RP ID, which makes every
  existing passkey unusable (they are bound to `localhost`), and turns "anyone" into the internet. That needs its own
  ADR (ADR-0012's VPS revisit, ADR-0024's revisit). This ADR's `revisit_when` names it.

## Threats

| Threat | Control |
| --- | --- |
| Spam accounts | Pending until approved; pending cap 20; 7-day expiry; global and per-client limits; reject frees the row |
| Credential stuffing | Not applicable: no passwords; passkeys are origin-bound and user-verified (FR-AUTH-3); sign-in failures keep FR-AUTH-11 |
| Pending-account DoS | The cap stops new sign-ups, not sign-in; the owner's list stays at ≤ 20 rows. Filling the cap or the global limit blocks other sign-ups for up to an hour or until the owner rejects: accepted on localhost, revisit with the VPS |
| Lockout through FR-AUTH-11 | Failed sign-up verifications count in the shared failure limit, so a local process can block sign-in for an hour. Already true of sign-in today (SRV-AUTH owner note 3); unchanged, revisit with the VPS |
| CSRF / Origin | The sign-up and approval routes are non-GET `/api/*` routes: Origin and JSON Content-Type checked before any handler (FR-AUTH-12); session cookies are SameSite=Lax |
| Privilege escalation | Role is fixed to `learner` in code; approval cannot change it; configured handles cannot take over sign-up accounts (Decision 5) |
| Privacy of handles | Handles are probe-able at ≤ 5 per client per hour (Decision 6); the page tells people the handle is public to the owner and not their real name; display names are shown only to the owner; client addresses are stored only salted and hashed, deleted after the window |
| Cost exposure | No provider and no kernel for sign-up users (Decision 7); existing per-user caps if that ever changes |
| A stolen owner cookie approving an attacker | Approve, reject and disable need a recent session (≤ 10 minutes) |

## Rejected options — why

- **B. Invitation links.** The owner chose open sign-up on 2026-11-23. Kept for the record: it is ADR-0016's model
  for course roles and stays the stage-8 path for those; sign-up gives an account, not a course role.
- **C. CLI-only.** Stays available (`users[]` plus `reader bootstrap --handle`) and remains the only way to create an
  owner. It fails the owner's request: no sign-up page.
- **D. No approval.** Any local process would get a session at once; the owner made approval a condition.

## Consequences

- SRV-AUTH changes: a migration (users `source`, `state`, `handle_skeleton`; the `signup` challenge kind and its
  columns; a `signup_attempts` system table), new routes, the sign-up page and states, `sync_users`, `session_user`
  and sign-in verify. Drafted as proposed requirements FR-AUTH-23..32 and INV-AUTH-10..14.
- WEB-ACCOUNTS (new, draft): the owner's "Sign-ups" screen and header link.
- OPS-DOD: operations `auth.signup` and `auth.approve`, every cell `missing` until implemented.
- Harder: the auth surface grows by one public write path. Forbidden: a sign-up route that sets a role, starts a
  session, or runs while `sharing.signup` is `off`.

## Open questions (owner)

1. **Default.** Recommended: `off` in reader.example.yaml and in code; you set `open` when you want it. Or `open` by
   default?
2. **Approval.** Keep approval required (recommended; your condition), or allow `open` without approval?
3. **Handle rules.** 3–32 lower-case ASCII letters, digits and hyphens, the skeleton check and the reserved list
   above. Cyrillic handles are then impossible; display names may be in any script. Accept?
4. **Reachability.** Is sign-up meant for people on this Mac only, or do you plan a LAN name, tunnel or VPS? The latter
   needs a separate ADR and re-registration of every passkey (RP ID change).
5. **What an approved learner can do.** Today: sign in and see an empty list (no provider, no kernel, no shared
   courses until stage 8). Is that enough for now?
