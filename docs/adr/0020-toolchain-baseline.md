---
id: ADR-0020
title: Toolchain baseline — Node 26 LTS, Python 3.14, Debian 13 for everything reader-ai builds
status: proposed
date: 2026-11-20
verified_by: "CI on Node from web/.nvmrc; server CI on Python 3.14 once switched; spikes/agent-sdk-container results/20261120T180638 and 20261120T180949 (PR #6) run on python:3.14-slim-trixie; spikes/ingest-docling RESULT.md (OCR conversion on 3.14, pending); spikes/kernel-roundtrip re-run on 3.14 (pending)"
revisit_when: "A new Node LTS line is promoted (next: Node 28, about 2027-10); every server, worker, ingest and sandbox dependency installs and runs on a newer Python minor on linux/arm64; Debian stable changes"
supersedes: []
---

## Context

Owner decision, 2026-11-20: "Use latest LTS versions of libs etc."

ADR-0006 (itself proposed) states the server runtime as Python 3.13. The roadmap asks for Node 22+. The merged spike `spikes/kernel-roundtrip` uses `python:3.13-slim-bookworm`.

**Scope:** runtimes and base images only, meaning Node, Python and the Debian base of every image reader-ai builds. Library versions, Rust, Deno, Postgres and third-party images (Caddy, Postgres) are out of scope. Libraries follow their own pins in lockfiles.

State on 2026-11-20 (endoflife.date API; nodejs.org/dist/index.json):

| Runtime | Version | Active / security support until | End of life |
| --- | --- | --- | --- |
| Node.js 26 "Lithium" | 26.13.1 (Homebrew `node@26`: 26.13.0) | LTS since 2026-10-28; active until 2027-10-27 | 2029-04-30 |
| Node.js 24 | 24.22.3 | active support ended 2026-10-20 | 2028-04-30 |
| Python 3.15 | 3.15.2 | 2028-10-09 | 2031-10-31 |
| Python 3.14 | 3.14.10 | 2027-10-01 | 2030-10-31 |
| Python 3.13 | 3.13.18 | security fixes only since 2026-10-01 | 2029-10-31 |
| Debian 13 trixie | — | 2028-08-09 | 2030-06-30 (LTS) |
| Debian 12 bookworm | — | regular support ended 2026-07-11 | 2028-06-30 (LTS) |

Python has no LTS line. "Latest LTS" for Python therefore means the newest minor that the whole stack can install and run.

### Python 3.15 vs 3.14 (PyPI JSON API, newest release of each package, 2026-11-20)

- **No cp315 linux/arm64 wheel** (pip would build from source or fail):
  - scikit-image 0.26.0, through EasyOCR; only 0.27.0rc1 (2026-10-27) has one;
  - pyclipper 1.4.0, a hard EasyOCR dependency;
  - onnxruntime 1.32.0, which has no sdist either;
  - tree-sitter 0.26.1;
  - PyYAML 6.0.3.

  Each has a cp314 aarch64 wheel. The `docling[easyocr]` image on `python:3.15-slim-trixie` failed to build: pip fell back to the scikit-image sdist, which needs a C compiler.
- **Installable on 3.15** (a cp315, abi3 or `py3-none` wheel; installs, not run): torch, docling, pydantic-core, numpy, opencv-python-headless (abi3), pyzmq, claude-agent-sdk, jupyter-kernel-gateway, sqlalchemy, asyncpg, procrastinate, fastapi, uvicorn, pydantic, mypy, ruff, structlog, opentelemetry-sdk, psycopg, testcontainers, hypothesis, keyring.
  - Several declare classifiers only up to 3.13 (claude-agent-sdk 0.2.186) or 3.14 (docling, pydantic-core, asyncpg, uvicorn, opentelemetry-sdk, testcontainers, opencv).
  - Several declare none (procrastinate, keyring, pyzmq).
  - jupyter-kernel-gateway 3.0.1 (2024-03) and easyocr 1.7.2 (2024-09) predate Python 3.14.

### What has been run on 3.14 / trixie

