# Spike: library-publish — RESULT

**Question (docs/roadmap.md 0.3):** Publish `ui-builder-lib-with-agents` (from `web3/material-parity`) to GitHub Packages through a release workflow in its repository that runs its gates; consume the version in Vite and Storybook 10; apply a draft Calm token file.
**Measure:** release time; bundle size of the 20 components reader-ai needs; contrast of Calm.
**Pass criterion:** the package installs by version; Calm passes 4.5:1 and 3:1.
**Fallback:** consume a built commit until the release workflow is green.

**Verdict: PARTIAL, waiting for the owner.**
- The release workflow is written and committed on a local library branch, but not pushed, tagged or run. The library's AGENTS.md forbids an agent to push or publish. So "installs by version" and the release time are **not measured**.
- The fallback works: a tarball of a built commit installs into a Vite 8 + React 19 + Storybook 10 consumer on Node 26. All 20 components typecheck and render in Chromium with no page errors.
- The draft Calm theme passes all 62 contrast pairs.

Date: 2026-11-20.

## Environment

| Item | Value |
| --- | --- |
| Host | Apple M5 Max, macOS 27.2; a Docling measurement was running on the Docker VM at the same time (host CPU shared) |
| Library | `/Users/m1than/Developer/ui-builder-lib-with-agents`, base `web3/material-parity` at `acc9e4af` (2026-10-29); package `@acme/ui` 0.0.0, `private: true`, 89 entries, runtime dependency `@floating-ui/react-dom`, peers React 19 |
| Library branch | `release/github-packages` at `6355fe67` (local only) |
| Tarball | `npm run build` + `npm pack` → `data/spikes/library-publish/acme-ui-0.0.0.tgz` (gitignored), 353,801 bytes, 437 files, sha256 `14425dfe1e426384…` |
| Consumer | `spikes/library-publish/consumer/`: Node 26.13.0, Vite 8.3.8, React 19.3.1, TypeScript 7.0.2, Storybook 10.6.8 (`@storybook/react-vite`), `@vitejs/plugin-react` 6.3.0 |

## 1. Release workflow (library repository, local branch)

`.github/workflows/release.yml` plus decision record `docs/decisions/0045-release-to-github-packages.md` (indexed). `npm run check` passes (43 records, numbers unique), and `actionlint` reports no findings.

| Concern | Design |
| --- | --- |
| Trigger | A pushed tag `v<semver>`, checked by a regex in the publish job. A person pushes the tag; agents never do (library AGENTS.md) |
| Gates | Job `gates`: `npm run gates` (every gate CI runs: static checks, every test file, sizes) on the tagged commit, with the CI browser (Chromium 1194, Playwright 1.56.1). `publish` needs it |
| **Scope** | GitHub Packages accepts only the owner's scope. The source keeps the template name `@acme/ui` (`init.mjs` renames it once per new project). The publish job sets `name=@<owner>/ui`, `version=<tag without v>`, `publishConfig.registry=https://npm.pkg.github.com` and `private=false` with `npm pkg set`, in its own checkout only; nothing is committed back. This is safe because no source file imports the package by name (`grep -rn "from '@acme/ui" src`: 0 hits). The name appears only in comments and error texts, so `dist/` is the same bytes under either name |
| Credentials | Built-in `GITHUB_TOKEN`, job permissions `contents: read`, `packages: write`. No personal token |
| Not run | The workflow itself. The first tag is its test, so release time is not measured |

## 2. Consuming a built commit (the fallback)

`consumer/` imports each component by its own entry (`@acme/ui/<entry>`), plus `@acme/ui/styles.css` and the generated `calm.css`. `src/Gallery.tsx` renders the 20 components in their smallest composition.

**The 20 components** are from ADR-0013's "B. Template main" row. All 20 exist; two have different entry names. Full list: dialog, tabs, menu, tooltip, toast, popover, drawer, data-grid, tree-view, progress, switch, slider, text-field, select, chip, avatar, calendar → `date-calendar`, chart → `charts`, skeleton, scroll-area.

