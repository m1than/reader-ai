# Spike ingest-confine — Docling under a deny-default `sandbox-exec` profile

**Question** (SRV-INGEST review, B1): can the native converter (ADR-0021: formula model on, code model off, OCR off) run under a deny-default macOS sandbox profile with no Mach lookups, no IOKit, no network and writes only under its work directory? At what speed?

**Answer: yes, at native speed.** Configuration D converted the same 20 pilot pages with identical counts: 6 formulas decoded and 11 tables, matching `spikes/ingest-docling` D. Confined throughput was 15.69–15.85 pages/min, against 15.85–16.57 unconfined on the same pages, within run-to-run spread. Every negative probe was denied.

## Setup

- Mac: macOS 27.2, 18 cores. CPython 3.14.10 (uv), `docling==2.151.1`, torch 2.15.0, in a dedicated venv `data/spikes/ingest-confine/venv` (gitignored).
- Models: `docling-tools models download -o data/spikes/ingest-confine/models layout tableformer code_formula`, 1.2 GB.
- Runtime: the converter loads models only from that directory through `artifacts_path`, with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.
- Device forced to CPU; `num_threads` = 18.
- Each run gets a fresh WORK directory (mode 0700) holding a copy of the PDF, its output, `HOME` and `TMPDIR`, and serving as the cwd.
- The environment is reset with `env -i` to `PATH=/usr/bin:/bin`, `HOME`, `TMPDIR` and `LANG`.
- Pages: the ingest-docling sample, 128, 130, 132, 133, 141, 143, 378, 381, 461, 468, 483, 497, 559, 592, 602, 661, 672, 700, 776, 812. It includes the 5 formula pages.
- Book text: none is committed or printed. Results are counts and seconds only (`results/runs.jsonl`).

```bash
bash spikes/ingest-confine/run.sh "<path to the Bashir PDF>" confined <tag>   # or: bare <tag>
bash spikes/ingest-confine/probe.sh                                            # negative probes, confined only
```

## Throughput (20 pages, warm, `results/runs.jsonl`)

| Run | Profile | Total s | Pages/min | Formula pages s (6 formulas) | Other 15 pages s |
| --- | --- | --- | --- | --- | --- |
| bare1 | none | 72.4 | 16.57 | 65.9 | 6.5 |
| bare2 | none | 75.7 | 15.85 | 69.1 | 6.6 |
| conf1 | intermediate | 72.2 | 16.62 | 65.6 | 6.6 |
| conf2 | intermediate | 74.4 | 16.13 | 67.8 | 6.6 |
| conf3 | **final** (`converter.sb`) | 75.7 | 15.85 | 69.0 | 6.7 |
| conf4 | **final** | 76.5 | 15.69 | 69.9 | 6.6 |

- **Model load:** 1.9–2.0 s in every run.
- **The sandbox costs nothing measurable.** The final-profile mean is 15.77 pages/min against 16.21 unconfined, a 2.7% gap inside the unconfined spread (15.85–16.57).
- **Formulas dominate:** about 11.0–11.7 s per formula. Pages without formulas take 0.2–0.7 s, tables included.
- **Cold first run:** 97.8 s total, 12.27 pages/min, outside `run.sh`. The extra time is torch's one-time compile cache, which lands in `TMPDIR`, inside WORK.
- **The sample is formula-heavy, so pages/min here is not book throughput.** Estimate for the whole book, not measured: 935 pages, of which 35 hold formula items (ingest-docling full run). About 900 × 0.4 s + 35 × 11.5 s ≈ 12.7 min.
- Faster than ingest-docling D (126.2 s for the same pages): forcing CPU instead of AUTO/MPS, and loading models from a local directory, are the visible differences. Not isolated.

## The profile (`converter.sb`)

Parameters, all passed with `-D`: `PYROOT` (the CPython install the venv links to), `VENV`, `MODELS`, `SCRIPTS` (directory of the converter script) and `WORK`.

| Rule | Why |
| --- | --- |
| `(deny default)` | Everything not listed is denied, including `mach-lookup`, `iokit-open`, `network*`, `ipc-posix*` and `signal`; none has an allow rule |
| Read and map: PYROOT, VENV, SCRIPTS, `/usr/lib`, `/System/Library`, `/System/Cryptexes`, `/private/var/db/dyld` | Interpreter, packages, dyld and system frameworks |
| Read: MODELS | Model weights |
| Read: `/dev/urandom`, `/dev/null`; write data: `/dev/null` | Python startup, subprocess plumbing |
| `file-read-metadata` everywhere | `stat` and path resolution. Metadata only, never contents |
| Read and write: WORK | Input copy, output, HOME, TMPDIR and the torch compile cache |
| `process-exec` of `VENV/bin/python` and `PYROOT/bin/python3.14` only | Nothing else can be executed |
| `process-fork`, `sysctl-read` | torch and the stdlib query CPU topology |

**Allowances added beyond the starting list, each with the failure it fixed:**

