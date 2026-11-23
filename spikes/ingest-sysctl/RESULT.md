# Spike: ingest-sysctl — closing the channel a confined converter used to learn another run's WORK

**Question (SRV-INGEST revision 2, review round 3, 2026-11-23):** the reviewer showed on this Mac, under the product
profile (`server/src/reader/ingest/host/converter.sb`), that a confined child can read a concurrent run's argv
through `sysctl kern.procargs2`, learn its WORK, and then fork, `setsid`, and rewrite its own args to become a member
of that other run under FR-INGEST-22's rule, pushing it over `memory_limit`. Which profile change closes every
channel to another process's arguments, cwd and paths, and does the converter still work under it?

**Answer:** variant **v2** (`converter-v2.sb`) closes all seven channels tried, the attack's child is no longer a
member of the other run, and SRV-INGEST's [host] and [local] converter tests pass under it, the 20 pilot pages
included. Neither half of v2 is enough alone (v3, v4), and a deny by sysctl name (the reviewer's first suggestion)
does not stop `kern.procargs2` at all.

Date: 2026-11-23, macOS 27.2, the owner's Mac. No model spend; no book text in any output.

```bash
uv run --project server python -I spikes/ingest-sysctl/run.py <data_dir> <scratch dir> spikes/ingest-sysctl/results/attack.json
cd server && READER_TEST_INGEST_HOST=1 READER_TEST_INGEST_DATA=<data_dir> READER_TEST_PILOT_PDF=<pilot.pdf> \
  uv run python -I ../spikes/ingest-sysctl/host_tests.py ../spikes/ingest-sysctl/converter-v2.sb
```

## Method

`run.py` starts, for each profile, a confined "run B" (`<VENV>/bin/python -I -c sleep … --input <WORK_B>/in/material.pdf`,
cwd WORK_B, FR-INGEST-4's argv shape) and a confined "run A" executing `probe.py` in WORK_A. WORK names are 16 random
hex, siblings under one scratch directory. The probe tries each channel against run B's pid, then forks a child that
calls `setsid()`, `chdir`s to the learned WORK (or `/`), points stdout and stderr at `/dev/null`, rewrites its args to
`x --input <learned WORK or a guess>/in/material.pdf` and sleeps. The unconfined driver then applies FR-INGEST-22's
cwd and args arms for run B to that child. The probe's own argv carries WORK_B (the harness passes it in to detect
hits), so a hit on the probe's own `kern.procargs2` is not counted as learning.

Profiles:
- **product:** today's profile, `(allow sysctl-read)` unrestricted, nothing about process-info;
- **deny-by-name:** product + `(deny sysctl-read (sysctl-name-prefix "kern.procargs") (sysctl-name "kern.proc") (sysctl-name-prefix "kern.proc."))`;
- **v3:** product with `(allow sysctl-read)` replaced by an allow-list (`hw.`, `machdep.`, `vm.`, `sysctl.`, `security.mac.`, `kern.os*`, and twelve named `kern.` entries; no `kern.proc*`);
- **v4:** product + `(deny process-info*) (allow process-info* (target self))`;
- **v2:** v3's sysctl allow-list **and** v4's process-info rules.

## Results (`results/attack.json`)

| Channel against run B | product | deny-by-name | v3 (sysctl list) | v4 (process-info) | **v2 (both)** |
| --- | --- | --- | --- | --- | --- |
| `kern.procargs2` (argv and env) | allowed, names WORK_B | allowed | allowed | allowed | **denied** |
| `kern.procargs` | allowed, names WORK_B | allowed | allowed | allowed | **denied** |
| `kern.proc.pid` | allowed | allowed | allowed | allowed | **denied** |
| `kern.proc.all` | allowed | denied | denied | allowed | **denied** |
| `proc_pidinfo` VNODEPATHINFO (cwd) | allowed, names WORK_B | allowed | allowed | denied | **denied** |
| `proc_pidpath` | allowed | allowed | allowed | denied | **denied** |
| `proc_listallpids` | allowed | allowed | allowed | denied | **denied** |
| list WORK's parent directory | denied | denied | denied | denied | denied |
| `chdir` into WORK_B (path known) | allowed | allowed | allowed | allowed | allowed |
| WORK_B learned | yes | yes | yes | yes | **no** |
| attacker's child a member of run B | **yes** (args and cwd) | yes | yes | yes | **no** |

Converter under v2 (`host_tests.py`): 5 passed in 149.9 s — the synthetic PDF (VEC-INGEST-2), the child's output never
reaching the terminal, the 20 pilot pages (VEC-INGEST-3), the pilot outline (VEC-INGEST-4) and the confinement probes
(INV-INGEST-4).

## Findings

1. **A deny by sysctl name does not stop `kern.procargs2`, `kern.procargs` or `kern.proc.pid` for another pid;** it
   stops only `kern.proc.all`. The same holds for v3's allow-list alone. These per-pid queries are refused only when
   both the sysctl allow-list and the process-info rules are present (v2). The mechanism is not identified; the
   matrix above is the evidence, and the [host] test pins it.
2. **`(deny default)` does not cover process-info:** under the product profile `proc_pidinfo`, `proc_pidpath` and
   `proc_listallpids` of another same-uid process were allowed. v4's two rules refuse them.
3. **`chdir` into another run's WORK is allowed** when its path is known (the global `file-read-metadata` allows the
   lookup), so FR-INGEST-22's cwd arm, like its args arm, is spoofable by a child that knows another WORK. With v2
   no channel tried reveals it, and WORK's parent is not listable; the residual is a WORK path leaking by a channel
   not tried here.
4. The product profile's change is therefore v2's two edits: replace `(allow sysctl-read)` by the allow-list, and
   add `(deny process-info*)` and `(allow process-info* (target self))`. It is drafted as a proposed amendment of
   SRV-INGEST FR-INGEST-4 and INV-INGEST-4; the product file changes with its Red tests after review.

## Not measured

- Throughput under v2 on its own (the five tests ran in 149.9 s together; the 20-page sample was 75.7–76.5 s under
  the product profile in spikes/ingest-confine). The full pilot run under v2.
- Whether the allow-list is minimal: it is wide enough for Python, torch and Docling on these tests; names the
  converter never reads were not pruned.
