---
id: ADR-0037
title: Re-enabling disabled sign-up accounts
status: proposed
date: 2026-11-24
verified_by: "SRV-AUTH FR-AUTH-48 [db]: disable → enable → sign-in with the kept passkey; enable twice is idempotent; pending and configured users refused; a learner gets 403 with nothing changed; WEB-ACCOUNTS and WEB-MANAGE show Enable only on a disabled sign-up account"
revisit_when: "Sign-up is served beyond 127.0.0.1 (the VPS ADR); or more than 5 enable actions in a month, which would mean disable is used as a temporary suspension and deserves its own state"
supersedes: []
---

> Amends, if accepted: ADR-0031 (accepted 2026-11-23) decision 4, the sentence "disable cannot be undone in the
> product (the screen says so)". Drafted with WEB-MANAGE (V/manage, 2026-11-24) after the coordinator's default for
> the management layer's decision D4: add an Enable action. Until the owner accepts this ADR, SRV-AUTH FR-AUTH-48
> is not built and no screen offers Enable.

## Context

ADR-0031 lets anyone on the Mac sign up; the owner approves each account and can disable it. Decision 4 made
disable permanent in the product. The reason given there is implicit: a disabled account cannot quietly come back.
Today the only undo is the owner's database (WEB-ACCOUNTS owner note 1), and a person disabled by mistake must sign
up again under another handle, because the old handle stays taken by the disabled row.

The management layer (WEB-MANAGE) gives the owner a People tab with Disable, Delete, Sign out everywhere and Remove
a passkey. The coordinator's default for decision D4 (2026-11-24) is to add Enable beside Disable.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| A. Keep disable permanent | ADR-0031 unchanged; a disable is final | A mistaken disable costs the person their handle; recovery needs psql |
| B. Enable for sign-up accounts (FR-AUTH-48) | One owner action restores access; passkeys kept through the disable keep working; sessions revoked by the disable stay revoked | One more owner route with `recent_session`; the copy "cannot be undone here" goes; disable becomes reversible, so it no longer proves an account is gone for good |
| C. Delete instead of enable | WEB-MANAGE FR-AUTH-43 deletes a sign-up learner with no data, freeing the handle | Does not help a person who owns data; they lose it or stay disabled |

## Decision (proposed)

Option B, for `source = 'signup'` accounts only. Configured accounts stay governed by `users[]` in reader.yaml
(FR-AUTH-1 re-enables a configured handle that is named again). Enable runs under the same row lock and the same
recent-session rule as approve, reject and disable, and writes an audit row (SRV-EVENTS FR-EVENTS-30 `user.enable`).

## Rejected options — why

- A: the cost of a mistake is the handle and any data the person owns; the protection it buys (a disable cannot be
  reversed by a stolen owner session) is already covered by `recent_session` (10 minutes) on the enable route.
- C alone: it covers only accounts without data.

## Consequences

- Easier: a mistaken disable is undone in one click.
- Harder: a disabled account is no longer a final state; anyone auditing access reads the audit rows, not
  `disabled_at` alone.
- Unchanged: reader.yaml never changes a sign-up account (INV-AUTH-13); sessions ended by a disable are not revived.

## Open questions

1. Accept B, or keep A?
