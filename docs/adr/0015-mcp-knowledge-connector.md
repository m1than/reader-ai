---
id: ADR-0015
title: MCP connector — sectorized knowledge base for other agents
status: proposed
date: 2026-11-20
verified_by: "SRV-MCP tests against the MCP 2026-07-28 conformance suite where available; contract tests for every tool; a personal-use material never returns more than the citation limit"
revisit_when: "A new MCP specification revision changes the transport or authorization model"
---

## Context

> Amendment proposed by [ADR-0039](0039-knowledge-layers-and-amendment-rules.md) (2026-11-26): sectors become the `topic` facet; a concept layer is added; findings gain validity and an `orphaned` state; the tool table is replaced by the `brain/v1` contract, which keeps role filtering and the citation limit. In force once ADR-0039 is accepted; then, where the two disagree, ADR-0039 wins.

Owner decision, 2026-11-20: a separate connector that other agents can attach over MCP, acting as the markup source for a knowledge base organised by sectors, built from the conclusions and results produced while reading.

MCP state as of 2026-11-20: revision 2026-07-28 (final since 2026-07-28) removed the initialize handshake and the `Mcp-Session-Id` header. Every Streamable HTTP request is self-contained and carries `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers; HTTP+SSE transport, Roots, Sampling and Logging are deprecated.

## Knowledge model

| Entity | Meaning |
| --- | --- |
| **Sector** | A node in a top-down hierarchy of topics (`crypto/hash`, `crypto/ecc/signatures`, `bitcoin/transactions`). The top levels are a taxonomy of the knowledge domain, not of any book: the agent proposes them at gate G1 from the material and the learner's profile, the owner approves, lower levels grow from findings |
| **Finding** | A unit of knowledge produced while studying: `conclusion`, `tradeoff`, `decision`, `measurement`, `definition`, `erratum`, `open_question`. Has citations (source spans or URLs), sectors, the lesson and cell it came from, author (learner or agent), and `verified` (passed the fact-check stage) |
| **Link** | Typed relation between findings or sectors: `supports`, `contradicts`, `supersedes`, `depends_on` |

Findings are created by the pipeline (fact-checked claims), by the tutor at session close (learner conclusions, confirmed by the learner), and by the learner directly. The study journal from the blockchain knowledge base becomes findings of kind `decision`.

## Decision

A separate process (`mcp` entrypoint, same codebase) implementing MCP 2026-07-28 over Streamable HTTP at `/mcp`, plus stdio for local agents, with the official Python SDK.

| Tool | Scope | Returns |
| --- | --- | --- |
| `list_sectors(parent?)` | `kb:read` | Child sectors with finding counts |
| `get_sector(id)` | `kb:read` | Summary, top findings, related sectors |
| `search(query, sectors?, kinds?, course?)` | `kb:read` | Hybrid search hits with citations |
| `get_finding(id)` | `kb:read` | Finding with citations and links |
| `get_lesson_outline(course, lesson)` | `kb:read` | Sections, sectors, status |
| `propose_finding(...)` | `kb:write` | Creates a finding in state `proposed`; appears in the owner's review queue |

Resources mirror the read tools (`reader://sector/{id}`, `reader://finding/{id}`). Tool catalogs are declared as data and ordered deterministically.

**Access.** Remote clients use OAuth 2.1 per the MCP authorization specification, with reader-ai as the authorization server; local agents may use scoped personal access tokens. Every result is filtered by the caller's course roles (ADR-0016).

**Copyright limit.** For material with `license: personal-use`, results carry findings and citations, never source text beyond a short quote (default 300 characters per citation, configurable down).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **MCP server over the same database** | Any MCP client (Claude Code, Claude Desktop, other agents) reads the KB with no custom integration | One more entrypoint and its auth |
| REST API only | Simpler | Every agent needs a custom client |
| Export to files (Markdown vault) | Works offline | Stale copies; no search, no access control |

## Rejected options — why (numbers)

- **REST only.** Each consuming agent needs 1 custom integration; with MCP the count is 0.
- **File export only.** Access control is lost the moment a file is copied; personal-use material would leak through any shared folder.

## The book turned around for the learner

A book's table of contents is not used as the sector tree. It is used once, for a view that shows the learner how the material is reorganised for them: every source section is mapped onto sectors and marked by the learner's state (known from the diagnostic → skim; new → read in depth; a prerequisite missing → read another sector first). For the Bashir pilot this is the visible answer to "why the plan's order differs from the book's chapter order".

## Resolved questions (owner, 2026-11-20)

1. Sectors grow top-down from a domain taxonomy; the table of contents only feeds the view above.
2. External agents may propose findings through MCP (`kb:write`); proposals land in the owner's review queue. MCP is the dedicated layer for this.
