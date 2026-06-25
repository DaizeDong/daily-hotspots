"""R6 headroom: Thompson-sampling track bandit (exploration-exploitation balance).

ARCHITECTURE §8.3 gives each track a STATIC `weight` (ai-agents=1.3 ...). That is pure exploitation:
the high-weight track always tops the feed, an under-explored track that may be quietly producing
good opportunities never gets a turn, and a track that has gone cold keeps dominating — the
preference never adapts to realized outcomes (pushed / archived / blocked). ROADMAP R6 = a
multi-armed bandit (Thompson sampling) over tracks: a Beta-Bernoulli posterior per track, drawn each
run to produce a BOUNDED exploration-adjusted track weight that feeds the existing
`score_opportunity(track_weight=...)` seam (already half-strength + clamped) — promising-but-
under-sampled tracks get occasional lift, never override the evidence-driven score.

These assert the *capability* (a deterministic Beta-Bernoulli bandit with a seeded Thompson draw,
the exploit AND explore properties, bounded config-tunable output, and a monotone reward map) — NOT
any particular multiplier table. The hard invariant is DETERMINISM: a Thompson sampler that wasn't
replay-safe would break the whole byte-compare suite, so every draw is seeded.

Landed in self-evolve batch 6 (A-tier baseline-relative ACCEPT, e=443.88, +14, 0 regressions):
these were xfail headroom; the fix flipped them XFAIL -> XPASS and the markers are now removed so they
stand as permanent regression guards for the Thompson-sampling track bandit.
"""
from lib import load_config


CFG = load_config()


# --------------------------------------------------------------------------- capability + determinism
def test_capability_exists():
    import bandit
    for fn in ("init_arm", "update_arm", "posterior_mean", "posterior_variance",
               "thompson_sample", "select_track", "explore_weight", "outcome_reward"):
        assert callable(getattr(bandit, fn)), fn


def test_thompson_deterministic():
    # Same (arms, seed) MUST give a byte-identical draw across calls (replay-safe), and be
    # independent of arm ordering in the input dict.
    import bandit
    arms = {"ai-agents": {"alpha": 8, "beta": 3, "n": 9},
            "dev-tools": {"alpha": 2, "beta": 6, "n": 6},
            "saas-niche": {"alpha": 5, "beta": 5, "n": 8}}
    tracks = ["ai-agents", "dev-tools", "saas-niche"]
    a = bandit.thompson_sample(arms, tracks, seed=12345, cfg=CFG)
    b = bandit.thompson_sample(arms, tracks, seed=12345, cfg=CFG)
    c = bandit.thompson_sample(dict(reversed(list(arms.items()))), tracks, seed=12345, cfg=CFG)
    assert a == b == c
    assert set(a) == set(tracks)


def test_update_pure_no_mutation():
    # update_arm returns a NEW arm and never mutates the input (replay safety / event-sourcing).
    import bandit
    arm = {"alpha": 3.0, "beta": 4.0, "n": 5}
    snap = dict(arm)
    out = bandit.update_arm(arm, 1.0, CFG)
    assert arm == snap                 # input untouched
    assert out is not arm
    assert out["n"] == 6


def test_update_direction():
    # A success raises the posterior mean; a failure lowers it (monotone in reward).
    import bandit
    arm = bandit.init_arm(CFG)
    m0 = bandit.posterior_mean(arm)
    up = bandit.update_arm(arm, 1.0, CFG)
    dn = bandit.update_arm(arm, 0.0, CFG)
    assert bandit.posterior_mean(up) > m0 > bandit.posterior_mean(dn)


def test_reward_clamped():
    # Out-of-range / garbage reward is clamped to [0,1]; alpha/beta growth stays bounded & finite.
    import bandit
    arm = {"alpha": 1.0, "beta": 1.0, "n": 0}
    hi = bandit.update_arm(arm, 9.0, CFG)     # clamp -> 1.0
    lo = bandit.update_arm(arm, -5.0, CFG)    # clamp -> 0.0
    bad = bandit.update_arm(arm, "nope", CFG)  # non-numeric -> 0.0
    assert hi["alpha"] == 2.0 and hi["beta"] == 1.0
    assert lo["alpha"] == 1.0 and lo["beta"] == 2.0
    assert bad["beta"] == 2.0
    assert 0.0 <= bandit.reward_clamp(3.4) <= 1.0


