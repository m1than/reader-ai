---
id: ADR-0020
title: Toolchain baseline — newest Node LTS, newest Python the whole stack supports, current Debian stable
status: proposed
date: 2026-11-20
verified_by: "CI on the baseline (Node from .nvmrc, Python from server/.python-version); spike images on the baseline image build and pass (spikes/agent-sdk-container results/20261120T180638, spikes/ingest-docling)"
revisit_when: "A new Node LTS line is promoted; a new Python minor has stable linux/arm64 wheels for every server and worker dependency (today blocked by scikit-image for 3.15); Debian stable changes"
supersedes: []
---

## Context

Owner decision, 2026-11-20: "Use latest LTS versions of libs etc."

ADR-0006 fixes the server runtime at Python 3.13. The roadmap asks for Node 22+, and the spike images used `python:3.13-slim-bookworm`. State on 2026-11-20:

| Runtime | Newest | Support | Source |
| --- | --- | --- | --- |
| Node.js | 26.13.1, LTS "Lithium" (26.13.0 in Homebrew `node@26`) | LTS line | nodejs.org/dist/index.json |
| Python | 3.15.2; 3.14.10; 3.13.18 | EOL 2031-10, 2030-10, 2029-10 | endoflife.date/api/python.json |
| Debian | 13 "trixie"; 12 "bookworm" | EOL 2030-06, 2028-06 | endoflife.date/api/debian.json |

Python has no LTS line, so "latest LTS" for Python means the newest minor that the stack can actually use.

Measured on 2026-11-20 (PyPI JSON API, newest release of each package):

- Python 3.15 has wheels (cp315 or universal) for torch, docling, pydantic-core, numpy, opencv-python-headless, pyzmq, claude-agent-sdk, jupyter-kernel-gateway, sqlalchemy, asyncpg, procrastinate, fastapi, uvicorn, pydantic, mypy, ruff, structlog, opentelemetry-sdk, psycopg, testcontainers, hypothesis and keyring.
- **scikit-image** (a dependency of EasyOCR, the OCR engine in `spikes/ingest-docling`) has no stable cp315 linux/arm64 wheel. Only 0.27.0rc1 has one. The `docling[easyocr]` image on `python:3.15-slim-trixie` fails to build: pip falls back to the sdist, which needs a C compiler.
- scikit-image 0.26.0 has a cp314 manylinux aarch64 wheel. The Docling image builds on `python:3.14-slim-trixie`.
- The Agent SDK image runs on `python:3.14-slim-trixie`: 1 text and 1 sandboxed tools run pass.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. Node 26 LTS, Python 3.14, Debian 13** | Newest LTS / stable where the whole stack has stable wheels; 1 more year of Python support than 3.13 | One minor behind the newest Python |
| B. Node 26 LTS, Python 3.15, Debian 13 | Newest everything | OCR path does not build without a compiler or a release-candidate scikit-image |
| C. Keep Node 22, Python 3.13, Debian 12 | No change | Against the owner's instruction; Debian 12 ends 2 years earlier (2028-06 vs 2030-06) |

## Decision

Node 26 LTS for `web/` and tooling, Python 3.14 for `server/`, `sandbox/` and all images, and Debian 13 trixie as the base of every image (`python:3.14-slim-trixie`). This replaces the "Runtime" row of ADR-0006 (Python 3.13 → 3.14) and the roadmap's "Node 22+".

## Rejected options — why (numbers)

- **B (Python 3.15).** 1 of the measured dependencies (scikit-image, through EasyOCR) has no stable cp315 linux/arm64 wheel, so the ingest image fails to build. Building it from source would add a compiler toolchain to the image (size not measured), and the release candidate is not a stable release.
- **C (no change).** It contradicts the owner's decision of 2026-11-20. Debian 12's EOL is 2028-06, against 2030-06 for Debian 13.

## Consequences

- `web/` declares `engines.node >= 26` and `.nvmrc` = 26; CI's `lts/*` already resolves to 26.
- `server/.python-version` and `requires-python` move to 3.14 once this ADR is accepted. Until then they stay at 3.13 (ADR-0006).
- Spike images already use `python:3.14-slim-trixie`. Spikes are outside the spec gate and measure on the target runtime.
- The owner's Mac keeps its default `node` (22). `node@26` is installed as a separate Homebrew keg and used for this repository only, so other projects are not affected.
- Moving to Python 3.15 is a revisit trigger: it happens when every server and worker dependency has a stable cp315 linux/arm64 wheel.

## Open questions

- Should `uv` pin the exact patch (3.14.10) or the minor (3.14)? Proposed: the minor in `.python-version`, with the patch coming from the image tag digest.
