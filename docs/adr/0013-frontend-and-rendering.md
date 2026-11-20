---
id: ADR-0013
title: Frontend — React on the owner's headless library, a calm theme, Storybook as the live workbench, safe rendering
status: proposed
date: 2026-11-20
verified_by: "WEB-UI: every reader-ai component has stories and interaction tests in CI; the Calm theme passes contrast checks (text 4.5:1, controls 3:1) in light and dark; WEB-RENDER tests render each MIME type and reject script injection"
revisit_when: "The library's component API changes incompatibly at the pinned commit, or a notebook feature needs JupyterLab internals that @jupyterlab/services does not expose"
---

## Context

Owner decisions, 2026-11-20:

- A web app with its own UI that can be changed on the fly through a component library in Storybook.
- Take the owner's library `m1than/ui-builder-lib-with-agents` as the base. Redesign the look into calm tones without decorative gradients, so a learner connects to the platform with focus and attention.
- ipywidgets are needed in v1.
- Everything must render and draw correctly: math, diagrams, tables, code, notebook outputs, shared views.

What the library is (read 2026-11-20):

- Headless React 19 components. Behaviour and accessibility live in TypeScript; looks live in one static stylesheet. ESM only, one package entry per component.
- Its own spec → test → implement → review workflow, with live Playwright tests and size budgets.
- A theme is a token swap on `<html>`; components read tokens only.
- Branch `main` holds the template: core layers plus Select.
- Branch `web3/material-parity` (commit `acc9e4a`, 2026-10-29) has 100 component modules, including dialog, tabs, menu, tooltip, toast, data-grid, tree-view, charts and chat. Its theme "Holo" is a dark Web3 palette with gradients and glass.

## Decision

| Concern | Choice |
| --- | --- |
| App | React 19, TypeScript `strict`, Vite, TanStack Router and Query; API client generated from the server's OpenAPI |
| Component base | `ui-builder-lib-with-agents` at a pinned commit of `web3/material-parity`, consumed as a built package. reader-ai never edits the library's source |
| Look | A new token set **Calm** in `web/src/ui/tokens/` that replaces Holo by token swap. Calm means: neutral paper-like surfaces, one muted accent, no gradients, no glass blur, no glow; a type scale tuned for long reading; motion only where it carries state, and none when `prefers-reduced-motion` is set; light and dark variants with measured contrast |
| reader-ai components | `web/src/ui/` composes library parts into product components: Cell, Output, TutorPanel, RecallCard, HabitTracker, SectorMap, GateReview, CohortBoard |
| "Change on the fly" | Tokens are data (W3C Design Tokens JSON, compiled to CSS variables). A Storybook toolbar edits tokens live and exports the diff as a JSON patch for a PR. The agent edits components through Storybook MCP |
| Storybook | Storybook 10 with the Vitest addon, stories for every reader-ai component and a gallery of library components under the Calm theme. Caddy serves the built Storybook at `/storybook` for the owner |
| Kernel client | `@jupyterlab/services` against the api's authenticated kernel relay |
| Notebook UI | Own React cell components rendering nbformat JSON; JupyterLab is not embedded |
| Widgets | ipywidgets through `@jupyter-widgets/html-manager` over the same kernel connection; only the standard `@jupyter-widgets/controls` and `output` modules load. Third-party widget JavaScript is blocked in v1 |

**Rendering matrix**

| Content | Renderer | Safety |
| --- | --- | --- |
| Markdown | remark/rehype (GFM) | rehype-sanitize allowlist |
| Math | KaTeX | Trust disabled |
| Diagrams | Mermaid | `securityLevel: strict`; Calm colors through Mermaid theme variables |
| Code | Shiki | Static HTML; a Calm syntax theme |
| Output `text/plain`, `image/png`, `image/jpeg`, `application/json`, `text/markdown`, `text/latex` | Built-in | — |
| Output `image/svg+xml`, `text/html` | Sandboxed iframe (`sandbox="allow-scripts"`, no same-origin) | Cannot read cookies or call the api |
| ipywidgets (controls, output) | `@jupyter-widgets/html-manager` | Standard modules only |

## Options for the component base

| Option | Gives | Costs |
| --- | --- | --- |
| **A. Built package of `web3/material-parity`, pinned, re-themed by tokens** | 100 component modules ready; the library's tests and budgets stay with it; Calm is a token set, not a fork | A build step to consume an unpublished package; pin upgrades are deliberate |
| B. New library from the template `main`, components built on demand | Clean start with only what reader-ai needs | Starts from 1 component (Select); every other component goes through the 10-step workflow first |
| C. Vendor the library into `web/` | One repository | Two process systems (the library's workflow and specd-lite) and two hook sets in one tree |
| D. Shadcn/MUI instead | Familiar | Ignores the owner's library; theming fights the token model |

## Rejected options — why (numbers)

- **B. Template `main`.** 1 ready component against the roughly 20 the reader-ai screens need (dialog, tabs, menu, tooltip, toast, popover, drawer, data-grid, tree-view, progress, switch, slider, text-field, select, chip, avatar, calendar, chart, skeleton, scroll-area). All 20 exist on `web3/material-parity`.
- **C. Vendoring.** Doubles the rule systems an agent must follow in one tree: specd-lite hooks H1–H3 plus the library's hooks and step budgets. A blocked edit would have two possible owners.
- **D. Other libraries.** Discards a library the owner maintains for exactly this agent-driven workflow.
- **Embedded JupyterLab notebook.** Its widgets are built on Lumino, not React; 0 of them can be restyled through Calm tokens or edited as stories.

## Consequences

- The library's tokens use the `acme` prefix on that branch; Calm overrides `--acme-*` variables. Renaming the prefix is a library change, not a reader-ai one.
- Consuming the package: reader-ai's `web/` builds the pinned commit in a dedicated step (`npm run build` of the library) and installs its `dist/`. Publishing the library to GitHub Packages would replace that step; it is the library owner's decision.