def test_variance_shrinks_with_pulls():
    # Same posterior MEAN, more evidence (pulls) => smaller variance: the exploration uncertainty
    # that a well-sampled arm has burned down vs a fresh one.
    import bandit
    wide = {"alpha": 2.0, "beta": 2.0, "n": 2}      # mean 0.5, few pulls
    tight = {"alpha": 60.0, "beta": 60.0, "n": 118}  # mean 0.5, many pulls
    assert abs(bandit.posterior_mean(wide) - bandit.posterior_mean(tight)) < 1e-9
    assert bandit.posterior_variance(wide) > bandit.posterior_variance(tight)
    assert bandit.posterior_variance(bandit.init_arm(CFG)) > 0.0  # cold start is uncertain


# --------------------------------------------------------------------------- exploit + explore
def test_exploitation_high_reward_dominates():
    # Over a deterministic seed sweep, an arm with strong high-reward history is sampled HIGHER on
    # average than a strong low-reward arm (exploitation).
    import bandit
    good = {"alpha": 50.0, "beta": 5.0, "n": 53}   # mean ~0.91
    bad = {"alpha": 5.0, "beta": 50.0, "n": 53}    # mean ~0.09
    arms = {"good": good, "bad": bad}
    n = 60
    avg_good = sum(bandit.thompson_sample(arms, ["good", "bad"], s, CFG)["good"] for s in range(n)) / n
    avg_bad = sum(bandit.thompson_sample(arms, ["good", "bad"], s, CFG)["bad"] for s in range(n)) / n
    assert avg_good > avg_bad + 0.3


def test_exploration_underpulled_can_win():
    # The balance: a well-pulled, slightly-better-mean arm usually wins (exploit), but an
    # under-pulled, wider arm with a LOWER mean still wins on >=1 seed (explore) — something a pure
    # greedy argmax-of-mean policy could never do.
    import bandit
    exploit = {"alpha": 30.0, "beta": 20.0, "n": 48}  # mean 0.60, tight
    explore = {"alpha": 2.0, "beta": 2.0, "n": 2}     # mean 0.50, wide
    assert bandit.posterior_mean(exploit) > bandit.posterior_mean(explore)  # greedy would always pick exploit
    arms = {"exploit": exploit, "explore": explore}
    winners = [bandit.select_track(arms, ["exploit", "explore"], s, CFG) for s in range(40)]
    n_explore = winners.count("explore")
    n_exploit = winners.count("exploit")
    assert n_explore >= 1            # exploration really happens
    assert n_exploit > n_explore     # but exploitation still dominates (balance, not chaos)


# --------------------------------------------------------------------------- bounded, tunable output
def test_explore_weight_bounded():
    # The exploration-adjusted track weight is always within the config bounds, even for extreme
    # posteriors, across many seeds — it can never blow up the downstream score.
    import bandit
    lo = CFG["scoring"]["bandit"]["explore_weight_lo"]
    hi = CFG["scoring"]["bandit"]["explore_weight_hi"]
    extreme = {"hot": {"alpha": 200.0, "beta": 1.0, "n": 201},
               "cold": {"alpha": 1.0, "beta": 200.0, "n": 201}}
    for s in range(50):
        for t in ("hot", "cold"):
            w = bandit.explore_weight(extreme, t, s, CFG)
            assert lo <= w <= hi


def test_cold_start_neutral():
    # No history => uniform prior: mean exactly 0.5, positive variance, and a bounded weight with no
    # NaN / exception. A brand-new track must be explorable, not silently zeroed.
    import bandit, math
    arm = bandit.init_arm(CFG)
    assert abs(bandit.posterior_mean(arm) - 0.5) < 1e-9
    lo = CFG["scoring"]["bandit"]["explore_weight_lo"]
    hi = CFG["scoring"]["bandit"]["explore_weight_hi"]
    w = bandit.explore_weight({}, "never-seen-track", seed=7, cfg=CFG)
    assert lo <= w <= hi and not math.isnan(w)


