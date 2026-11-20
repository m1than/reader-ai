# Spike: library-publish — RESULT

**Question (docs/roadmap.md 0.3):** Publish `ui-builder-lib-with-agents` (from `web3/material-parity`) to GitHub Packages through a release workflow in its repository that runs its gates; consume the version in Vite and Storybook 10; apply a draft Calm token file.
**Measure:** release time; bundle size of the 20 components reader-ai needs; contrast of Calm.
**Pass criterion:** the package installs by version; Calm passes 4.5:1 and 3:1.
**Fallback:** consume a built commit until the release workflow is green.

**Verdict: PARTIAL, waiting for the owner.**
- The release workflow is written and committed on a local library branch, but not pushed, tagged or run. The library's AGENTS.md forbids an agent to push or publish. So "installs by version" and the release time are **not measured**.
- The fallback works: a tarball of a built commit installs into a Vite 8 + React 19 + Storybook 10 consumer on Node 26. All 20 components typecheck and render in Chromium with no page errors.
- The draft Calm theme passes all 218 contrast pairs. That includes the tinted backgrounds the library composes (soft buttons, hover overlays, toasts, chips), after the tokens were adjusted. Before the adjustment, 20 of them failed.

Date: 2026-11-20. Deep-reviewer round 1 (`VERDICT: changes`) is addressed in this revision.

## Environment

| Item | Value |
| --- | --- |
| Host | Apple M5 Max, macOS 27.2; a Docling measurement was running on the Docker VM at the same time (host CPU shared) |
| Library | `/Users/m1than/Developer/ui-builder-lib-with-agents`, base `web3/material-parity` at `acc9e4af` (2026-10-29); package `@acme/ui` 0.0.0, `private: true`, 89 entries, runtime dependency `@floating-ui/react-dom`, peers React 19 |
| Library branch | `release/github-packages`: `6355fe67` (workflow + record 0045), then `dbb932cb` (review fixes). Local only |
| Tarball | `npm run build` + `npm pack` → `data/spikes/library-publish/acme-ui-0.0.0.tgz` (gitignored), 353,801 bytes, 437 files, sha256 `14425dfe1e426384b19fd03339946e7c43a9bfbf3e4908059350921e4e74afca` |
| Consumer runtime | **Node 26.13.0 from `/opt/homebrew/opt/node@26/bin`** (`PATH` prefixed) for install, typecheck, Vite, Storybook and `calm.mjs`. Vite 8.3.8, React 19.3.1, TypeScript 7.0.2, Storybook 10.6.8 (`@storybook/react-vite`), `@vitejs/plugin-react` 6.3.0 |
| Library size gate runtime | Node 22.22.1 (`~/.nvm/versions/node/v22.22.1/bin/node`), the library's CI runtime; see finding 6 |

## 1. Release workflow (library repository, local branch)

`.github/workflows/release.yml` plus decision record `docs/decisions/0045-release-to-github-packages.md` (indexed). After the review fixes, `npm run check` passes (43 records) and `actionlint` reports no findings.

| Concern | Design |
| --- | --- |
| Trigger | A pushed tag `v<semver>`. A person pushes the tag; agents never do (library AGENTS.md) |
| Tag check | The first step of `gates`, a regex `^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$`, so a bad tag fails in seconds |
| Gates | Job `gates`: `npm run gates` (every gate CI runs: static checks, every test file, sizes) on the tagged commit, with the CI browser (Chromium 1194, Playwright 1.56.1). `publish` needs it. Its 90-minute timeout is a guess: CI splits the same tests into 6 shards, and one job's duration is unmeasured (record 0045) |
| **Scope** | GitHub Packages accepts only the owner's scope, and the source keeps the template name `@acme/ui` (`init.mjs` renames it once per new project). The publish job lowercases the owner once and uses that value for setup-node's `scope` and the package name. Then, in its own checkout only, `npm pkg set` sets `name=@<owner>/ui`, `version=<tag without v>`, `repository.url=git+https://github.com/<owner>/<repo>.git`, `publishConfig.registry=https://npm.pkg.github.com` and `private=false`. Nothing is committed back. This is safe because no source file imports the package by name (`grep -rn "from '@acme/ui" src`: 0 hits), so `dist/` is the same bytes under either name |
| Dist-tag | `npm publish --tag next` when the version contains `-` (prerelease), else `--tag latest`. The tag value is not interpolated into the command; it only selects one of two literals |
| Credentials | Built-in `GITHUB_TOKEN`, job permissions `contents: read`, `packages: write`; `persist-credentials: false` on both checkouts. No personal token |
| Actions | First-party (`checkout`, `setup-node`, `cache`) by major version, as `ci.yml` does; SHA pinning is a later hardening step (record 0045) |
| Not run | The workflow itself. The first tag is its test, so release time is not measured |

