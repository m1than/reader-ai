# Definition of Done

Commit 1635505. Report exit code 0. FAIL lines in live.log that no operation maps: 0.

| Operation | Gate | Status | functional | security | privacy | a11y | latency | cost | observability | recovery |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| auth.bootstrap — Register the first passkey from a one-time bootstrap link | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | PASS | PASS |
| auth.signin — Sign in with a passkey | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | PASS | PASS |
| auth.signout — Sign out | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | MISSING | PASS |
| auth.add_passkey — Add another passkey to my account (recent session) | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | PASS | PASS |
| material.ingest — Add a material to reader.yaml and ingest it (reader-ingest, confined native converter, import job) | tracked | MISSING | MISSING | MISSING | MISSING | NA | MISSING | NA | MISSING | PASS |
| ingest.setup — Prepare the converter venv and models (reader-ingest --setup) | tracked | MISSING | PASS | PASS | MISSING | NA | MISSING | NA | MISSING | PASS |
| ingest.requeue — Requeue a dead import (reader-ingest --requeue) | tracked | MISSING | PASS | PASS | MISSING | NA | MISSING | NA | PASS | PASS |
| material.browse — Browse my materials (list, outline, spans of a page) | tracked | MISSING | PASS | PASS | MISSING | NA | MISSING | NA | MISSING | PASS |
| search.query — Search the owner's materials (hybrid dense + full text) | tracked | MISSING | PASS | PASS | MISSING | NA | MISSING | NA | MISSING | PASS |
| lesson.list — List my lessons | tracked | MISSING | PASS | PASS | MISSING | MISSING | MISSING | NA | MISSING | PASS |
| lesson.open — Open a lesson and read it (markdown, math, code, citations, open findings) | tracked | MISSING | PASS | PASS | MISSING | MISSING | MISSING | NA | MISSING | PASS |
| cell.run — Run a lab cell | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | MISSING | PASS |
| cell.interrupt — Interrupt a running cell | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | MISSING | PASS |
| cell.reset — Reset the notebook kernel (fresh container) | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | MISSING | PASS |
| cell.run_all — Run all cells of a lesson | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | MISSING | PASS |
| course.import — Import a course plan (gate G1) | tracked | MISSING | PASS | PASS | MISSING | NA | MISSING | NA | MISSING | PASS |
| lesson.compile — Compile a lesson with agents (draft, fact-check, review, revise, labs, G2 report) | tracked | MISSING | PASS | MISSING | PASS | NA | MISSING | MISSING | PASS | PASS |
| lesson.compile_status — Watch a compile run (state, stages, spend) | tracked | MISSING | PASS | PASS | PASS | NA | MISSING | NA | PASS | PASS |
| lesson.compile_resume — Resume a compile run with a dead step | tracked | MISSING | PASS | PASS | PASS | NA | MISSING | MISSING | PASS | PASS |
| lesson.findings — Read open findings and the G2 report of a compiled lesson | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | MISSING | MISSING |
| tutor.ask — Ask the tutor about the open lesson | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | MISSING | PASS | PASS |
| tutor.history — Read a tutor answer and my earlier questions on a lesson | tracked | MISSING | PASS | PASS | PASS | MISSING | MISSING | NA | PASS | PASS |
| stack.backup — Back up the stack (reader backup DIR) | tracked | MISSING | MISSING | MISSING | PASS | NA | MISSING | NA | PASS | PASS |
| stack.restore — Restore the stack from a backup (reader restore DIR) | tracked | MISSING | MISSING | PASS | PASS | NA | MISSING | NA | PASS | MISSING |
| stack.up — Start the stack (reader up) | tracked | MISSING | PASS | MISSING | PASS | NA | MISSING | NA | PASS | PASS |
| stack.down — Stop the stack (reader down, reader watcher stop) | tracked | MISSING | PASS | PASS | PASS | NA | MISSING | NA | PASS | PASS |
| stack.status — Show stack health (reader status, reader watcher status) | tracked | MISSING | PASS | PASS | PASS | NA | MISSING | NA | PASS | PASS |
| stack.doctor — Check the host setup (reader doctor) | tracked | MISSING | PASS | PASS | PASS | NA | MISSING | NA | PASS | PASS |
| stack.trust — Trust or untrust the local TLS root (reader trust, reader down --untrust) | tracked | MISSING | PASS | PASS | PASS | NA | MISSING | NA | PASS | MISSING |
| secrets.set — Store a secret in the Keychain (reader secrets set NAME) | tracked | MISSING | PASS | PASS | PASS | NA | MISSING | NA | PASS | PASS |

