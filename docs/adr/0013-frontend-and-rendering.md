---
id: ADR-0013
title: Frontend — React, TypeScript, Vite; Storybook as the live component workbench; safe rendering of every output
status: proposed
date: 2026-11-20
verified_by: "WEB-UI: every component has stories and interaction tests run by the Storybook test runner in CI; WEB-RENDER tests render each MIME type and reject script injection"
revisit_when: "A notebook feature needs JupyterLab internals that @jupyterlab/services does not expose"
---

## Context

Owner decision, 2026-11-20: a web app with its own UI that can be changed on the fly through a component library in Storybook. Everything must render and draw correctly: math, diagrams, tables, code, notebook outputs, shared views.

The owner already maintains a headless React component library built for coding agents (`m1than/ui-builder-lib-with-agents`, Storybook-gated, with a Material parity branch). The agent could not read that repository in this session, so its reuse is an open question below.

## Decision

| Concern | Choice |
| --- | --- |
| App | React 19, TypeScript `strict`, Vite, TanStack Router and Query; API client generated from the server's OpenAPI |
| Component library | `web/src/ui`: headless primitives plus reader-ai components (Cell, Output, TutorPanel, RecallCard, HabitTracker, SectorMap, ...). Every component has stories; stories are the visual spec |
| "Change on the fly" | Design tokens as data in W3C Design Tokens format (`web/src/ui/tokens/*.json`) compiled to CSS variables; a Storybook toolbar edits tokens live and exports the diff as a PR-ready JSON patch; the agent edits components through Storybook MCP |
| Storybook | Storybook 10 with the Vitest addon; the built Storybook is served by Caddy at `/storybook` for the owner |
| Kernel client | `@jupyterlab/services` against the api's authenticated kernel relay |
| Notebook UI | Own React cell components; cells are rendered from nbformat JSON, not by embedding JupyterLab |

**Rendering matrix**

| Content | Renderer | Safety |
| --- | --- | --- |
| Markdown | remark/rehype (GFM) | rehype-sanitize allowlist |
| Math | KaTeX | Trust disabled |
| Diagrams | Mermaid | `securityLevel: strict` |
| Code | Shiki | Static HTML |
| Output `text/plain`, `image/png`, `image/jpeg`, `application/json`, `text/markdown`, `text/latex` | Built-in | — |
| Output `image/svg+xml`, `text/html` | Sandboxed iframe (`sandbox="allow-scripts"`, no same-origin) | Cannot read cookies or call the api |
| ipywidgets | Not in v1 | — |

## Options for the component library

| Option | Gives | Costs |
| --- | --- | --- |
| **A. Consume `ui-builder-lib-with-agents` as the base** | Reuses the owner's own agent-oriented library, its Storybook gate and conventions | Unknown here: API stability of its `main` template; coupling two repositories' release cycles |
| B. Own library in `web/src/ui` on Radix-style headless primitives | Self-contained; no cross-repo coupling | Re-builds primitives the owner already has |
| C. MUI | Largest component set | Theming through tokens fights MUI's own theme system; heavier bundle |

**Notebook UI options:** own React cells (chosen) vs embedding JupyterLab's notebook widget. JupyterLab's widgets are built on Lumino, not React; restyling them through Storybook stories is not possible, which contradicts the owner's requirement.

## Rejected options — why (numbers)

- **Embedded JupyterLab notebook.** 0 of its widgets can be edited as Storybook components; the owner's "change the UI on the fly" requirement fails at the central screen.
- **MUI (C).** Two theme systems (tokens and MUI theme) to keep in sync for every visual change.

## Open questions

1. Component library: A (reuse `ui-builder-lib-with-agents`) or B (own library in this repo)? A needs read access to that repository for the agent and a decision on which branch (`main` template or `web3/material-parity`) is the base.
2. ipywidgets support: needed for any planned lab, or out of scope for v1?