1. **Read of `/` (the root directory itself, a literal).** Without it, dyld aborts at process start (`abort_with_reason` in `dyld4::CacheFinder`, exit 134) before Python runs. Allowing all of `/System` did not help; allowing `/` alone did.
2. **Read of `/private/etc/apache2/mime.types` (a literal).** The stdlib `mimetypes` module, imported by Docling, opens every existing file in `mimetypes.knownfiles`. A sandbox `EPERM`, unlike a missing file, raises `PermissionError` and the import fails. This file is the only one present on this Mac.
3. **`process-exec` of `PYROOT/bin/python3.14`.** The venv's `bin/python` is a symlink, and the exec check applies to the resolved target. Without this, `sandbox-exec: execvp() … Operation not permitted`, exit 71. The literal replaces an earlier `subpath PYROOT` exec rule (conf1 and conf2), so no other binary in the install is executable.
4. **The cwd must be inside WORK** (`run.sh` changes into it). This is a process setting, not a rule: `os.path.abspath` calls `getcwd`, which fails with EPERM in an unreadable directory, and torch's import fails.

The starting list's `/System/Volumes/Preboot/Cryptexes` turned out unnecessary and is not in the final profile.

## Other measurements

- **POSIX semaphores and shared memory:** not needed. Both are denied (probes below), and the conversion ran with identical counts.
- **Mach lookups:** the only denials sandboxd logged during the 20-page runs were `com.apple.system.opendirectoryd.libinfo` (2) and `com.apple.system.notification_center` (1), both non-fatal: a user-name lookup and change notification.
- **No IOKit:** with the device forced to CPU, nothing needed IOKit. MPS was not tried under the profile; it would need `iokit-open` and Mach lookups for Metal, which this profile refuses on purpose.
- **Writes outside WORK:** none needed.
  - The conversion succeeded with HOME and TMPDIR inside WORK.
  - Nothing outside WORK changed: the `outside-*.txt` target stayed absent.
  - sandboxd logged no file-write denial for the converter.
  - Caveat: the unified log did not show file-read denials reliably either (a deliberate denied read of a home file produced no log line). "No denial logged" is therefore weaker evidence than the identical output.
- **Offline model load:** works. Network is denied by the profile, models come only from `artifacts_path`, and the Hugging Face offline variables are set. Model load took 1.9 s.
- **`-D` parameters must be realpaths.**
  - Measured: `WORK=/tmp/x` (a symlink path) denies a write to `/private/tmp/x/f`, while `WORK=/private/tmp/x` allows it.
  - The runners pass `pwd -P` and `readlink -f` results. A product implementation must `realpath` every parameter.
- **Where code is read from:**
  - The spike's converter is a standalone script read from `SCRIPTS`, with Docling and torch from `VENV`.
  - A product converter shipped as a module of the `reader` package needs that package's directory readable. With an editable install the `.pth` points into the repository, so the profile would have to allow `server/src` (read only), or the converter ships as a non-editable install inside the dedicated venv. Recommended: the latter.

## Negative probes, final profile (`results/probes.json`)

| Probe | Result |
| --- | --- |
| TCP connect to 127.0.0.1 | EPERM |
| TCP bind and listen on 127.0.0.1 | EPERM |
| Unix connect `~/.docker/run/docker.sock` and `/var/run/docker.sock` | EPERM |
| Unix bind inside WORK | EPERM |
| `bootstrap_look_up("com.apple.SecurityServer")` (Keychain service, name lookup only) | denied, kern_return 1100 |
| `bootstrap_look_up("com.apple.coreservices.launchservicesd")` | denied, 1100 |
| exec `/usr/bin/security`, `/usr/local/bin/docker`, `/bin/sh` | EPERM |
| signal to the parent (`kill(ppid, 0)`) | EPERM |
| open `/dev/tty` | EPERM |
| list the home directory; read a file in it; read `/etc/passwd` | EPERM |
| write outside WORK | EPERM |
| `socketpair()` | allowed: no external endpoint |
| write, symlink to an outside path, `mkfifo` inside WORK | allowed: the parent must treat WORK as untrusted (SRV-INGEST review, B2) |
| POSIX semaphore, POSIX shm | EPERM |

## Conclusion for SRV-INGEST

- `sandbox-exec` confinement is implementable for the converter at no measurable speed cost, with the profile above. The dedicated-macOS-user fallback is not needed.
- FR-INGEST-4 should name:
  - this rule set;
  - the realpath requirement;
  - the cwd in WORK;
  - HOME and TMPDIR in WORK;
  - `env -i` with the four variables;
  - CPU device;
  - models only via `artifacts_path`, plus the offline variables;
  - the converter as a non-editable install in its own venv.
- INV-INGEST-4 can adopt the probe table as its oracle.
- The symlink and FIFO results confirm the review's B2: the parent must open the converter's outputs with `O_NOFOLLOW|O_NONBLOCK` and `fstat` them as regular files.
- `sandbox-exec` is deprecated by Apple but ships and works on macOS 27.2.