## Cost and latency

| Operation | n | p50 ms | p95 ms | Latency budget | Runs | USD max per run | USD mean per run | Cache-read share | Cost budget |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| auth.bootstrap | 0 | – | – | – | 0 | – | – | – | – |
| auth.signin | 0 | – | – | – | 0 | – | – | – | – |
| auth.signout | 0 | – | – | – | 0 | – | – | – | – |
| auth.add_passkey | 0 | – | – | – | 0 | – | – | – | – |
| material.ingest | 0 | – | – | p50 ≤ 6000 ms per page; n ≥ 20; target; ADR-0021 revisit_when ≥ 10 pages/min = 6,000 ms/page (NFR-INGEST-1); measured 15.69–15.85 pages/min confined (spikes/ingest-confine) | 0 | – | – | – | – |
| ingest.setup | 0 | – | – | – | 0 | – | – | – | – |
| ingest.requeue | 0 | – | – | – | 0 | – | – | – | – |
| material.browse | 0 | – | – | – | 0 | – | – | – | – |
| search.query | 0 | – | – | p95 ≤ 30000 ms; n ≥ 40; baseline; SRV-SEARCH query call timeout 30 s (spec/modules/SRV-SEARCH.yaml:141: worst batch of 8 = 13.29 s + query 0.93 s, about 2× margin; spikes/embed-service); a hard deadline, so a baseline, not a target | 0 | – | – | – | – |
| lesson.list | 0 | – | – | – | 0 | – | – | – | – |
| lesson.open | 0 | – | – | – | 0 | – | – | – | – |
| cell.run | 0 | – | – | p95 ≤ 500 ms; n ≥ 30; target; NFR-KERNEL-1, roadmap SRV-KERNEL row; reviewer measured 47 ms median, 51 ms p95 over 30 runs direct to the gateway | 0 | – | – | – | – |
| cell.interrupt | 0 | – | – | – | 0 | – | – | – | – |
| cell.reset | 0 | – | – | p95 ≤ 30000 ms; n ≥ 3; baseline; FR-KERNEL-5 reset deadline (a hard deadline, so a baseline); measured 1.12–1.20 s round trips over 3 resets (docs/progress.yaml:541, OPS-SANDBOX) | 0 | – | – | – | – |
| cell.run_all | 0 | – | – | – | 0 | – | – | – | – |
| course.import | 0 | – | – | – | 0 | – | – | – | – |
| lesson.compile | 0 | – | – | p95 ≤ 2700000 ms; n ≥ 1; target; NFR-PIPELINE-1, 45 min per lesson (ADR-0005 / spikes/compile-cost pass criterion); measured 8.3–9.4 min for L02. n_min 1: each sample costs ≈ $2.4–3.0 (structural) | 0 | – | – | – | ≤ $4.350000 per run; n ≥ 1; target; ADR-0005 per-lesson target ($100 for 23 lessons); the SRV-PIPELINE run cap is $8.00 (bounds.run_cap_usd, a declared deviation); measured $2.42–2.97 for L02 |
| lesson.compile_status | 0 | – | – | – | 0 | – | – | – | – |
| lesson.compile_resume | 0 | – | – | – | 0 | – | – | – | ≤ $8.000000 per run; n ≥ 1; baseline; SRV-PIPELINE bounds.run_cap_usd: a resume requeues the dead step of the same run, which keeps its stored cap_usd (FR-PIPELINE-11, FR-AGENT-9); a hard cap, so a baseline |
| lesson.findings | 0 | – | – | – | 0 | – | – | – | – |
| tutor.ask | 0 | – | – | p95 ≤ 30000 ms; n ≥ 20; target; NFR-TUTOR-1 (20 questions on L02, idle worker) | 0 | – | – | – | ≤ $0.300000 per run; n ≥ 20; baseline; SRV-TUTOR bounds.question_cap_usd = 0.30 (derived by VEC-TUTOR-3: attempts_max × worst-case reserve), a hard cap, so a baseline; NFR-TUTOR-2 expects a mean of about $0.048 |
| tutor.history | 0 | – | – | – | 0 | – | – | – | – |
| stack.backup | 0 | – | – | – | 0 | – | – | – | – |
| stack.restore | 0 | – | – | – | 0 | – | – | – | – |
| stack.up | 0 | – | – | p95 ≤ 180000 ms; n ≥ 1; baseline; FR-CLI-10 health deadline (a hard deadline, so a baseline; NFR-DEPLOY-1 120 s plus initdb) | 0 | – | – | – | – |
| stack.down | 0 | – | – | – | 0 | – | – | – | – |
| stack.status | 0 | – | – | – | 0 | – | – | – | – |
| stack.doctor | 0 | – | – | – | 0 | – | – | – | – |
| stack.trust | 0 | – | – | – | 0 | – | – | – | – |
| secrets.set | 0 | – | – | – | 0 | – | – | – | – |

