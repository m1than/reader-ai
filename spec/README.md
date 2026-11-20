# spec/

Module specs are the source of truth (ADR-0001). A spec says what a module must do, which files it owns, which track it belongs to, and how each statement is verified.

- `modules/<layer>-<name>.yaml` — one spec per module; start from [`_template.yaml`](_template.yaml).
- `fixtures/<module>/` — fixed inputs with known outputs (`VEC-*` items), each with its source; recorded agent transcripts for tests live in `fixtures/<module>/transcripts/`.

Lifecycle: `draft` (agent) → `approved` (owner) → `implemented` (owner). Code is written only for `approved` specs whose `paths` claim the files (hook H1, CI T8).

Tracks: `product` (user-facing features), `ai` (agents, models, evaluation), `engineering` (platform, performance, optimization). CI rule T1 rejects a spec without one.

The 28 planned specs, their stages, tracks and key requirements are in [docs/roadmap.md](../docs/roadmap.md). Specs are drafted stage by stage, not all at once.