| Image | Evidence | Status |
| --- | --- | --- |
| Agent SDK worker (`claude-agent-sdk` 0.2.186) | `spikes/agent-sdk-container/results/20261120T180638/` and `20261120T180949/` (PR #6, merged): 2 text runs and 1 sandboxed tools run pass | measured (runs) |
| Docling + EasyOCR ingest | image builds on `python:3.14-slim-trixie` (branch `0/S0.spike.ingest-docling`) | build only; an OCR conversion is pending in `spikes/ingest-docling/RESULT.md` (hypothesis until then) |
| Kernel sandbox (Kernel Gateway 3.0.1, ipykernel, evcxr, deno) | none; `spikes/kernel-roundtrip` measured `python:3.13-slim-bookworm` | not measured (hypothesis); re-run on 3.14 pending |

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. Node 26 LTS, Python 3.14, Debian 13** | Newest Node LTS and Debian stable; newest Python where every measured dependency has an aarch64 wheel; 1 more year of Python support than 3.13 (EOL 2030-10 vs 2029-10) | One minor behind the newest Python; kernel and OCR images not yet run on 3.14 |
| B. Node 26 LTS, Python 3.15, Debian 13 | Newest everything | 5 measured dependencies have no cp315 aarch64 wheel; the ingest image does not build |
| C. Keep Node 22, Python 3.13, Debian 12 | No change | Against the owner's instruction. Debian 12 regular support already ended (2026-07-11); Python 3.13 is security-only (since 2026-10-01) |

## Decision

- **Node 26 LTS** for `web/`. The major is pinned in `web/.nvmrc`, and CI reads that file (`node-version-file`), so a new LTS line is adopted only by changing the file, never by `lts/*` floating.
- **Python 3.14** for `server/`, the worker, the ingest image and the sandbox image.
- **Debian 13 trixie** as the base of every image reader-ai builds (`python:3.14-slim-trixie`).

This replaces the "Runtime" row of ADR-0006 (Python 3.13 → 3.14) and the roadmap's "Node 22+". For the kernel sandbox and the OCR path, the decision holds only after their pending runs pass (see the table above). If either fails on 3.14, that image stays on Python 3.13 (on bookworm as measured; 3.13 on trixie is itself a hypothesis) until it is fixed, and this ADR is amended. The owner should accept this ADR knowing that the kernel and OCR rows are hypotheses until their runs exist.

## Rejected options — why (numbers)

- **B (Python 3.15).** 5 measured dependencies (scikit-image, pyclipper, onnxruntime, tree-sitter, PyYAML) have no cp315 linux/arm64 wheel, and the ingest image fails to build. Compiling from source would add a compiler toolchain to images (size not measured). onnxruntime has no sdist at all.
- **C (no change).** It contradicts the owner's decision of 2026-11-20. Debian 12 left regular security support on 2026-07-11, and Python 3.13 has been security-only since 2026-10-01.

## Consequences

- `web/package.json`: `engines.node` `>=26 <27`, plus `web/.nvmrc` = 26. CI's web job reads `web/.nvmrc`.
- The tooling in `tools/` (hooks, trace-check) keeps `engines >=20` and runs on whatever `node` the owner's shell provides, 22 today. It is not part of this baseline.
- On acceptance, these 3.13 pins move to 3.14 in one PR:
  - `server/.python-version`;
  - `server/pyproject.toml` `requires-python`, `[tool.ruff] target-version`, `[tool.mypy] python_version`;
  - `server/uv.lock`;
  - `AGENTS.md` (repository map);
  - `docs/roadmap.md` (0.2 skeleton);
  - an "Amended by ADR-0020" note in ADR-0006's Runtime row, following the ADR-0019 precedent.
- Spikes started after 2026-11-20 use `python:3.14-slim-trixie`. Spikes are outside the spec gate and measure on the target runtime.
- **Image pinning.** Spikes use floating tags (`python:3.14-slim-trixie`). Images that ship (`sandbox/`, `deploy/`) pin the base by digest (`@sha256:…`), updated deliberately. The host's uv-managed Python may run a different 3.14 patch than the images.
- The owner's Mac keeps its default `node` (22). `node@26` is installed as a separate Homebrew keg and used for `web/` only.

## Open questions

- Should the shipped images pin by digest (proposed above) or by full version tag (`3.14.10-slim-trixie`)?
- Should `tools/` also move to Node 26 (`engines`, hooks run with `node@26`)?