## Missing

### auth.bootstrap

- auth.bootstrap × a11y — no axe run on /api/auth/bootstrap (the browser suite scans only / and the lesson page)
- auth.bootstrap × latency — no sourced budget for the bootstrap ceremony; record timings in the live suite

### auth.signin

- auth.signin × a11y — no axe run on /api/auth/signin (the browser suite scans only / and the lesson page)
- auth.signin × latency — no sourced budget for the sign-in ceremony; record timings in the live suite

### auth.signout

- auth.signout × a11y — no evidence in this run for page /
- auth.signout × latency — no sourced budget
- auth.signout × observability — no spec requires a log line for sign-out (FR-AUTH-18 covers failures only)

### auth.add_passkey

- auth.add_passkey × a11y — no add-passkey page in the slice (api only); a page and its axe run come with stage 4b
- auth.add_passkey × latency — no sourced budget

### material.ingest

- material.ingest × functional — no evidence in this run for item FR-INGEST-5
- material.ingest × functional — no evidence in this run for item FR-INGEST-7
- material.ingest × functional — no evidence in this run for item FR-INGEST-8
- material.ingest × functional — no evidence in this run for item VEC-INGEST-2
- material.ingest × functional — no evidence in this run for item VEC-INGEST-3
- material.ingest × security — no evidence in this run for item INV-INGEST-4
- material.ingest × privacy — no evidence in this run for item INV-INGEST-5
- material.ingest × latency — no evidence in this run for budget latency
- material.ingest × observability — no evidence in this run for item FR-INGEST-5

### ingest.setup

- ingest.setup × privacy — no canary check for the setup step's output
- ingest.setup × latency — no sourced budget (a multi-GB download, network dependent)
- ingest.setup × observability — no spec requires a log line for setup

### ingest.requeue

- ingest.requeue × privacy — no evidence in this run for item INV-INGEST-5
- ingest.requeue × latency — no sourced budget

### material.browse

- material.browse × privacy — canary: span text never reaches api logs (INV-INGEST-5's [db] test reads a spans page that does not hold the canary span; a read of that span's page under caplog is needed)
- material.browse × latency — no sourced budget
- material.browse × observability — no spec requires a log line for material reads

### search.query

- search.query × privacy — no evidence in this run for item INV-SEARCH-5
- search.query × latency — no evidence in this run for budget latency
- search.query × observability — FR-SEARCH-8 requires the line; no test asserts it is emitted

### lesson.list

- lesson.list × privacy — canary: lesson titles and text never reach api logs
- lesson.list × a11y — no evidence in this run for page /
- lesson.list × latency — no sourced budget (NFR-UI-1 bounds bundle bytes, not time)
- lesson.list × observability — no spec requires a log line for lesson reads

### lesson.open

- lesson.open × privacy — canary: lesson cell text never reaches api logs
- lesson.open × a11y — no evidence in this run for page /lessons/{lesson_id}
- lesson.open × latency — no sourced budget for time to first render of a lesson
- lesson.open × observability — no spec requires a log line for lesson reads

