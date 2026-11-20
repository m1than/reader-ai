# Spike: passkey-localhost — RESULT

**Question (docs/roadmap.md 0.3):** Do passkeys register and sign in on `https://localhost` behind Caddy's internal CA?
**Pass criterion:** Safari and Chrome on the owner's Mac both register and authenticate.
**Fallback if it fails:** an `mkcert` certificate for `reader.test` in `/etc/hosts`.
**Verdict: PASS for Safari 27.2 and Chromium 157 (Dia), both through Apple Passwords (iCloud Keychain). The roadmap criterion as written ("Safari and Chrome", also ADR-0012) stays open** until the owner accepts the substitution or reruns in Google Chrome:
- Google Chrome would not launch on the owner's Mac. `open -a "Google Chrome"` reported "Unable to find application". `/Applications/Google Chrome.app` exists, but Spotlight reports no bundle identifier for it; not investigated further.
- The Chromium run used Dia. The browser name comes from the owner's launch command (`open -a Dia`); the log shows only the user agent `Chrome/157.0.0.0`.
- Both registrations carry AAGUID `fbfc3007-154e-4ecc-8c0b-6e020557d7bd`, which is "Apple Passwords" in the passkey AAGUID list (github.com/passkeydeveloper/passkey-authenticator-aaguids, `aaguid.json`). Both browsers used the same macOS platform authenticator, and Chrome's own provider (Google Password Manager) was not exercised.

