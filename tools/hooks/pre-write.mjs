#!/usr/bin/env node
// Claude Code PreToolUse hook for Write / Edit / MultiEdit (ADR-0001).
// Exit 2 blocks the call; stderr is returned to the agent as the reason.
//   H1 spec-first: an implementation file is writable only if an approved/implemented
//      spec claims it through `paths` (spikes/ is outside the gate);
//   H2 a test traced to an approved spec changes only together with the spec (P7);
//   H3 owner gates (ADR-0019): spec `implemented` and any ADR decision; the agent may approve specs;
//   H4 waivers are the owner's (OPS-DOD FR-DOD-12): a write of the Definition-of-Done index that adds or changes
//      a `waived` cell is refused.
// Bash writes are invisible to this hook; CI (tools/trace) closes that gap.
import { readFileSync, existsSync, realpathSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, relative, resolve, sep } from 'node:path';

// Without its dependencies the hook must not silently allow everything: fail closed.
const lib = await import('../lib/repo.mjs').catch(() => {
  process.stderr.write('[hook] tooling dependencies are missing: run `npm ci --prefix tools`.\n');
  process.exit(2);
});
const {
  isImplFile, isTestFile, isSpecFile, isAdrFile, traceIds, statusOf,
  parseAdr, loadRepo, claimsOf, LOCKED_SPEC_STATUSES,
} = lib;

const event = JSON.parse(readFileSync(0, 'utf8') || '{}');
const input = event.tool_input ?? {};
const projectRoot = resolve(process.env.CLAUDE_PROJECT_DIR ?? event.cwd ?? '.');
const abs = resolve(projectRoot, input.file_path ?? '');

// Every git worktree of this repository is checked against its own top level, so the gates hold in sibling
// worktrees too (before, `../reader-ai-x/…` matched no pattern and every gate was skipped). Files outside any
// checkout of this repository (scratchpad, memory, other repositories) are not gated.
const git = (cwd, ...args) =>
  execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
const nearestDir = (p) => {
  let d = dirname(p);
  while (!existsSync(d) && d !== dirname(d)) d = dirname(d);
  return d;
};
const commonDir = (cwd) => realpathSync(resolve(cwd, git(cwd, 'rev-parse', '--git-common-dir')));

let root = projectRoot;
let target = abs;
let foreign = false;
try {
  const dir = nearestDir(abs);
  let top = null;
  try { top = git(dir, 'rev-parse', '--show-toplevel'); } catch { top = null; }
  if (top === null) {
    foreign = true; // not inside any git checkout
  } else if (commonDir(dir) !== commonDir(projectRoot)) {
    foreign = true; // another repository
  } else {
    root = top;
    // git reports real paths (/private/var on macOS); compare against the real path of the target too.
    target = resolve(realpathSync(dir), relative(dir, abs));
  }
} catch {
  process.stderr.write('[hook] cannot resolve the git checkout of this file; failing closed.\n');
  process.exit(2);
}
if (foreign) process.exit(0);
const rel = relative(root, target).split(sep).join('/');

const current = existsSync(abs) ? readFileSync(abs, 'utf8') : '';
const incoming = [
  input.content,
  input.new_string,
  ...(input.edits ?? []).map((e) => e.new_string),
].filter(Boolean).join('\n');

const dirty = () => {
  try {
    return execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' })
      .split('\n').filter(Boolean).map((line) => line.slice(3).trim());
  } catch { return []; }
};

// ADR-0019: the agent may approve specs; `implemented` stays the owner's gate after the live check.
const SPEC_GATE = /^status:\s*implemented\b/m;
const ADR_GATE = /^status:\s*(accepted|rejected|superseded)\b/m;
const adrStatus = (text) => parseAdr(rel, text).meta?.status;

const checks = [
  {
    id: 'H1',
    when: () => isImplFile(rel)
      && !claimsOf(loadRepo(root).specs, rel)
        .some((spec) => LOCKED_SPEC_STATUSES.includes(spec.doc.status)),
    why: () => `${rel} is an implementation file and no approved spec claims it (ADR-0001, spec-first). `
      + 'Draft or extend a spec whose `paths` cover this file, review it against its ADRs, set '
      + '`status: approved` with `approved_by: agent` (ADR-0019), then write failing tests. '
      + 'Throwaway measurements go to spikes/.',
  },
  {
    id: 'H2',
    when: () => {
      if (!isTestFile(rel)) return false;
      const { ownerOf } = loadRepo(root);
      const locked = [...new Set(traceIds(current + '\n' + incoming).map((id) => ownerOf.get(id)))]
        .filter((spec) => spec && LOCKED_SPEC_STATUSES.includes(spec.doc.status));
      const changed = dirty();
      return locked.length > 0 && !locked.some((spec) => changed.includes(spec.file));
    },
    why: () => `${rel} is traced to an approved spec. Tests must not change unless the spec changes (specd P7): `
      + 'edit the spec first (the requirement the test checks), then the test.',
  },
  {
    id: 'H3',
    when: () =>
      (isSpecFile(rel) && SPEC_GATE.test(incoming)
        && (current ? statusOf(current) : undefined) !== 'implemented')
      || (isAdrFile(rel) && ADR_GATE.test(incoming)
        && adrStatus(current) !== incoming.match(ADR_GATE)[1]),
    why: () => `${rel}: \`implemented\` (spec) and ADR decisions are owner gates (ADR-0019). `
      + 'Leave the status as is, record the request in docs/progress.yaml and notify the owner.',
  },
];

// H4: compare the sets of waived (operation, criterion) cells before and after the write. When either text does
// not parse, fall back to counting `waived:` keys, so an unparsable write cannot slip a waiver through.
const dod = await import('../dod/index.mjs').catch(() => {
  process.stderr.write('[hook] tools/dod/index.mjs cannot load: run `npm ci --prefix tools`.\n');
  process.exit(2);
});
checks.push({
  id: 'H4',
  when: () => {
    if (rel !== dod.INDEX_FILE) return false;
    const after = dod.nextText(current, input);
    const [a, b] = [dod.parseIndex(current), dod.parseIndex(after)];
    if (a.error || b.error) return (after.match(/\bwaived:/g) ?? []).length > (current.match(/\bwaived:/g) ?? []).length;
    return dod.waiverViolations(a.index, b.index).length > 0;
  },
  why: () => `${rel}: waivers are the owner's (OPS-DOD FR-DOD-12). Do not add, move or change a \`waived\` cell; `
    + 'keep the gap as `missing`, record the request in docs/progress.yaml and notify the owner.',
});

const failed = checks.find((check) => check.when());
if (failed) {
  process.stderr.write(`[${failed.id}] ${failed.why()}\n`);
  process.exit(2);
}
process.exit(0);
