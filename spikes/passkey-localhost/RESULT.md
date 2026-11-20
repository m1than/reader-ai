# Spike: passkey-localhost — RESULT

**Question (docs/roadmap.md 0.3):** Do passkeys register and sign in on `https://localhost` behind Caddy's internal CA?
**Pass criterion:** Safari and Chrome on the owner's Mac both register and authenticate.
**Fallback if it fails:** an `mkcert` certificate for `reader.test` in `/etc/hosts`.
**Verdict: PASS, with one substitution.** Safari 27.2 and a Chromium browser both registered and signed in on `https://localhost` behind Caddy's internal CA, once the owner trusted that CA. The Chromium browser was **Dia** (user agent `Chrome/157.0.0.0`), not Google Chrome: Google Chrome would not launch on the owner's Mac (`open -a "Google Chrome"`: "Unable to find application"; `/Applications/Google Chrome.app` exists, but Spotlight reports no bundle identifier for it, so Launch Services does not resolve it by name; not investigated further). The `mkcert` fallback is not needed. Host name for stage 1: `localhost`.

## What is here

| File | Role |
| --- | --- |
| `compose.yaml` | `caddy` (2.11.9-alpine, `tls internal`) published on `127.0.0.1:${PORT:-443}` only, in front of `app` |
| `Caddyfile` | `localhost { tls internal; reverse_proxy app:8000 }` |
| `app/` | py_webauthn 3.0.1 (ADR-0012) + FastAPI 0.165.0 on `python:3.14-slim-trixie`, non-root. RP ID `localhost`, origin `https://localhost`. Discoverable credentials (`residentKey: required`), user verification required on register and sign-in, in-memory storage. One page with Register and Sign in, using the browser's `PublicKeyCredential.parse*OptionsFromJSON` and `toJSON()` |
| `check.sh` | Brings the stack up, exports Caddy's root CA to `data/spikes/passkey-localhost/caddy-root.crt` and runs the browser-free checks below. Leaves the stack running |
| `data/spikes/passkey-localhost/log.jsonl` (gitignored) | One JSON line per step: `register.options`, `register.ok` / `register.fail`, `login.options`, `login.ok` / `login.fail`, with the error, AAGUID, attestation format, device type (single/multi-device), backup state, UV flag and user agent |

## Browser-free checks (agent, 2026-11-20)

`bash spikes/passkey-localhost/check.sh` → **PASS: 12 of 12**

| Check | Result |
| --- | --- |
| Root CA exported from the Caddy container | PASS. `CN=Caddy Local Authority - 2026 ECC Root`, valid to 2036-09-28, SHA-256 `74:B7:7B:74:…:CA:04:72` (this CA instance; it is regenerated after `down -v`) |
| TLS verifies against that root only (`curl --cacert`) | PASS |
| TLS fails without it (not publicly trusted) | PASS |
| Leaf certificate SAN covers `DNS:localhost` | PASS |
| Page served through Caddy | PASS |
| Registration options: `rp.id = localhost`; `residentKey` and `userVerification` = `required`; challenge ≥ 32 bytes; ES256 (-7) offered | PASS |
| Authentication options: `rpId = localhost`, UV required, no `allowCredentials` (discoverable sign-in) | PASS |
| A forged registration is rejected and logged (`register.fail`, `InvalidRegistrationResponse`) | PASS |

Not verifiable without a browser: whether Safari and Chrome accept `localhost` as a secure context for WebAuthn behind a certificate they trust, and which authenticator each offers.

## Owner's part (Safari and Chrome, about 5 minutes)

From the repository root:

1. **Start the stack and export a fresh CA:**
   ```bash
   bash spikes/passkey-localhost/check.sh
   ```
   It should end with `PASS: 12 of 12 checks`. If port 443 is taken, prefix `PORT=8443` and use `https://localhost:8443` below.
2. **Trust Caddy's root CA in your login Keychain.** This is your action; the agent does not change trust stores:
   ```bash
   security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db data/spikes/passkey-localhost/caddy-root.crt
   ```
   macOS asks for your password. Safari and Chrome both use the macOS trust store, so one command covers both. It trusts only this Caddy instance's root, which signs certificates for `localhost`.
3. **Safari:** open `https://localhost`. There must be no certificate warning; if there is one, the CA is not trusted yet. The page shows "JSON helpers available".
   - Click **Register**, and confirm with Touch ID. The page shows `"ok": true` with `"backed_up"` and `"device_type"`.
   - Click **Sign in**, pick the passkey, and confirm with Touch ID. The page shows `"ok": true` and your user name.
4. **Chrome:** the same at `https://localhost`. Chrome may offer "iCloud Keychain" or "Chrome profile"; note which you chose.
   - A passkey from step 3 (iCloud Keychain) may already be offered at Sign in. Register a second one in Chrome as well, so both browsers register.
5. **Read back the log:**
   ```bash
   jq -c '{event, ua: (.ua | capture("(?<b>Chrome|Version/[0-9.]+ Safari)").b), backed_up, device_type, uv, error}' data/spikes/passkey-localhost/log.jsonl
   ```
6. **Fill in the table below**, or paste the log output to the agent.

### Results (owner's run, 2026-11-20)

The owner ran steps 1–4 through Claude Code's `!` prompt. The table comes from `data/spikes/passkey-localhost/log.jsonl` (gitignored): the events with a browser user agent, in order. The certificate-warning check and the authenticator dialog were not reported in words. Both are inferred: the page loaded without a TLS failure in either browser (the requests reached the app), and the credential flags are shown below.

| Browser (user agent) | Register | Sign in | Credential flags (from the verified response) | Errors |
| --- | --- | --- | --- | --- |
| Safari (`Version/27.2 Safari/605.1.15`) | `register.ok` | `login.ok` | `MULTI_DEVICE`, backed up, UV true | none |
| Dia, Chromium (`Chrome/157.0.0.0 Safari/537.36`) | `register.ok` | `login.ok` | `MULTI_DEVICE`, backed up, UV true | none |

- Both credentials are multi-device and backed up, which points to a synced passkey provider (iCloud Keychain or the browser's own sync). Which provider each browser offered was not recorded.
- User verification was true on every registration and sign-in, as required.
- Not tested: Google Chrome itself (see the verdict), and Firefox.

## Teardown

```bash
docker compose -f spikes/passkey-localhost/compose.yaml down -v
security delete-certificate -c "Caddy Local Authority - 2026 ECC Root" ~/Library/Keychains/login.keychain-db
```

The first command removes the stack and Caddy's CA volume. The second removes the trust you added in step 2; it is the owner's command, like the trust itself. Passkeys created for `localhost` stay in iCloud Keychain or the Chrome profile. Delete them in System Settings → Passwords, or in Chrome → Password Manager, if you want.

## Notes for OPS-DEPLOY and SRV-AUTH

- `down -v` regenerates the CA, so every new CA needs trusting again. `reader up` should keep `caddy_data` (ADR-0012 already backs it up) and offer the trust step once.
- The origin must match exactly: `https://localhost` on 443, or `https://localhost:<port>` otherwise. The app reads it from `READER_ORIGIN`.
- The trust step is a one-time owner action per CA. The browsers needed nothing beyond the macOS login-keychain trust.
- `log.jsonl` events without a user agent (curl) break a naive `jq` `capture` on `.ua`. Use `(.ua // "")`.
