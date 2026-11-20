---
id: ADR-0011
title: Declarative reader.yaml with secret references resolved per platform
status: proposed
date: 2026-11-20
verified_by: "SRV-CONFIG tests: schema rejects inline secret values; resolver tests per scheme; OPS-CLI smoke on macOS, Windows and Linux runners resolves a keychain reference"
revisit_when: "A deployment target cannot use any of the four reference schemes"
---

> Proposed amendment: [ADR-0023](0023-provider-token-delivery.md) (2026-11-20) — secrets reach only their holder: `/run/secrets`, or a per-run subprocess environment. Never service-level `environment:`, `env_file:` or `docker run -e`.
>
> Proposed amendment: [ADR-0026](0026-secret-delivery-read-only-services.md) (2026-11-20) — Compose refuses environment-sourced secrets on read-only services (spikes/compose-secrets-ro). Delivery becomes either a tmpfs `/run/secrets` filled by `reader up` over `docker exec` stdin (recommended) or a file-sourced Compose secret; the owner chooses.

## Context

Owner decisions, 2026-11-20: configuration in YAML; secrets may live in the container's `.env` and in the macOS Keychain; macOS, Windows and Linux must all work.

The macOS Keychain (Passwords app) stores passwords and keys as items. Passkeys are WebAuthn credentials for signing in; they cannot hold an API key. reader-ai uses the Keychain for secrets and passkeys for login (ADR-0012).

A container cannot reach the host's Keychain, Credential Locker or Secret Service. Keychain references must therefore be resolved on the host.

## Decision

**One file, `reader.yaml`**, validated by a JSON Schema generated from the Pydantic settings models (editor completion through the schema URL). It declares instance settings, users and their providers, models per task, budgets, kernels, materials with their limits, sectors, and sharing defaults. Secret values never appear in it; only references:

| Scheme | Resolved by | Typical use |
| --- | --- | --- |
| `secret://env/NAME` | host CLI, from the environment of the shell that runs `reader up`; inside containers, the server | Owner's shell profile, CI, quick local runs |
| `secret://file/run/secrets/NAME` | server, from a mounted file | Compose secrets, servers |
| `secret://hostfile/<absolute path>` | host CLI, from a file on the host; `reader doctor` refuses it unless the mode is 0600 and the file is outside every git work tree | A token kept in a private file on the owner's machine |
| `secret://keychain/<service>/<account>` | host CLI through `keyring`: macOS Keychain, Windows Credential Locker, Linux Secret Service (GNOME Keyring, KWallet) | Owner's laptop |
| `secret://op/<vault>/<item>/<field>` | host CLI through the 1Password CLI | Optional |

**Host CLI `reader`** (Python, installed with `uv tool install`): `reader up` reads `reader.yaml`, resolves `env`, `hostfile`, `keychain` and `op` references on the host, and starts `docker compose up` with the values only in that child process's environment. Compose maps them into containers as secrets (`secrets: <name>: environment: <VAR>`), which appear as files under `/run/secrets/`. Nothing is written to disk. `reader secrets set NAME` stores a value in the platform keyring.

**`.env` fallback** for headless Linux servers: `deploy/.env` (gitignored, mode 0600 checked by `reader doctor`), referenced through `secret://env/...`.

Example:

```yaml
version: 1
instance: { url: https://reader.local, data_dir: ./data }
users:
  - handle: ivan
    role: owner
    providers:
      - kind: anthropic-subscription
        token: secret://keychain/reader-ai/claude-oauth-token
tasks:
  draft:      { model: opus,   budget_usd_per_lesson: 2.0 }
  fact_check: { model: sonnet, budget_usd_per_lesson: 1.0 }
  review:     { model: opus,   budget_usd_per_lesson: 1.2 }
  tutor:      { model: sonnet }
budgets: { monthly_usd: 200, warn_at: 0.8 }
kernels: [python3]
materials:
  - id: bashir-2025
    file: ./materials/bashir-mastering-blockchain-ru-2025.pdf
    sha256: 61e8d4c390ba63c3f13efe0860d5042f8d1b19ba8f26b4f2e14e73478bc9e2b2
    license: personal-use
    language: ru
```

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **References + host-side resolution + Compose secrets** | One config file on every OS; secrets stay in the OS store until start | A host CLI to maintain |
| `.env` only | Simplest | Plain-text secrets on disk on the owner's laptop |
| Mount the keychain into the container | No host CLI | Impossible on macOS and Windows: Keychain and Credential Locker are not files a Linux container can open |
| Vault / Infisical service | Central secret management | One more service to run and unseal for 1–3 users |

## Rejected options — why (numbers)

- **`.env` only.** Leaves every provider token in a plain-text file; the owner explicitly wants the Keychain as an option.
- **Mounting the keychain.** Works on 0 of 2 non-Linux platforms the owner needs (macOS, Windows).
- **Vault.** Adds 1 stateful service with its own unseal procedure to protect 1–5 secrets.

## Consequences

- Redaction: the logger and the event serializer drop any value that came from a resolved secret (test fixture with a canary value).
- `reader doctor` checks file modes, unresolved references and keyring availability on the current OS.
