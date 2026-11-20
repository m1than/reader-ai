---
id: ADR-0024
title: Local CA trust and passkey browsers — localhost confirmed, TLS-only trust removed by hash, caddy_data as a secret
status: proposed
date: 2026-11-20
verified_by: "OPS-DEPLOY: `reader up` trusts the root with `-p ssl` and `reader down --untrust` removes it by SHA-1 with no orphaned trust setting (checked by trust-settings export); caddy_data backups are encrypted like other secrets"
revisit_when: "Caddy's internal CA supports name constraints; Google Chrome or another browser fails passkeys on https://localhost"
supersedes: []
---

## Context

ADR-0012 says TLS uses Caddy's internal CA for `localhost`, that `reader up` offers to trust the root, that `caddy_data` is backed up, and that passkeys are "verified in `spikes/passkey-localhost` in Safari and Chrome before OPS-DEPLOY is approved". The spike ran on 2026-11-20 (RESULT.md, PR #7, PR #11; deep-reviewer approved in round 2):

| Fact | Source |
| --- | --- |
| Safari 27.2 and Dia (Chromium 157): `register.ok` and `login.ok` on `https://localhost`, both through Apple Passwords (AAGUID `fbfc3007-…`), user verification true, sign count 0 | RESULT.md results table |
| Google Chrome would not launch on the owner's Mac; the owner accepted the substitution ("accept passkey substitution", 2026-11-20) | progress.yaml |
| Caddy's root has `CA:TRUE, pathlen:1` and no name constraints, so it can sign any host name. Its key lives in `caddy_data` | RESULT.md browser-free checks |
| `add-trusted-cert -r trustRoot` without `-p` trusts the root for every policy | RESULT.md owner steps |
| Deleting the certificate by name without `-t` left an orphaned trust setting, found by exporting the trust settings and searching for its SHA-1; removed with `remove-trusted-cert` | RESULT.md teardown, PR #11 |
| Every Caddy CA made in one year has the same name ("Caddy Local Authority - 2026 ECC Root") | RESULT.md teardown |

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **A. Keep `localhost` + Caddy internal CA; trust with `-p ssl`; untrust by hash; treat `caddy_data` and its backups as secrets** | The measured path; no `/etc/hosts` change | The owner's Mac trusts a CA that can sign any name while its key exists |
| B. `mkcert` for `reader.test` (ADR-0012's fallback) | Same trust model, another tool | Not needed: `localhost` passed |

## Decision

This amends ADR-0012's TLS consequence and its volume table:
- **Host name:** `localhost`, confirmed for stage 1. The passkey criterion is met by Safari and Chromium (Dia) via Apple Passwords, per the owner's acceptance; Google Chrome and Google Password Manager stay untested.
- **Trust:** `reader up` trusts the root with `-p ssl` (TLS only), selected by the certificate file. `reader down --untrust` runs `remove-trusted-cert`, then deletes the certificate by SHA-1 with `-t`, and verifies that no trust setting for that hash remains.
- **`caddy_data`** holds a private key that the owner's Mac trusts for any host name. Its backups are encrypted and access-controlled like other secrets (ADR-0011); a restore keeps the same CA, so no re-trust is needed.

## Rejected options — why

- **B.** Its trust model is the same, and `localhost` already passed. It stays ADR-0012's fallback if a browser fails on `localhost`.

## Consequences

- **SRV-AUTH** must close the spike server's gaps:
  - store the WebAuthn `user.id` and check `userHandle`;
  - bind the user to the registration flow;
  - per-ceremony, expiring, session-bound challenges;
  - accept a sign count of 0 from synced passkeys;
  - negative tests for a wrong challenge, a wrong origin and a replay.
- **Hypothesis:** Google Chrome with its own password manager behaves like Dia. It is measured when Chrome is available, by the `revisit_when` check.

## Open questions

- None for the owner.
