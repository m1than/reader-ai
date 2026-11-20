---
id: ADR-0012
title: Self-hosted deployment — Docker Compose, Caddy, named volumes, passkey sign-in
status: proposed
date: 2026-11-20
verified_by: "OPS-DEPLOY: `reader up` on a clean machine reaches a healthy stack; e2e sign-in with a virtual WebAuthn authenticator; backup-restore drill reproduces a published lesson"
revisit_when: "Moving from the owner's Mac to a VPS (planned after load measurement), more than 20 active users, or a host that cannot run Docker Compose"
---

> Proposed amendment: [ADR-0024](0024-local-ca-trust-and-passkey-browsers.md) (2026-11-20) — `localhost` confirmed (Safari + Chromium via Apple Passwords, substitution accepted by the owner); trust with `-p ssl`, untrust by hash; `caddy_data` handled as a secret.

## Context

Owner decision, 2026-11-20: start as containers, set up volumes properly, configure through Caddy, serve any UI as a self-hosted service. The owner works on macOS; macOS, Windows and Linux must be supported.

## Decision

**Compose stack** (`deploy/compose.yaml`): `caddy`, `api`, `worker`, `mcp`, `kernel`, `postgres` (see docs/architecture.md). Images are multi-arch (amd64, arm64), so Apple Silicon runs natively.

| Volume | Content | Backed up |
| --- | --- | --- |
| `pgdata` | Postgres cluster | `pg_dump` by `reader backup` |
| `blobs` | Uploads, lesson revisions, artifacts | Yes, as a tarball |
| `caddy_data`, `caddy_config` | Certificates, ACME state | Yes (avoids ACME rate limits after restore) |
| `scratch` | Kernel working directory | No, tmpfs |

**Networks.** `edge` (caddy ↔ api, mcp), `core` (api, worker, mcp ↔ postgres), `sandbox` (api, worker ↔ kernel). The kernel joins only `sandbox` and has no route to `core` or the internet by default (ADR-0014).

**Caddy.** Automatic HTTPS for a public domain; its internal CA for `localhost` and `.local` names; security headers (CSP, HSTS, `frame-ancestors 'none'`); routes `/api/*` → api, `/mcp` → mcp, `/` → SPA files.

**Authentication.** Passkeys (WebAuthn) through `py_webauthn`; server-side sessions in Postgres with an HttpOnly, Secure, SameSite=Lax cookie. The first start prints a one-time bootstrap link that registers the owner's passkey. Others join by invitation link (ADR-0016). MCP clients authenticate with OAuth 2.1 per the MCP authorization specification, or with scoped personal access tokens for local agents (ADR-0015).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Compose + Caddy** | One file, runs on Docker Desktop and Docker Engine; automatic TLS | Single host |
| Kubernetes (Helm) | Scaling, rolling updates | A cluster for 1–20 users |
| Bare-metal processes (systemd) | No container runtime | No uniform path on macOS and Windows |
| Traefik / nginx instead of Caddy | Familiar | Owner chose Caddy; nginx needs certbot for TLS |

**Sign-in options:** passkeys (chosen), passwords plus TOTP, external OIDC (Google/GitHub). Passwords add a credential store to protect and a reset flow; OIDC makes a self-hosted instance depend on a third party. Passkeys need neither.

## Rejected options — why (numbers)

- **Kubernetes.** Control-plane overhead and Helm charts for a stack of 6 containers serving 1–20 users.
- **systemd processes.** Works on 1 of the 3 required operating systems.

## Resolved questions (owner, 2026-11-20)

1. v1 runs only on the owner's Mac. A VPS follows after load is measured on the Mac (job throughput, kernel memory per session, ingestion CPU time, tutor concurrency); the roadmap's last stage produces those numbers.

Mac-only consequences:

- TLS through Caddy's internal CA for `localhost`; `reader up` offers to trust the root certificate. Passkeys on `https://localhost` are verified in `spikes/passkey-localhost` in Safari and Chrome before OPS-DEPLOY is approved.
- No public URL: MCP for local agents uses personal access tokens over `localhost`; OAuth for remote MCP clients and Telegram webhooks switch on with the VPS.
- Containers run under Docker Desktop on Apple Silicon (arm64 images); the Apple GPU is not available inside containers, so model inference in ingestion runs on CPU (ADR-0008).
- Backups go to a folder the owner chooses (an external disk or a synced folder), set in `reader.yaml`.