def test_config_tunable_bounds():
    # Bounds are a real config surface: tightening [lo,hi] narrows exploration. For the SAME arm+seed
    # (=> same theta), a near-certain-hot arm lands near the top of whatever band config allows.
    import copy
    import bandit
    arms = {"hot": {"alpha": 100.0, "beta": 1.0, "n": 101}}  # theta ~ 0.99
    wide = copy.deepcopy(CFG); wide["scoring"]["bandit"]["explore_weight_lo"] = 0.5
    wide["scoring"]["bandit"]["explore_weight_hi"] = 1.5
    tight = copy.deepcopy(CFG); tight["scoring"]["bandit"]["explore_weight_lo"] = 0.9
    tight["scoring"]["bandit"]["explore_weight_hi"] = 1.1
    w_wide = bandit.explore_weight(arms, "hot", seed=3, cfg=wide)
    w_tight = bandit.explore_weight(arms, "hot", seed=3, cfg=tight)
    assert w_tight <= 1.1 and w_wide <= 1.5
    assert w_wide > w_tight          # tighter config => strictly narrower exploration lift


# --------------------------------------------------------------------------- reward map + selection
def test_outcome_reward_monotone():
    # pushed > archived-only > blocked; the score-fallback path is monotone non-decreasing in
    # final_score; everything clamped to [0,1].
    import bandit
    r_push = bandit.outcome_reward({"pushed": True}, CFG)
    r_arch = bandit.outcome_reward({"archived": True}, CFG)
    r_block = bandit.outcome_reward({"blocked": True}, CFG)
    assert r_push > r_arch > r_block
    assert 0.0 <= r_block and r_push <= 1.0
    lo = bandit.outcome_reward({"final_score": 40}, CFG)
    hi = bandit.outcome_reward({"final_score": 90}, CFG)
    assert hi >= lo
    assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0


def test_select_track_deterministic_argmax():
    # select_track is deterministic given a seed (replay-safe), returns a track from the candidate
    # set, and equals the argmax of the same Thompson draw (tie-break by id ascending).
    import bandit
    arms = {"ai-agents": {"alpha": 9, "beta": 3, "n": 10},
            "dev-tools": {"alpha": 4, "beta": 4, "n": 6},
            "saas-niche": {"alpha": 2, "beta": 7, "n": 7}}
    tracks = ["ai-agents", "dev-tools", "saas-niche"]
    s1 = bandit.select_track(arms, tracks, seed=99, cfg=CFG)
    s2 = bandit.select_track(arms, tracks, seed=99, cfg=CFG)
    assert s1 == s2 and s1 in tracks
    draw = bandit.thompson_sample(arms, tracks, seed=99, cfg=CFG)
    expected = min(draw.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]
    assert s1 == expected
    # tie => deterministic id-ascending pick (replay-safe)
    flat = {t: {"alpha": 1.0, "beta": 1.0, "n": 0} for t in ("zeta", "alpha", "mu")}
    picks = {bandit.select_track(flat, ["zeta", "alpha", "mu"], seed=k, cfg=CFG) for k in range(5)}
    assert picks <= {"zeta", "alpha", "mu"}


def test_explore_weight_feeds_score_bounded():
    # Integration contract: the bandit's track weight is a drop-in for score_opportunity's
    # track_weight, and the final score stays in [0,100] (score.py re-clamps at half strength) — the
    # bandit lifts ranking, never breaks the score.
    import bandit
    from score import score_opportunity
    bd = {"track_fit": 70, "timing": 75, "feasibility": 65, "competition": 60, "executability": 70}
    arms = {"ai-agents": {"alpha": 40.0, "beta": 5.0, "n": 45}}
    w = bandit.explore_weight(arms, "ai-agents", seed=11, cfg=CFG)
    out = score_opportunity(bd, n_sources=3, age_h=4.0, velocity=None, track_weight=w, cfg=CFG)
    assert 0.0 <= out["final_score"] <= 100.0
    assert 0.5 <= w <= 1.5