| Check | Result | Command |
| --- | --- | --- |
| Install from tarball by path | PASS | `npm install` (Node 26) |
| Strict typecheck of all 20 compositions against the package's `.d.ts` | PASS (0 errors) | `tsc --noEmit` (`skipLibCheck: true` skips only checking inside `.d.ts`; usage is checked) |
| Vite production build | PASS | `vite build` |
| `styles.css` loads | PASS. The built CSS carries the layers `acme.tokens`, `acme.baseline` and `acme.components`, and the Calm overrides win over them (unlayered) | `grep` on `dist/assets/*.css` |
| Renders in Chromium, light and dark | PASS. 20 children in `main`, 2 tabs, 2 grids with 34 cells, 1 tree, 1 progressbar, 1 switch, 1 slider, 1 chart SVG, 1 toast. Computed `--acme-canvas` is `#f6f4ef` / `#1b1a18` and `--acme-accent` is `#3d6b6e` / `#8fb8b6`. No page errors; the only console error is `/favicon.ico` 404 (the page has no favicon) | `vite preview` + `CHROMIUM_PATH=… node render-check.mjs <library>/node_modules/playwright` → `results/render.json` |
| Storybook 10 build with the gallery (stories Light, Dark) | PASS | `storybook build` |

The render check uses the library's existing Playwright with its cached Chromium 1194 (the build its CI pins), so this spike installs no browser.

### Bundle size

| What | gzip -9 | How |
| --- | --- | --- |
| **All 20 components together** (JS, shared chunks counted once) | **56,535 B** (≈ 55 KiB) | Gallery `vite build` JS (124,224 B) minus a React-only build of the same config (67,689 B) |
| React 19 + ReactDOM baseline | 67,689 B | `vite build --mode baseline` |
| Stylesheet `styles.css` + Calm (all 89 components' CSS; the library ships one file) | 30,179 B | Gallery `vite build` CSS (229,758 B raw) |
| Whole library, all 89 entries (library's figure) | 138,841 B | library `node scripts/check-size.mjs`, row "index (all)" |

Per entry, from the library's own size gate (min + gzip -9, React external, Floating UI and shared layers counted in every entry, so these do not add up):

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

`node calm.mjs` → **62 of 62 pairs pass** (WCAG 2.2 relative luminance) → `results/contrast.json`.

| Mode | Text pairs (min 4.5:1): lowest | Non-text pairs (min 3:1): lowest | Body text on surface |
| --- | --- | --- | --- |
| Light | 5.42:1 (accent on canvas) | 3.58:1 (control border on canvas) | 15.43:1 |
| Dark | 5.85:1 (muted text on raised surface) | 3.41:1 (control border on raised surface) | 13.26:1 |

- **Text pairs:** text, muted text, accent and the four status colours, each on canvas, surface and raised surface; plus on-tone on accent.
- **Non-text pairs:** control border, focus and accent on the same three surfaces.
- **Not checked:** colours inside the library's component CSS that do not come from these tokens; WEB-UI's full Storybook contrast audit (ADR-0013 `verified_by`) does that.

## Findings

1. **Publishing needs a scope rename.** It is done in the workflow, not in the source (record 0045). Consumers will import `@m1than/ui/<entry>`, while a locally packed tarball is still `@acme/ui`. reader-ai should switch its imports once, when it moves from tarball to registry.
2. **Installing from GitHub Packages needs a token.** It needs an npm token with `read:packages`, even for a public repository. For reader-ai: an `.npmrc` with `@m1than:registry=https://npm.pkg.github.com`, and the token as `secret://…` (ADR-0011), never in the repository. CI needs it as an Actions secret.
3. **One stylesheet for all components.** `styles.css` is 30 KB gzip and is not split per component. That is fine for v1.
4. **No page background.** The library sets none unless its baseline or `css-baseline` entry is used: the computed body background was transparent. reader-ai's app shell must apply `--acme-canvas` itself or use the baseline entry.
5. **Rule conflict.** The library's AGENTS.md (never push or publish) and reader-ai's AGENTS.md (the library is prepared as a branch and the task is blocked for the owner) agree. This spike stops at a local branch.

## What the owner must do

1. **Review the library branch:** `cd ~/Developer/ui-builder-lib-with-agents && git log -1 release/github-packages && git show --stat release/github-packages`. It adds `.github/workflows/release.yml` and decision record 0045.
2. **Push it and open a PR in the library repository:** `git push -u origin release/github-packages`. Merge it into `web3/material-parity` when CI is green.
3. **Tag the merged commit:** `git tag v0.1.0 <merged sha> && git push origin v0.1.0`. Watch the `release` workflow, and note its duration, which is the release time this spike could not measure.
4. **Tell the agent the version.** The agent then reruns this consumer against `@m1than/ui@0.1.0` from the registry and records install time and release time. That needs a `read:packages` token on the Mac, provided the owner's way (for example the Keychain, ADR-0011).
