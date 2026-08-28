# Roadmap

Current: **v0.5.0**

**Shipped history lives in [CHANGELOG.md](CHANGELOG.md), and only there.** This file used to carry a
second, hand-maintained copy of it, which is how the "Planned" list below came to be advertising six
items that had all shipped, and how the Reddit lane went on being described here as the
reddit-mcp-buddy login tier for weeks after it was replaced by arctic-shift. What stays here is what
has NOT been done.

## Landed, and where to read about it

The R1 to R6 self-evolve headroom from ARCHITECTURE section 9 is all in the tree with tests:
multilingual classify fixtures, the weight-retune regression gate (`score.weight_regression_gate`),
adversarial dedup fixtures, the lifecycle window-closed downweight, oversleep catch-up
(`digest.catch_up_digests`), and the Thompson-sampling track bandit (`scripts/bandit.py`). One
caveat carries forward into the open list below: **R6 is complete but has never run in production.**

## Open

**Three mechanisms exist but no entry point turns them on.** The bandit needs `run.py`'s CLI to call
`process(..., persist_bandit=bandit.bandit_enabled(cfg))`; until then `scoring.bandit.enabled` is a
switch with nothing on the other end and every run uses the static track weight. The roster pull-cap
rotation needs `run.py` to call `rt.advance_rotation(roster, len(plan))` and save the roster after
the pull pass; until then a capped roster re-plans the same window every run, and the tail accrues
no pulls at all. And `lib.DEFAULT_CONFIG` ships `crowdedness_mode`, `crowdedness_blend` and
`demand_freshness_mode`, which `score.py` does not read, so the demand-parity retune those keys
describe is not in force.

**The pre-viral prune guard cannot fire on the live archive.** It reads engagement counts that the
archive writer never persists onto evidence, so it evaluates to zero for every origin. The engine
now reports `pre_viral_guard.state == "inert"` instead of letting it read as protection, and the
pulls-log `kept` guard is what actually spares a working handle. The fix is in the writer.

**`verify_gate.gate_batch` returns no `below_floor` list**, so the coverage line honestly prints
`未达门槛 未统计` every run. Having the gate return the cards it dropped for score would turn that
into a real number and let the digest list them.

**One real same-story pair still does not merge.** Its two halves have disjoint curated entity sets,
so the required second signal is absent, and its character 3-gram similarity (0.131) is too close to
the unrelated-pair ceiling (0.032) to become a global single-signal threshold without inviting the
false merges the adversarial suite exists to prevent. The tractable fix is upstream: `lib`'s CJK
tokenizer emits whole clauses as single tokens.

**hardware-iot needs a surface an X roster cannot provide.** Six handles are seeded and the track
works, but reaching hardware founders properly means YouTube and vertical hardware forums.

**linux.do and V2EX are self-contained in this repo by design, for now.** market-intel does not
catalog either source, so `reference/collect.md` is their single home. Moving them into
market-intel's `reference/discovery-cn.md` as the shared definition is an audit-recommended
follow-up; doing it half way would create exactly the two-homes drift the arrangement avoids.

**The vendored hooks and CI disagree about a missing guard.** CI treats an absent `pii_guard.py` or
`data_boundary.py` as an error; `.githooks/pre-commit` and `.githooks/pre-push` treat the same
absence as a pass (`[ -f "$GUARD" ] || exit 0`). Which way to resolve it is the operator's call, but
two controls answering the same question in opposite directions is not a resting state.
