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
(`digest.catch_up_digests`), and the Thompson-sampling track bandit (`scripts/bandit.py`). R6 now
has an entry point as well: `run.py --bandit`, or `scoring.bandit.enabled` in config. Both are
explicit and neither is on by default, so the static track weight is still what a default run uses;
what changed is that the switch now has something on the other end, and a run that flips it reports
every draw it made. Whether the loop has yet turned on a production schedule is an operator
decision, not a missing mechanism.

**The hooks and CI no longer disagree about a missing guard.** `.githooks/pre-commit` and
`.githooks/pre-push` answered an absent `pii_guard.py` or `data_boundary.py` with
`[ -f "$GUARD" ] || exit 0`, a PASS, while `.github/workflows/pii-guard.yml` answered the same
question with exit 1, so deleting a scanner silently disarmed every local check and the only control
left saying so ran after the push. Closed by b24bfff (2026-08-28): both hooks now exit 1 on that
absence with a re-vendor instruction, so the two controls agree and the fail-closed one is reached
first.

## Open

**The roster pull-cap rotation still has no entry point.** `run.py` never calls
`rt.advance_rotation(roster, len(plan))` after the pull pass and never saves the roster afterwards,
so a capped roster re-plans the same window every run and the tail accrues no pulls at all. This was
one of three such gaps; the bandit switch and the three scoring keys have since been wired, and this
is what is left.

**The pre-viral prune guard cannot fire on the live archive.** It reads engagement counts that the
archive writer never persists onto evidence, so it evaluates to zero for every origin. The engine
now reports `pre_viral_guard.state == "inert"` instead of letting it read as protection, and the
pulls-log `kept` guard is what actually spares a working handle. The fix is in the writer.

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
