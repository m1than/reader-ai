# spec/

Module specs are the source of truth (ADR-0001). A spec says what a module must do, which files it owns, and how each statement is verified.

- `modules/<layer>-<name>.yaml` — one spec per module; start from [`_template.yaml`](_template.yaml).
- `fixtures/<module>/` — fixed inputs with known outputs (`VEC-*` items), each with its source.

Lifecycle: `draft` (agent) → `approved` (owner) → `implemented` (owner). Code is written only for `approved` specs whose `paths` claim the files (hook H1, CI T8).

## Planned modules

Drafted after the owner accepts the ADRs, in this order, so each step is usable on its own:

| # | Module | ADR | First usable result |
| --- | --- | --- | --- |
| 1 | `OPS-DEPLOY`, `SRV-CONFIG` | 0011, 0012 | `reader up` brings up an empty, healthy stack with passkey sign-in |
| 2 | `SRV-EVENTS` | 0006 | Event log, job queue, SSE stream |
| 3 | `SRV-INGEST` | 0008 | Bashir PDF → outline (VEC: 494 bookmarks) and chunks with spans |
| 4 | `SRV-LESSON`, `WEB-NOTEBOOK`, `OPS-SANDBOX` | 0003, 0004, 0013, 0014 | Lesson L01 opens, labs run in the sandbox |
| 5 | `SRV-AGENT`, `SRV-PIPELINE` | 0005, 0009 | L02 compiled end to end through both gates |
| 6 | `SRV-TUTOR`, `SRV-RECALL`, `SRV-HABIT`, `WEB-SESSION` | 0010 | Daily study loop |
| 7 | `SRV-SEARCH`, `SRV-MCP` | 0007, 0015 | Sectorized knowledge base over MCP |
| 8 | `SRV-SHARE` | 0016 | Paired program |
