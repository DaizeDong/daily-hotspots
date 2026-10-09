# Roadmap

Current: **v0.5.0**

Release history is preserved in [CHANGELOG.md](CHANGELOG.md). This page records current
mechanisms, their validation limits and remaining work.

## Landed, and where to read about it

- The R1 to R6 design items have implementations and tests: multilingual classification fixtures,
  `score.weight_regression_gate`, adversarial dedup cases, lifecycle downweighting,
  `digest.catch_up_digests`, and the Thompson-sampling track bandit in `scripts/bandit.py`.
  `run.py --bandit` or `scoring.bandit.enabled` enables the bandit and reports its draws; both
  are off by default. An active production schedule requires separate operator evidence.
- Forwarding hooks require pinned Guards and fail when scanners are missing. The local
  fail-open behavior was closed by b24bfff on 2026-08-28. Restore missing submodules with
  `git submodule update --init --recursive`; do not vendor copies.
- `run.py --sources` freezes each roster batch, retains successful pull receipts, resumes partial
  batches under the same run ID and advances once. Regressions cover replay and conflicts;
  production collection still requires a curated PRIVATE roster and current source access.

Operational contracts are in [CONFIG.md](CONFIG.md), [DATA.md](DATA.md) and the linked skill
references. Implementation and synthetic test coverage do not establish current delivery,
source availability or business value.

## Open

**The pre-viral prune guard cannot fire on the live archive.** It reads engagement counts that the
archive writer never persists onto evidence, so it evaluates to zero for every origin. The engine
now reports `pre_viral_guard.state == "inert"` instead of letting it read as protection, and the
pulls-log `kept` guard is what actually spares a working handle. The fix is in the writer.

**One real same-story pair still does not merge.** Its two halves have disjoint curated entity sets,
so the required second signal is absent, and its character 3-gram similarity (0.131) is too close to
the unrelated-pair ceiling (0.032) to become a global single-signal threshold without inviting the
false merges the adversarial suite exists to prevent. The tractable fix is upstream: `lib`'s CJK
tokenizer emits whole clauses as single tokens.

**hardware-iot needs broader collection surfaces.** Initialization leaves every roster empty;
the generated planner fixture includes six invented hardware accounts only for tests. Reaching
hardware founders requires reviewed sources such as YouTube and vertical hardware forums.

**linux.do and V2EX are self-contained in this repo by design, for now.** market-intel does not
catalog either source, so `reference/collect.md` is their single home. Moving them into
market-intel's `reference/discovery-cn.md` as the shared definition is an audit-recommended
follow-up; doing it half way would create exactly the two-homes drift the arrangement avoids.

## Configuration and storage acceptance

Companion selection and write admission use the source contracts. Synthetic regressions cover conflicting roots, absent setup and undeclared or ignored writes. Current provider connectivity, delivery and restore success require separate operational evidence. The declared 64 MiB working-data budget remains enforced; any over-budget companion remains out of conformance until reviewed dependency and retention closure is completed. No empty template, static document check or path classification establishes readiness or authorizes deletion.
