---
id: ADR-0024
title: Local CA and passkeys — localhost confirmed, TLS-only trust recorded and removed by hash, no backup of the internal CA key in Mac-only v1
status: proposed
date: 2026-11-20
verified_by: "OPS-DEPLOY: `reader up` trusts the root in the user domain (login keychain) with `-p ssl` and records the root and its SHA-1 on the host; `reader down --untrust` removes every recorded root by hash, and a trust-settings export shows none left; owner live check: passkey register and sign-in pass with TLS-only trust in Safari and a Chromium browser"
revisit_when: "Caddy's internal CA supports name constraints; a browser fails passkeys on https://localhost; reader-ai moves to a VPS with a public certificate"
supersedes: []
---

## Context

ADR-0012 says:
- TLS uses Caddy's internal CA for `localhost`, and `reader up` offers to trust the root;
- `caddy_data` is backed up "to avoid ACME rate limits after restore";
- passkeys are "verified in `spikes/passkey-localhost` in Safari and Chrome before OPS-DEPLOY is approved".

The spike ran on 2026-11-20 (RESULT.md on main, PRs #7 and #11; deep-reviewer approved in round 2):

| Fact | Source |
| --- | --- |
| Safari 27.2 and Dia (Chromium 157) passed `register.ok` and `login.ok` on `https://localhost` through Apple Passwords (AAGUID `fbfc3007-…`), user verification true. The trust used was `-r trustRoot` with no `-p` (all policies) | RESULT.md results table and owner steps |
| Whether Safari showed a certificate warning is unconfirmed; Chromium implies a trusted chain (WebAuthn worked) | RESULT.md |
| Google Chrome would not launch on the owner's Mac. The owner accepted the substitution (progress.yaml on main, PR #14) | progress.yaml |
| Caddy's root: `CA:TRUE, pathlen:1`, no name constraints, so it can sign any name. Its key is in `caddy_data` | RESULT.md browser-free checks |
| Deleting the certificate by name without `-t` left an orphaned trust setting (found by SHA-1 in a trust-settings export; removed with `remove-trusted-cert`) | RESULT.md teardown, PR #11 |
| Every Caddy CA made in one year has the same name | RESULT.md teardown |

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **A. Do not back up the internal CA key in Mac-only v1**; on restore, Caddy makes a new CA and the owner re-trusts once | No copy of an any-name signing key in backups | One re-trust per restore |
| B. Back up `caddy_data` encrypted, as ADR-0012 says | Restore keeps the CA | The key that the owner's Mac trusts for any name lives in every backup. ADR-0012's reason (ACME rate limits) does not apply to an internal CA |
| C. A name-constrained root for `localhost` | Limits the blast radius | Not considered in the spike, and unmeasured |

## Decision

This amends ADR-0012's TLS consequence and its volume table:
- **Host name:** `localhost`, confirmed for stage 1. The passkey criterion is met by Safari and Chromium (Dia) via Apple Passwords, per the owner's acceptance. Google Chrome and Google Password Manager stay untested.
- **Trust domain:** the user domain, the login keychain. `reader up` trusts the root with `-p ssl` (TLS only) and keeps a host copy of each trusted root together with its SHA-1, recorded at trust time.
- **Untrust:** `reader down --untrust` removes the trust and deletes the certificate by SHA-1 (with `-t`) for **every recorded root**, then verifies with a trust-settings export that none remains.
- **Backup (option A):** in Mac-only v1, the internal CA key in `caddy_data` is not backed up. A restore costs one re-trust.

## Rejected options — why

- **B.** ADR-0012's reason for backing up `caddy_data` (ACME rate limits) is about public certificates. For the internal CA, the backup only multiplies copies of a key the Mac trusts for any name.
- **C.** It is listed as unconsidered and unmeasured, not rejected on numbers.

## Consequences

- **Hypothesis:** passkeys work with `-p ssl` (TLS-only) trust; the spike used all-policy trust. Check: the OPS-DEPLOY owner live check in `verified_by`, which also records whether Safari shows a certificate warning.
- **SRV-AUTH requirements** found by the spike's server review are listed in `spikes/passkey-localhost/RESULT.md`, "Notes for OPS-DEPLOY and SRV-AUTH".

## Open questions

- None for the owner.