## 2. Consuming a built commit (the fallback)

`consumer/` imports each component by its own entry (`@acme/ui/<entry>`), plus `@acme/ui/styles.css` and the generated `calm.css`. `src/Gallery.tsx` renders the 20 components in their smallest composition.

**The 20 components** are from ADR-0013's "B. Template main" row. All 20 exist; two have different entry names. Full list: dialog, tabs, menu, tooltip, toast, popover, drawer, data-grid, tree-view, progress, switch, slider, text-field, select, chip, avatar, calendar → `date-calendar`, chart → `charts`, skeleton, scroll-area.

| Check | Result | Command |
| --- | --- | --- |
| Install from tarball by path | PASS | `npm install` |
| Strict typecheck of all 20 compositions against the package's `.d.ts` | PASS (0 errors) | `tsc --noEmit` (`skipLibCheck: true` skips only checking inside `.d.ts`; usage is checked) |
| Vite production build | PASS | `vite build` |
| `styles.css` loads | PASS. The built CSS carries the layers `acme.tokens`, `acme.baseline` and `acme.components`, and the Calm overrides win over them (unlayered) | `grep` on `dist/assets/*.css` |
| Renders in Chromium, light and dark | PASS. 20 children in `main`, 2 tabs, 2 grids with 34 cells, 1 tree, 1 progressbar, 1 switch, 1 slider, 1 chart SVG, 1 toast. Computed `--acme-canvas` is `#f6f4ef` / `#1b1a18` and `--acme-accent` is `#315c5f` / `#a1c6c4` (the adjusted tokens). No page errors; the only console error is `/favicon.ico` 404 (the page has no favicon) | `vite preview --host 127.0.0.1` + `CHROMIUM_PATH=… node render-check.mjs <library>/node_modules/playwright` → `results/render.json` |
| Storybook 10 build with the gallery (stories Light, Dark) | PASS | `storybook build` |

The render check uses the library's existing Playwright with its cached Chromium 1194 (the build its CI pins), so this spike installs no browser.

### Bundle size (`results/sizes.json`, from `node consumer/sizes.mjs <library> <tarball>`)