### cell.run

- cell.run × a11y — no evidence in this run for page /lessons/{lesson_id}
- cell.run × latency — no evidence in this run for budget latency
- cell.run × observability — no spec requires a log line for this kernel action (INV-KERNEL-6 limits what kernel logs may hold: a privacy item)

### cell.interrupt

- cell.interrupt × a11y — no evidence in this run for page /lessons/{lesson_id}
- cell.interrupt × latency — no sourced budget for interrupt-to-idle
- cell.interrupt × observability — no spec requires a log line for this kernel action (INV-KERNEL-6 limits what kernel logs may hold: a privacy item)

### cell.reset

- cell.reset × a11y — no evidence in this run for page /lessons/{lesson_id}
- cell.reset × latency — no evidence in this run for budget latency
- cell.reset × observability — no spec requires a log line for this kernel action (INV-KERNEL-6 limits what kernel logs may hold: a privacy item)

### cell.run_all

- cell.run_all × a11y — no evidence in this run for page /lessons/{lesson_id}
- cell.run_all × latency — no sourced budget (bounded per cell by FR-KERNEL-10's 60 s timeout)
- cell.run_all × observability — no spec requires a log line for this kernel action (INV-KERNEL-6 limits what kernel logs may hold: a privacy item)

### course.import

- course.import × privacy — canary: course titles never reach api logs
- course.import × latency — no sourced budget
- course.import × observability — no spec requires a log line for course import

### lesson.compile

- lesson.compile × security — no evidence in this run for item INV-AGENT-1
- lesson.compile × latency — no evidence in this run for budget latency
- lesson.compile × cost — no evidence in this run for budget cost

### lesson.compile_status

- lesson.compile_status × latency — no sourced budget

### lesson.compile_resume

- lesson.compile_resume × latency — no sourced budget
- lesson.compile_resume × cost — no evidence in this run for budget cost

### lesson.findings

- lesson.findings × a11y — no evidence in this run for page /lessons/{lesson_id}
- lesson.findings × latency — no sourced budget
- lesson.findings × observability — no spec requires a log line for reading the G2 report
- lesson.findings × recovery — no test asserts the status routes' answer while the G2 report is absent (run running, stalled or stage_failed: g2 null, FR-PIPELINE-11)

### tutor.ask

- tutor.ask × a11y — no evidence in this run for page /lessons/{lesson_id}
- tutor.ask × latency — no evidence in this run for budget latency
- tutor.ask × cost — no evidence in this run for budget cost

### tutor.history

- tutor.history × a11y — no evidence in this run for page /lessons/{lesson_id}
- tutor.history × latency — no sourced budget

### stack.backup

- stack.backup × functional — live label 'stage-1: reader backup' runs only under --drill, disabled until S1.OPS-CLI-restore-seed
- stack.backup × security — live label 'stage-1: backup files are 0600' runs only under --drill (disabled)
- stack.backup × latency — no sourced budget (FR-CLI-17 bounds pg_dump at 600 s, a hard deadline)

### stack.restore

- stack.restore × functional — drill labels (restore onto a fresh stack, restored passkey) disabled until S1.OPS-CLI-restore-seed
- stack.restore × latency — no sourced budget (FR-CLI-18 bounds pg_restore at 600 s, a hard deadline)
- stack.restore × recovery — the backup-restore drill is disabled until S1.OPS-CLI-restore-seed

### stack.up

- stack.up × security — no evidence in this run for item FR-DEPLOY-9
- stack.up × latency — no evidence in this run for budget latency

### stack.down

- stack.down × latency — no sourced budget (watcher SIGKILL at 15 s is the only bound)

### stack.status

- stack.status × latency — no sourced budget

### stack.doctor

- stack.doctor × latency — no sourced budget

### stack.trust

- stack.trust × latency — no sourced budget (owner-interactive)
- stack.trust × recovery — no live check of untrust verification (owner-run, ADR-0024)

### secrets.set

- secrets.set × latency — no sourced budget (owner-interactive)