The host name for stage 1 is provisionally `localhost`; the `mkcert` fallback was not needed in this run.

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
| Root CA exported from the Caddy container | PASS. `CN=Caddy Local Authority - 2026 ECC Root`, `CA:TRUE, pathlen:1`, no name constraints, valid to 2036-09-28. Every `check.sh` run after `down -v` makes a new CA. The one the owner trusted (exported by the owner's run) has SHA-256 `9D:93:3D:94:13:50:D0:3A:C1:07:40:09:D7:52:E1:BB:4F:0F:FF:3A:23:9B:30:00:68:E6:19:DA:ED:E1:F7:0A` and SHA-1 `9D:06:0D:B2:0A:A4:89:FE:A1:BF:56:54:F5:80:C5:B5:90:CC:38:CB` |
| TLS verifies against that root only (`curl --cacert`) | PASS |
| TLS fails without it (not publicly trusted) | PASS |
| Leaf certificate SAN covers `DNS:localhost` | PASS |
| Page served through Caddy | PASS |
| Registration options: `rp.id = localhost`; `residentKey` and `userVerification` = `required`; challenge ≥ 32 bytes; ES256 (-7) offered | PASS |
| Authentication options: `rpId = localhost`, UV required, no `allowCredentials` (discoverable sign-in) | PASS |
| A malformed registration is rejected and logged (`register.fail`, "Could not parse registration credential"). It fails at JSON parsing, before any challenge, origin or signature check; no negative test covers a wrong challenge, a wrong origin or a replay | PASS |

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
   security add-trusted-cert -r trustRoot -p ssl -k ~/Library/Keychains/login.keychain-db data/spikes/passkey-localhost/caddy-root.crt
   ```
   macOS asks for your password. Safari and Chrome both use the macOS trust store, so one command covers both.
   **What this trusts:** a root CA with no name constraints. Its private key sits in the `caddy_data` Docker volume, and anyone who can read that volume can mint a certificate for **any** host name that your login then trusts. `-p ssl` limits the trust to TLS; without it, macOS trusts the root for every policy (code signing, S/MIME and others). The trust lasts until it is removed (see Teardown). The owner's run on 2026-11-20 used the command without `-p ssl`.
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

The owner ran steps 1–4 through Claude Code's `!` prompt. The table comes from `data/spikes/passkey-localhost/log.jsonl` (gitignored): the events with a browser user agent, in order. The owner did not report the certificate warning or the authenticator dialog in words.
- **Chromium:** a trusted chain is implied, because Chromium refuses WebAuthn on a page with a certificate error, and `register.ok` happened.
- **Safari:** it is unconfirmed whether Safari showed or bypassed a warning.
- **Authenticator:** identified from the AAGUID, not from the dialog.

| Browser (user agent) | Register | Sign in | Authenticator (AAGUID) | Credential flags | Errors |
| --- | --- | --- | --- | --- | --- |
| Safari (`Version/27.2 Safari/605.1.15`) | `register.ok` | `login.ok` | Apple Passwords | `MULTI_DEVICE`, backed up, UV true, sign count 0 | none |
| Dia, Chromium (`Chrome/157.0.0.0 Safari/537.36`) | `register.ok` | `login.ok` | Apple Passwords | `MULTI_DEVICE`, backed up, UV true, sign count 0 | none |

- Each `login.ok` is for the credential id registered seconds earlier by the same user agent.
- The server checks a single-use challenge, the expected origin `https://localhost` and RP ID `localhost`, user verification, and the signature against the stored public key (`app/main.py`).
- Not tested: Google Chrome itself and its Google Password Manager provider; Firefox.

## Teardown

```bash
docker compose -f spikes/passkey-localhost/compose.yaml down -v
security remove-trusted-cert data/spikes/passkey-localhost/caddy-root.crt
security delete-certificate -t -Z "$(openssl x509 -in data/spikes/passkey-localhost/caddy-root.crt -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)" ~/Library/Keychains/login.keychain-db
```

If the second command succeeds, the third may report that the certificate is not found; that is expected. The first command removes the stack and Caddy's CA volume, and with it the CA's private key. The second removes the trust setting you added in step 2. The third deletes the certificate by its SHA-1 hash, with `-t` for any trust left. Every CA from 2026 has the same name, so select by hash, not by name. Both are the owner's commands, like the trust itself.

**State after the owner's run (2026-11-20):**
- The owner ran `down -v` and `security delete-certificate -c "Caddy Local Authority - 2026 ECC Root"`. That was the earlier, weaker instruction: no `-t`, selected by name.
- The agent then checked by name: `security dump-trust-settings` lists no Caddy entry, and `security find-certificate -a -c "Caddy Local Authority"` finds none in the login keychain.
- The name check misses orphans: `dump-trust-settings` resolves names through the certificate, which was already deleted. A stricter check, prompted by the review, found the leftover: `security trust-settings-export` (user domain) still contained the root's SHA-1 `9D060DB20AA489FEA1BF5654F580C5B590CC38CB`. That is an orphaned trust setting, left because `delete-certificate` ran without `-t`.
- Risk: none in practice, because the CA's private key was destroyed with the volume, so nothing can be signed under that root again.
- Removal is the owner's action: `security remove-trusted-cert data/spikes/passkey-localhost/caddy-root.crt`, then repeat the export check.
- Owner's note: the agent ran these listing commands. They read trust settings and certificate names, no passwords or secrets. AGENTS.md says the agent never reads Keychain items, so whether such checks are allowed is the owner's call. Passkeys created for `localhost` stay in iCloud Keychain or the Chrome profile. Delete them in System Settings → Passwords, or in Chrome → Password Manager, if you want.

## Notes for OPS-DEPLOY and SRV-AUTH

- `down -v` regenerates the CA, so every new CA needs trusting again. `reader up` should keep `caddy_data` (ADR-0012 already backs it up) and offer the trust step once.
- The origin must match exactly: `https://localhost` on 443, or `https://localhost:<port>` otherwise. The app reads it from `READER_ORIGIN`.
- The trust step is a one-time owner action per CA. Chromium needed nothing beyond the macOS login-keychain trust (WebAuthn worked, so the chain was trusted). For Safari, whether a certificate warning appeared is unconfirmed.
- Teardown must remove the trust **by hash, with `-t`**: deleting the certificate by name leaves an orphaned trust setting that `dump-trust-settings` no longer shows by name (seen in the owner's run).
- **The trusted root can sign any host name.** `reader up` keeps and backs up `caddy_data` (ADR-0012), so whoever can read that volume or its backups can mint certificates the owner's Mac trusts. OPS-DEPLOY must:
  - offer the trust step with `-p ssl`;
  - protect `caddy_data` and its backups like a secret;
  - provide an untrust command that works by hash;
  - consider a CA with name constraints for `localhost`, if Caddy supports it.
- **For SRV-AUTH**, gaps of this spike's server that the product must close:
  - Store the WebAuthn `user.id` and check `response.userHandle` on discoverable sign-in.
  - Bind the user to the registration flow created at `/register/options`. Here, the name in `/register/verify` is trusted.
  - Make challenges per ceremony, expiring and session-bound.
  - Accept a signature counter of 0, which is what synced passkeys report. This gives no clone detection.
  - Add negative tests for a wrong challenge, a wrong origin and a replay.
- `log.jsonl` events without a user agent (curl) break a naive `jq` `capture` on `.ua`. Use `(.ua // "")`.