| What | gzip (Node zlib, level 9; system `gzip -9` differs by ≤ 21 B) | How |
| --- | --- | --- |
| **The 20 components as used in the gallery** (JS, shared chunks counted once; tree-shaken to the parts the gallery uses) | **56,538 B** (≈ 55 KiB) | Gallery `vite build` JS (124,206 B) minus a React-only build of the same config (67,668 B) |
| Ceiling: all 20 entries in full, React external | ≈ 59,345 B | **The reviewer's independent figure** (one esbuild bundle of all 20 entries), not reproduced here |
| React 19 + ReactDOM baseline | 67,668 B | `vite build --mode baseline` |
| Stylesheet `styles.css` + Calm (all 89 components' CSS; the library ships one file) | 30,172 B | Gallery `vite build` CSS (229,758 B raw) |
| Whole library, all 89 entries (library's figure) | 138,841 B | library `node scripts/check-size.mjs`, row "index (all)", Node 22 |

Per entry, from the library's own size gate (min + gzip -9, React external, Floating UI and shared layers counted in every entry, so they do not add up: the sum is 144,371 B). Run with Node 22, where the gate exits 0:

| Entry | B | Entry | B | Entry | B | Entry | B |
| --- | --- | --- | --- | --- | --- | --- | --- |
| menu | 17,203 | charts | 16,726 | select | 16,607 | tooltip | 13,136 |
| popover | 13,038 | data-grid | 9,998 | tree-view | 8,074 | date-calendar | 6,401 |
| toast | 5,642 | drawer | 5,390 | dialog | 4,943 | slider | 4,801 |
| tabs | 4,358 | scroll-area | 3,879 | text-field | 3,029 | chip | 2,625 |
| progress | 2,523 | avatar | 2,332 | switch | 2,273 | skeleton | 1,393 |

## 3. Calm theme (draft)

Neither repository had a Calm token file, so this spike adds a minimal draft from ADR-0013's description: neutral paper-like surfaces, one muted teal accent, no gradients, glass or glow, and light and dark variants.
- `consumer/calm.tokens.json`: W3C Design Tokens, colours only.
- `consumer/calm.mjs`: the generator. It checks contrast and writes `calm.css`, which overrides `--acme-*` unlayered and maps holo, glass and glow to flat values. It also sets durations to 0 under `prefers-reduced-motion`.

**What `calm.mjs` checks** (`results/contrast.json`, 218 pairs, WCAG 2.2 relative luminance; text 4.5:1, UI 3:1), computing every background the way the library's CSS composes it:

| Group | Pairs | How the background is computed |
| --- | --- | --- |
| Flat | text, muted text, accent and 4 status colours on canvas, surface and raised surface; control border, focus and accent (UI) on the same | token values |
| On-tone | on-tone text on each tone fill: accent, neutral, danger, success, warning, info | token values |
| Soft button (button.css:41-42) | tone text on its tone at 16 % and at 24 % (hover) over each surface; tones accent, neutral, danger | `color-mix(in oklab, tone p%, transparent)` = tone at alpha p (CSS Color 5), composited in gamma-encoded sRGB as browsers do |
| Outline button hover | tone text on `color-mix(in oklab, tone 10%, glass)`; glass = surface under Calm | **real Oklab interpolation** (Ottosson's matrices) |
| Ghost button hover | tone text on the 5 % text overlay (`--acme-hover`) over each surface | alpha composite |
| Hover border | control border (UI) on the 5 % hover overlay over each surface | alpha composite |
| Toast (toast.css:46, 87) | text and muted text on `color-mix(in srgb, status 10% / 18%, raised)` for 4 statuses + neutral | sRGB mix |
| Chip (chip.css:39, 43) | text on `color-mix(in oklab, tone 12%, raised)` and on the tone at 10 % over each surface | Oklab mix / alpha composite |

**Before and after.** With the first draft's tokens, 198 of 218 pairs passed. 20 failed, all composited:
- light accent on its 24 % tint over canvas: 3.90:1;
- light danger at 24 % over canvas: 4.04:1;
- dark neutral on its 24 % tint over raised: 3.75:1;
- dark control border on hover over raised: 2.96:1;
- dark muted text on an alert toast: 3.99–4.23:1.

These match the reviewer's sRGB estimates (3.90 and 3.76).

The tokens were then adjusted in lightness only, keeping the hues:

| Mode | Token | Before | After |
| --- | --- | --- | --- |
| Light | accent / focus | #3d6b6e | #315c5f |
| Light | neutral | #5e5a52 | #56524a |
| Light | danger | #a63a32 | #943229 |
| Dark | accent / focus | #8fb8b6 | #a1c6c4 |
| Dark | neutral | #a9a499 | #c7c2b7 |
| Dark | danger | #e39a92 | #f0b6af |
| Dark | text-muted | #a9a499 | #bdb8ad |
| Dark | control-border | #7f7a70 | #878277 |

Result: **218 of 218 pairs pass.**

| Mode | Lowest text pair (min 4.5) | Lowest UI pair (min 3) |
| --- | --- | --- |
| Light | 4.68:1 (danger on its 24 % tint over canvas) | 3.25:1 (control border on hover over canvas) |
| Dark | 4.57:1 (accent on its 24 % tint over raised) | 3.30:1 (control border on hover over raised) |

Muted text on surface is 6.57:1 (light) and 8.13:1 (dark).

Notes:
- `--acme-edge` (the library's 1 px inset highlight) is not overridden under Calm, so it keeps the library's value.
- Focus equals accent. That works because the library draws focus as a 2 px outline with an offset, so the ring sits on the surface (focus on surfaces ≥ 3:1 is checked), not on the accent fill.
- Not checked: colours inside the library's component CSS that come from neither these tokens nor the mixes above (for example alert, snackbar, event-calendar and progress tints). WEB-UI's full Storybook contrast audit (ADR-0013 `verified_by`) covers those.

## Findings

1. **Publishing needs a scope rename.** It is done in the workflow, not in the source (record 0045). Consumers will import `@m1than/ui/<entry>`, while a locally packed tarball is still `@acme/ui`. reader-ai should switch its imports once, when it moves from tarball to registry.
2. **Installing from GitHub Packages needs a token.** It needs an npm token with `read:packages`, even for a public repository. The reviewer believes it must be a **classic** personal access token, because fine-grained tokens do not cover GitHub Packages; **to be verified** at install time. For reader-ai: an `.npmrc` with `@m1than:registry=https://npm.pkg.github.com`, and the token as `secret://…` (ADR-0011), never in the repository. CI needs it as an Actions secret.
3. **One stylesheet for all components.** `styles.css` is 30 KB gzip and is not split per component. That is fine for v1.
4. **No page background.** The library sets none unless its baseline or `css-baseline` entry is used: the computed body background was transparent. reader-ai's app shell must apply `--acme-canvas` itself or use the baseline entry.
5. **Tinted backgrounds decide the palette.** Flat-pair contrast was not enough: the soft-button and alert-toast tints were the binding constraints. WEB-UI's Calm task should keep `calm.mjs`'s composited pairs (or the library's own contrast tests) as its gate.
6. **The library's size gate depends on the Node version.** Under Node 26.13.0 its `check-size.mjs` fails 5 entries by 1–19 B, while under Node 22.22.1 (its CI runtime) it passes:
   - breadcrumbs +1 B;
   - chat +9 B;
   - data-grid +19 B;
   - drawer +7 B;
   - modal +4 B.

   The likely cause is a different bundled zlib, which is not verified. The budgets sit at or within a few bytes of the measured sizes. If the library moves to Node 26 (reader-ai's proposed ADR-0020 baseline), its budgets must be re-measured.
7. **No conflict between the two repositories' rules.** The library's AGENTS.md (never push or publish) and reader-ai's AGENTS.md (the library is prepared as a branch and the task is blocked for the owner) agree. This spike stops at a local branch.

## What the owner must do

1. **Review the library branch:** `cd ~/Developer/ui-builder-lib-with-agents && git log --oneline -2 release/github-packages && git diff web3/material-parity...release/github-packages --stat`. It adds `.github/workflows/release.yml` and decision record 0045.
2. **Push it and open a PR in the library repository:** `git push -u origin release/github-packages`. Merge it into `web3/material-parity` when CI is green.
3. **Tag the merged commit:** `git tag v0.1.0 <merged sha> && git push origin v0.1.0`. Watch the `release` workflow, and note the duration of its `gates` job, which is the release time this spike could not measure.
4. **Tell the agent the version.** The agent then reruns this consumer against `@m1than/ui@0.1.0` from the registry and records install time and release time. That needs a `read:packages` token on the Mac (likely a classic PAT, to be verified), provided the owner's way (for example the Keychain, ADR-0011).
