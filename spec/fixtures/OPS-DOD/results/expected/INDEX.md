# Definition of Done

Commit abc1234. Report exit code 1. FAIL lines in live.log that no operation maps: 2.

| Operation | Gate | Status | functional | security | privacy | a11y | latency | cost | observability | recovery |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| lesson.read — Read a lesson \| with a pipe | enforced | FAIL | PASS | FAIL | MISSING | PASS | PASS | NA | PASS | MISSING |
| tutor.ask — Ask the tutor | tracked | FAIL | PASS | MISSING | FAIL | FAIL | MISSING | PASS | MISSING | FAIL |
| stack.up — Start the stack | enforced | FAIL | MISSING | WAIVED | MISSING | NA | PASS | FAIL | MISSING | MISSING |

## Cost and latency

| Operation | n | p50 ms | p95 ms | Latency budget | Runs | USD max per run | USD mean per run | Cache-read share | Cost budget |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| lesson.read | 3 | 60 | 70 | p95 ≤ 100 ms; n ≥ 3; target; structural: test | 0 | – | – | – | – |
| tutor.ask | 2 | 12000 | 15000 | p95 ≤ 30000 ms; n ≥ 5; target; NFR-X-1 | 2 | 0.250000 | 0.175000 | 0.4149 | ≤ $0.300000 per run; n ≥ 2; baseline; VEC-X-1 |
| stack.up | 2 | 900 | 1200 | p50 ≤ 1000 ms per page; n ≥ 2; target; structural: test | 1 | 0.000001 | 0.000001 | 0.0000 | – |

## Missing

### lesson.read

- lesson.read × privacy — no evidence in this run for item FR-X-3
- lesson.read × recovery — no recovery test

### tutor.ask

- tutor.ask × security — no evidence in this run for label "stage-1: sign-in options"
- tutor.ask × latency — no evidence in this run for budget latency
- tutor.ask × observability — no evidence in this run for item FR-X-6

### stack.up

- stack.up × functional — no evidence in this run for label "stage-1: all services healthy (four default plus kernels), watcher running"
- stack.up × privacy — no evidence in this run for item FR-X-7
- stack.up × observability — no evidence in this run for item FR-X-6
- stack.up × recovery — drill disabled
