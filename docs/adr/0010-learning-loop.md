---
id: ADR-0010
title: Learning loop — Atomic Habits as the design frame, FSRS for spacing, tutor bound to the cell
status: proposed
date: 2026-11-20
verified_by: "WEB-SESSION Playwright: resume to first interaction ≤ 2 interactions and ≤ 10 s; SRV-RECALL tests on FSRS scheduling; SRV-HABIT tests for the never-miss-twice rule"
revisit_when: "Over 8 weeks the owner misses two days in a row more than twice, or recall accuracy stays outside 80–90% for 4 weeks"
---

## Context

Owner decisions, 2026-11-20:

- Use the verticals of James Clear's *Atomic Habits*: distance, engagement and principles.
- "Distance" covers three things: friction before starting, the long run (compounding over months), and the intervals between repetitions.
- Engagement mechanics stay soft: habit tracker, never miss twice, visible progress. No points or leaderboards.
- Atomic Habits is the conceptual basis for how a learner works with data and text in the app. The book is not loaded into the app and is not a pilot course.
- Notifications go to Telegram first; any other channel is an adapter the user enables.
- Study budget: 60 minutes a day (2026-11-06).

The book's core: four laws of behavior change, one per step of the habit loop (cue, craving, response, reward): make it obvious, attractive, easy, satisfying. Supporting ideas: identity-based habits, the two-minute rule, habit stacking, environment design, habit tracking with "never miss twice", the plateau of latent potential, and the Goldilocks rule (work at the edge of current ability).

## Decision: the four laws as product requirements

| Law | Product mechanism | Measured by |
| --- | --- | --- |
| **Obvious** (cue) | Implementation intention per course: "I study *[course]* at *[time]* in *[place]*", stored and shown as the day's cue; optional habit stack ("after *[existing habit]*"); the home screen shows exactly one next step | Share of sessions started within 30 minutes of the declared time |
| **Attractive** (craving) | Each session opens on the most engaging artefact of the lesson (a lab or a live diagram), not on reading; identity line on the course page ("I am an engineer who derives crypto from first principles"); paired programs as "a culture where the behavior is normal" (ADR-0016) | Sessions started per week |
| **Easy** (response) | One click resumes the exact cell; every session starts with a two-minute entry action (one recall card, or re-run the last lab); focus mode hides everything but the notebook and tutor | Interactions and seconds from opening the app to the first study action: ≤ 2 and ≤ 10 s |
| **Satisfying** (reward) | Session close shows what changed today (cells done, recall accuracy, findings filed); habit tracker calendar; never miss twice: after one missed day the next plan shrinks to the two-minute version and the cue is repeated | Longest run without two consecutive misses |

## Decision: the three distances

| Distance | Mechanism |
| --- | --- |
| **Friction to start** | The "Easy" row above; the friction metric is an NFR, checked in Playwright on every release |
| **The long run** | A cumulative mastery view across weeks and months (lessons published, cards mature, findings filed) so progress during the plateau of latent potential is visible even when a single day feels flat |
| **Spacing** | Recall cards scheduled by FSRS (`py-fsrs`), target retention 0.90 by default, configurable per course |

## Decision: engagement level (Goldilocks rule)

The tutor calibrates question difficulty so the learner answers about 85% correctly: hard enough to stay engaged, easy enough to keep going. This matches Wilson et al., "The Eighty Five Percent Rule for optimal learning" (Nature Communications 10, 4646, 2019), which derives an optimal training accuracy of about 85%. Accuracy above 90% for a week raises difficulty; below 80% lowers it.

## Notifications

A `Notifier` port with adapters declared as data. A user enables channels in their settings; each channel declares which events it receives (the day's cue, the never-miss-twice nudge, a partner's check-in, a lesson waiting at gate G2, a budget warning).

```yaml
notifications:
  channels:
    - kind: telegram
      bot_token: secret://keychain/reader-ai/telegram-bot-token   # one bot per instance
      events: [daily_cue, missed_day, partner_checkin, gate_waiting, budget_warning]
```

- **Telegram first.** One bot per instance; a user links their chat by opening a one-time deep link (`/start <token>`). While v1 runs on the owner's Mac with no public URL, the adapter uses Bot API long polling (`getUpdates`), not webhooks; webhooks switch on with the VPS (ADR-0012).
- **Any other source** (email, Slack, ntfy, a generic webhook) is one adapter class implementing `send(user, event)` plus its config schema; no core change.
- Delivery goes through the job queue (ADR-0006): retries with backoff, idempotency key per (user, event id, channel), quiet hours per user.

## Tutor

The tutor is bound to the current cell: its context is the cell, its citations and the learner's notes, plus retrieval over the course. It asks before it tells (diagnostic first), and every factual answer cites a source span or URL, the same rule as the fact-check stage.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Four laws mapped to measurable mechanisms; soft engagement** | Each mechanism has a metric; nothing rewards activity for its own sake | Metrics must be instrumented from v1 |
| Full gamification (XP, badges, leaderboards) | Strong short-term pull | Owner rejected it (2026-11-20); rewards the count of actions, not understanding |
| No engagement layer, metrics only | Least code | Ignores the owner's request to use the book's principles |

## Spacing algorithm

| Option | Gives | Costs |
| --- | --- | --- |
| **FSRS** | Fewer reviews than SM-2 for the same retention (Anki manual; open-spaced-repetition/srs-benchmark); retention is an explicit parameter | Needs review history to personalize; defaults work from day one |
| SM-2 | Simple, well known | More reviews for the same retention per the same benchmark |
| Fixed intervals | Trivial | Ignores per-card difficulty |

## Rejected options — why (numbers)

- **Full gamification.** Rejected by the owner; it would also add a second objective (points) that can be maximized without passing a single recall card.
- **SM-2.** On the public srs-benchmark FSRS has lower prediction error than SM-2, which translates into fewer reviews at the same target retention; at 60 minutes a day every saved review is study time.

## Resolved questions (owner, 2026-11-20)

1. The implementation intention (time, place, habit stack) is set by each learner in the app when a course starts, not in this ADR.
2. Notifications: Telegram adapter first, behind a port that any channel the user enables can implement.
