---
id: ADR-0031
title: Open self-service sign-up with owner approval
status: proposed
date: 2026-11-23
verified_by: "SRV-AUTH FR-AUTH-23..32 and INV-AUTH-10..15 ([db], [browser] and the [live] `signup` step run with `sharing.signup: \"open\"`); WEB-ACCOUNTS FR-ACCOUNTS-1..5"
revisit_when: "The stack listens on anything other than 127.0.0.1 (LAN name, tunnel or VPS): that ADR must add per-client limiting; more than 20 pending sign-ups in a week; the global sign-up limit is used up by anything other than the owner's live check; SRV-SHARE (stage 8) gives approved learners access to shared courses; SRV-NOTIFY exists"
supersedes: []
---

> Amends, if accepted: ADR-0017 "v1 foundation" ("Self-service sign-up exists behind a disabled flag; v1 stays
> invitation-only on the owner's Mac") and ADR-0012 "Authentication" ("Others join by invitation link"). Both are
> `proposed`. No accepted ADR is contradicted: ADR-0024 (localhost, RP ID) and ADR-0026 stay as they are.
>
> Revision 1 (2026-11-23), after review round 1 and the owner's answers: one global limit, no per-client limit and no
> client addresses; default `open`; a pending sign-up loses to a configured handle; sign-up failures in their own
> scope; wider display-name rules; the live check is a real oracle.

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
- Rate limiting (FR-AUTH-11) is **one global counter** of failed ceremonies (100 per hour) in `auth_failures`. The api
  runs with `proxy_headers=False` behind Caddy on `127.0.0.1`, so every client looks the same to it.
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
| **A. Open sign-up, pending until the owner approves (chosen by the owner)** | Anyone at the server can ask for an account without the owner's terminal; nothing is granted until approval | A public ceremony that writes rows; a global limit, a pending cap and handle rules to keep it bounded; one owner screen |
| B. Invitation links (ADR-0016's model) | Only invited people can register; no public write path; role and expiry carried by the link | The owner makes a link per person and sends it; rejected by the owner on 2026-11-23 |
| C. CLI-only bootstrap (today: `users[]` plus `reader bootstrap --handle`) | No public write path at all | The owner edits reader.yaml, restarts and runs a command per person |
| D. Open sign-up without approval | No owner step | Any local process or person gets a session at once; refused by the owner's own condition |

## Decision

1. **Switch.** `sharing.signup` in reader.yaml takes `"open"` (the default when absent, owner 2026-11-23) or `"off"`;
   `false` is read as `"off"`. Any other value stops the api with `signup_not_supported` (FR-AUTH-16, kept). The brief
   named `auth.signup`; `sharing.signup` is the key SRV-AUTH already reserves and validates, and SRV-CONFIG rejects an
   unknown top-level `auth` key, so reusing it needs no SRV-CONFIG change. The example config quotes both values.
   Because the default is `open`, upgrading an instance whose config lacks the key turns sign-up on; the owner chose
   this. While `"off"`, every sign-up route answers 404 exactly as today (FR-AUTH-13) and the sign-in page shows no
   link.
2. **Page and ceremony.** A public page `GET /api/auth/signup`, linked from the sign-in page ("New here? Create an
   account") while `open`. The visitor chooses a **handle** and a **display name**, then creates a passkey:
   `POST /api/auth/signup/options {handle, display_name}` checks the rules and the limits and returns registration
   options for a fresh `webauthn_user_id`; `POST /api/auth/signup/verify` verifies through the same seam as bootstrap
   (FR-AUTH-15) before taking any lock, then creates the user and its credential in one transaction under a global
   sign-up lock. **No session is started.** The page shares passkey.js and its state table (FR-AUTH-20) and adds the
   sign-up states.
3. **Pending.** The new user has `role = learner` (fixed in code; the request carries no role and no route can create
   an owner), `source = signup` and `state = pending`. A pending user cannot sign in: sign-in verifies the passkey
   first and then answers 403 `pending_approval` without a session, so only the passkey's holder learns the state.
   No session row can exist for a pending user, and `session_user` also refuses any user whose state is not `active`.
   SRV-MCP (stage 7) must apply the same rule to every MCP authentication path.
4. **Approval.** An owner-only screen in the app, "Sign-ups" (WEB-ACCOUNTS), lists pending accounts with handle,
   display name and request time, and approves or rejects each. Approve sets `state = active`; reject deletes the
   pending user and its credential, so the handle is free again. Approved sign-up accounts can be disabled from the
   same screen, in one transaction that also revokes their sessions; **disable cannot be undone in the product** (the
   screen says so). Each action locks the user row and re-checks its state. Approve, reject and disable need a recent
   owner session (`recent_session`, 10 minutes, the rule add-passkey uses), because each changes who can use the
   instance. Notification is in-app only: the header shows "Sign-ups (n)" to the owner when n > 0. A Telegram notice
   waits for SRV-NOTIFY (an event `signup.pending`), because no spec has a server-side notification path today.
5. **Sync with reader.yaml.** `users.source` is `config` or `signup`. `sync_users` disables only `source = config`
   users missing from `users[]`, and a configured handle never takes over a sign-up account. When a `users[]` handle
   equals a sign-up handle exactly:
   - a **pending** sign-up row loses: it and its credential are deleted inside the sync transaction, and the
     configured user is created;
   - an **approved or disabled** sign-up row stops the api with `reader-server: handle_taken_by_signup <handle>`. The
     line names the handle so the owner can fix reader.yaml or delete the account (SRV-AUTH owner note 5).

   Without this rule, a configured handle with a `providers` entry would hand the owner's provider token to a
   stranger's passkey (the proxy matches by handle). The check is exact-handle only.
6. **Abuse controls.**
   - **One global limit** on `signup/options`: 60 calls per hour across all clients (each call that passes adds one
     `signup_attempts` row), answered 429 `rate_limited` with `Retry-After`, like FR-AUTH-11. There is **no
     per-client limit**: on `127.0.0.1` every client looks the same to the api, so a per-client key cannot work. The
     api reads and stores no client address. Per-client limiting moves to the future VPS ADR.
   - **Any local process can close sign-up for everyone for up to an hour** by using up the global limit (60 calls take
     seconds). No global number prevents that. Accepted on localhost as a residual (below).
   - **Separate failure scope.** `auth_failures` gains `scope` (`auth` or `signup`). Failed sign-up verifications
     count only toward a sign-up failure limit (100 per hour); FR-AUTH-11 counts only `auth`. Sign-up traffic can
     never lock anyone out of sign-in.
   - **Pending cap: 20.** When 20 accounts are pending, `signup/options` answers 429 `signup_full` (no Retry-After)
     until the owner decides. Unapproved accounts expire after **7 days**. The FR-AUTH-17 cleanup deletes them; it
     locks each row and skips one that an approval holds.
   - **Handle rules** (owner, 2026-11-23: lower-case Latin only):
     - `^[a-z][a-z0-9-]{2,31}$`, no `--`, no trailing `-`. Every accepted handle also fits OPS-CLI's kernel-service
       rule. ASCII-only removes cross-script lookalikes.
     - Within ASCII, a skeleton must not equal the skeleton of any existing user's or reserved handle. It is computed
       in order: lower-case; remove hyphens; `rn`→`m` and `vv`→`w`, left to right; `0`→`o` and `1`→`l`.
     - The skeleton is unique among sign-up rows only, so two configured handles may share one.
     - A reserved list (owner, admin, root, system, support, reader, reader-ai, api, auth, signin, signup, bootstrap,
       mcp, live-check, …) and every `users[]` handle are unavailable.
   - **Display name:** 1–64 Unicode code points after NFC and trimming. No code point of category Cc, Cf (bidi
     overrides included), Cs, Co, Cn, Zl or Zp. It is shown only to the owner, as text.
   - **Challenge expiry:** the sign-up ceremony uses the existing 300 s challenge (`challenge_ttl_s`), single use
     (INV-AUTH-1).
   - **Enumeration:** handles must be unique, so a sign-up form necessarily tells a visitor whether a handle is free.
     - `signup/options` answers the same 409 `handle_unavailable` for taken, pending, reserved, lookalike and invalid
       handles (the page checks the format before sending).
     - Probing is bounded only by the global limit: at most 60 probes per hour **for all clients together**, and a
       prober who spends them also closes sign-up for everyone else for that hour.
     - Display names, roles, states and request times are never revealed. Sign-in stays usernameless, so a known
       handle gives nothing to sign in with.
7. **Cost.** Approval grants an account, not spend.
   - A sign-up user has no `users[]` entry, so the agent proxy has no provider token for it: every model call answers
     `provider_missing`. It has no kernel service either.
   - If a provider or a kernel is ever granted to such a user, the existing per-user caps apply unchanged
     (`daily_cap_usd` for the tutor, `budgets.monthly_usd`, FR-AGENT-9/-12).
   - Until SRV-SHARE (stage 8) shares courses, an approved learner signs in to an empty lesson list (accepted by the
     owner, 2026-11-23).

## Reachability and ADR-0012 (single Mac)

With `open`, anyone who can reach `https://localhost` can ask for an account. On the v1 stack that means (owner,
2026-11-23: this Mac only, for now):

- **Only the Mac itself.** The port is bound to `127.0.0.1`; other machines on the LAN cannot connect. "Anyone" is
  every person using a browser on this Mac (another macOS account included) and every local process.
- **Browsers on other sites cannot drive it.** A page on another origin sends its own `Origin` and gets 403
  (FR-AUTH-12). DNS rebinding arrives with the attacker's origin and fails the same way. WebAuthn binds the passkey to
  RP ID `localhost`.
- **Local processes can.** Any process on the Mac can script the ceremony with a software authenticator, as the live
  checks do. The pending state and the owner's approval are the control. The global limit and the cap keep the
  pending list readable; they do not keep a determined local process out.
- **Beyond localhost is out of scope here.** Serving a LAN name, a tunnel or a VPS would:
  - change the RP ID, which makes every existing passkey unusable (they are bound to `localhost`);
  - turn "anyone" into the internet;
  - need per-client limiting.

  That needs its own ADR (ADR-0012's VPS revisit, ADR-0024's revisit), named in `revisit_when`.

## Threats

| Threat | Control |
| --- | --- |
| Spam accounts | Pending until approved; pending cap 20; 7-day expiry; one global limit; reject frees the row |
| Credential stuffing | Not applicable: no passwords; passkeys are origin-bound and user-verified (FR-AUTH-3); sign-in failures keep FR-AUTH-11 |
| Pending-account DoS | The cap stops new sign-ups, not sign-in; the owner's list stays at ≤ 20 rows. Residual, below |
| Sign-up closed by a local process | Any local process can use up the global limit in seconds and close sign-up for up to an hour. Residual, below |
| Lockout of sign-in through failures | Removed: sign-up failures have their own scope and never count toward FR-AUTH-11. Sign-in's own lever (SRV-AUTH owner note 3) is unchanged |
| CSRF / Origin | The sign-up and approval routes are non-GET `/api/*` routes: Origin and JSON Content-Type checked before any handler (FR-AUTH-12); session cookies are SameSite=Lax |
| Privilege escalation | Role fixed to `learner` in code; approval cannot change it; configured handles never adopt sign-up accounts (Decision 5); review round 1 found no escalation path |
| Privacy of handles | Probing is bounded by the global limit only (≤ 60 per hour for everyone together); the page tells people the owner sees the handle and it need not be their name; display names are shown only to the owner; no client address is read, stored or logged |
| Cost exposure | No provider and no kernel for sign-up users (Decision 7); existing per-user caps if that ever changes |
| A stolen owner cookie approving an attacker | Approve, reject and disable need a recent session (≤ 10 minutes) |
| Lost passkey | No recovery for sign-up users; the owner disables the account and the person signs up again under another handle |
| Passkey left behind | A rejected or expired sign-up leaves its passkey on the device; it names a deleted account and signs in to nothing |

**Residuals accepted on localhost** (revisit with the VPS ADR): any local process can (1) close sign-up for everyone
for up to an hour through the global limit and (2) fill the pending cap until the owner rejects. Both block only new
sign-ups, never sign-in or existing accounts.

## Rejected options — why

- **B. Invitation links.** The owner chose open sign-up on 2026-11-23. Kept for the record: it is ADR-0016's model
  for course roles and stays the stage-8 path for those; sign-up gives an account, not a course role.
- **C. CLI-only.** Stays available (`users[]` plus `reader bootstrap --handle`) and remains the only way to create an
  owner. It fails the owner's request: no sign-up page.
- **D. No approval.** Any local process would get a session at once; the owner made approval a condition.
- **A per-client limit on the Mac.** Behind `127.0.0.1` every client looks the same to the api, so a per-client key
  would only be a second global limit, plus header parsing and stored addresses for no gain. Deferred to the VPS ADR.

## Consequences

- SRV-AUTH changes, drafted as proposed requirements FR-AUTH-23..32 and INV-AUTH-10..15:
  - a migration: users `source`, `state` and `handle_skeleton` (with a sign-up-only unique index); the `signup`
    challenge kind and its columns; `auth_failures.scope`; a `signup_attempts` system table;
  - new routes, and the sign-up page with its states;
  - changes to `sync_users`, `session_user` and sign-in verify.
- WEB-ACCOUNTS (new, draft): the owner's "Sign-ups" screen and header link.
- OPS-DOD: operations `auth.signup` and `auth.approve`, every cell `missing` until implemented. Their evidence comes
  only from a live run with sign-up `open`; SKIP lines are not evidence.
- Harder: the auth surface grows by one public write path. Forbidden: a sign-up route that sets a role, starts a
  session, or runs while `sharing.signup` is `"off"`.

## Resolved questions (owner, 2026-11-23)

1. **Default:** `open` (the draft recommended `off`; the owner chose `open`).
2. **Approval:** required.
3. **Handles:** lower-case Latin only, with the lookalike check and the reserved list.
4. **Reachability:** this Mac only, for now.
5. **An approved learner before stage 8** signs in to an empty lesson list: acceptable.

## Open questions

- None for the owner.
