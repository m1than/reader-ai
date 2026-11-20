# reader-ai

Turn any study material into an interactive, verified notebook course, and study it with Claude as a tutor.

- **Upload** a book, paper, documentation site or repository. Structure comes from the material's own outline first.
- **Compile** it into one `.ipynb` lesson per unit: diagnostics, reading route, explanations with citations, what changed since publication, errata, trade-off tables, labs with independent expected values, recall cards.
- **Verify** every lesson before it is published: fact-check, lab execution in a sandbox, adversarial review by a fresh agent, two owner gates.
- **Study** in the same place: one click to resume, a sandboxed kernel for practice, a tutor bound to the current cell, spaced repetition, a habit tracker built on the four laws of *Atomic Habits*.
- **Share** courses, run paired study and teaching programs; every person's tutor runs on their own credentials.
- **Connect** other agents over MCP to the knowledge base the study produces, organised by sectors.

Self-hosted: Docker Compose behind Caddy, Postgres with pgvector, passkey sign-in, one declarative `reader.yaml` with secrets kept in the OS keychain.

## Status

Design phase. The decision package is in [docs/adr](docs/adr/) (19 ADRs) with an overview in [docs/architecture.md](docs/architecture.md) and the staged build plan in [docs/roadmap.md](docs/roadmap.md). The owner has agreed to ADR-0001 … 0018; the status flip is his commit. ADR-0019 (autonomous agent mode) is proposed. Implementation starts with stage 0 of the roadmap, run by the agent.

Users own what they build: the whole knowledge base can be downloaded at any time as an open archive (Markdown, `.ipynb`, JSON).

Plans (ADR-0017, v2):
- **Free:** the user's own Claude Code or Codex agent runs the pipeline over MCP.
- **Pro and Max:** reader-ai runs the transformation on its own models, with credits sized in pages of material and a 70% gross margin over model cost.
- Pages shared by link with people who were not invited and have no plan or provider are read-only.

| ADR | Decision |
| --- | --- |
| [0001](docs/adr/0001-process-and-enforcement.md) | specd-lite process, spec-first gate, CI and hooks |
| [0002](docs/adr/0002-product-scope.md) | Scope, non-goals, Bashir pilot |
| [0003](docs/adr/0003-notebook-runtime.md) | Jupyter kernel protocol via Kernel Gateway |
| [0004](docs/adr/0004-lesson-notebook-format.md) | Lessons as nbformat v4 with a cell metadata schema |
| [0005](docs/adr/0005-agent-runtime-and-providers.md) | Claude Agent SDK, per-user providers, budgets |
| [0006](docs/adr/0006-backend-architecture.md) | Async Python, outbox events, Postgres queue, SSE |
| [0007](docs/adr/0007-storage.md) | Postgres + pgvector, content-addressed blobs |
| [0008](docs/adr/0008-ingestion.md) | Outline-first ingestion, Docling |
| [0009](docs/adr/0009-compile-pipeline-and-verification.md) | Staged compile with verification and owner gates |
| [0010](docs/adr/0010-learning-loop.md) | Learning loop: Atomic Habits, FSRS, tutor |
| [0011](docs/adr/0011-config-and-secrets.md) | `reader.yaml`, secret references, keychain on macOS/Windows/Linux |
| [0012](docs/adr/0012-deployment-and-auth.md) | Compose, Caddy, volumes, passkeys |
| [0013](docs/adr/0013-frontend-and-rendering.md) | React, Storybook workbench, safe rendering |
| [0014](docs/adr/0014-sandbox-security.md) | Sandbox for lab execution |
| [0015](docs/adr/0015-mcp-knowledge-connector.md) | MCP connector over the sectorized knowledge base |
| [0016](docs/adr/0016-sharing-and-paired-programs.md) | Sharing, roles, paired programs |
| [0017](docs/adr/0017-accounts-data-ownership-and-monetization.md) | Accounts, always-available export, monetization of transformation |
| [0018](docs/adr/0018-agent-bus-traces-and-local-models.md) | Agent bus, run traces and quality signals (v1); evaluation, local models, tuning (v2) |
| [0019](docs/adr/0019-autonomous-agent-mode.md) | Autonomous agent mode: the agent approves specs and merges; the owner reviews after |

## Pilot

[pilot/bashir](pilot/bashir/): Bashir, *Mastering Blockchain* (Russian edition, DMK 2025). All 23 chapters are marked up as lessons in `course.yaml`, generated from `plan.yaml` and the PDF bookmark outline:

```bash
uv run pilot/bashir/build_course.py /path/to/bashir-2025.pdf   # checks sha256, writes course.yaml
```

The PDF is never committed.

## Development

The build is run by an autonomous agent (ADR-0019) on the owner's Mac. Rules: [AGENTS.md](AGENTS.md) (shared) and [CLAUDE.md](CLAUDE.md) (Claude Code). Run state: [docs/progress.yaml](docs/progress.yaml).

Owner, one command (first time it clones; afterwards just the last part):

```bash
mkdir -p ~/Developer && cd ~/Developer && (test -d reader-ai || git clone https://github.com/m1than/reader-ai.git) && cd reader-ai && bash tools/agent/start.sh
```

`tools/agent/start.sh` is idempotent and runs these steps in order:
1. installs missing CLI tools;
2. pulls `main`;
3. asks before accepting ADR-0001 … 0019, an owner gate that needs your "y";
4. merges the auto mode rules into `~/.claude/settings.json`, keeping a backup;
5. links Telegram;
6. runs `tools/agent/bootstrap-mac.sh --install`;
7. starts `caffeinate -dimsu claude --permission-mode auto` with `tools/agent/kickoff.md`.

Run it again after any stop: the agent resumes from `docs/progress.yaml`.

Tooling:

```bash
npm ci --prefix tools
node tools/trace/check.mjs      # T1–T8: spec ↔ tests ↔ code ↔ ADRs
```

Details: [tools/README.md](tools/README.md).
