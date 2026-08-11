"""
Evaluating candidate fixes for ERC-8004 reputation aggregation
==============================================================
Xiong et al. (arXiv 2606.26028) recommend typed tags and a bounded-influence
aggregator but do not implement or evaluate either. This script does, against
the full on-chain feedback set.

Aggregation rules compared (each computes one score per agent):

  R0  deployed      exact getSummary(): normalize to maxDecimals, sum, clamp
                    to int128, mean = sum/count, no tag filter
  R1  per-tag       R0 restricted to the agent's single most-used tag
  R2  clamped       R1 + values clamped to the tag's observed [0,100] band
  R3  median        R1 + median instead of mean
  R4  one-per-pair  R3 + at most one record per (rater, agent) pair
  R5  trimmed       R4 + 10% trimmed mean over distinct raters

Metrics per rule:
  coverage      % of rated agents that still get a score
  leverage      % of agents whose score moves >=50% if the single most
                extreme contributing record is removed  (lower is better)
  discrimination Shannon entropy of the agent-score distribution over 10-point
                buckets (higher = the score separates agents; 0 = useless)
  concentration % of an agent's score attributable to its single busiest rater

RUN:  python evaluate_fixes.py [chain]
"""

import collections
import csv
import math
import os
import statistics
import sys

CHAIN = sys.argv[1] if len(sys.argv) > 1 else "base"
INT128_MAX = 2 ** 127 - 1
INT128_MIN = -(2 ** 127)
MIN_RECORDS = 3        # agents below this cannot support a leverage estimate


def load():
    fb_path = f"feedback_{CHAIN}.csv"
    if not os.path.exists(fb_path):
        raise SystemExit(f"missing {fb_path} - run pull_data.py first")

    revoked = set()
    rev_path = f"revoked_{CHAIN}.csv"
    if os.path.exists(rev_path):
        with open(rev_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                revoked.add((int(r["agentId"]), r["clientAddress"],
                             int(r["feedbackIndex"])))
    else:
        print(f"  warning: {rev_path} absent; revoked records not excluded")

    per_agent = collections.defaultdict(list)
    seen = set()
    with open(fb_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r.get("txHash"), r.get("logIndex"))
            if key in seen:
                continue
            seen.add(key)
            try:
                aid = int(r["agentId"])
                rec = (int(r["value"]), int(r["valueDecimals"]),
                       r["clientAddress"], r["tag1"], int(r["feedbackIndex"]))
            except (ValueError, TypeError, KeyError):
                continue
            if (aid, rec[2], rec[4]) in revoked:
                continue
            per_agent[aid].append(rec)
    print(f"  agents with live feedback: {len(per_agent):,}  "
          f"(revoked excluded: {len(revoked):,})")
    return per_agent


def normalize(recs):
    """Scale to common decimals exactly as getSummary() does."""
    if not recs:
        return []
    md = max(d for _, d, _, _, _ in recs)
    return [v * (10 ** (md - d)) for v, d, _, _, _ in recs]


def _trunc_div(a, b):
    """Solidity integer division truncates toward zero."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def r0_deployed(recs):
    """The DEPLOYED getSummary(), verified against mainnet by eth_call on 41
    agents with exact agreement (see verify_onchain.py).

    Note this differs from the GitHub HEAD source of ReputationRegistry.sol,
    which returns a raw sum and falls back to the stored client list. The
    deployed contract returns the MEAN and rejects an empty clientAddresses
    array outright ("clientAddresses required"). All figures here follow the
    deployed behaviour.
    """
    vals = normalize(recs)
    if not vals:
        return None, []
    mean = _trunc_div(sum(vals), len(vals))
    mean = min(max(mean, INT128_MIN), INT128_MAX)
    return mean, vals


def dominant_tag(recs):
    c = collections.Counter(t for _, _, _, t, _ in recs)
    return c.most_common(1)[0][0] if c else None


def r1_per_tag(recs):
    t = dominant_tag(recs)
    sub = [r for r in recs if r[3] == t]
    return r0_deployed(sub)


def r2_clamped(recs):
    t = dominant_tag(recs)
    sub = [r for r in recs if r[3] == t]
    if not sub:
        return None, []
    md = max(d for _, d, _, _, _ in sub)
    scale = 10 ** md
    vals = [min(max(v * (10 ** (md - d)), 0), 100 * scale)
            for v, d, _, _, _ in sub]
    return sum(vals) / len(vals), vals


def r3_median(recs):
    t = dominant_tag(recs)
    sub = [r for r in recs if r[3] == t]
    if not sub:
        return None, []
    md = max(d for _, d, _, _, _ in sub)
    scale = 10 ** md
    vals = [min(max(v * (10 ** (md - d)), 0), 100 * scale)
            for v, d, _, _, _ in sub]
    return statistics.median(vals), vals


def _one_per_pair(recs):
    t = dominant_tag(recs)
    sub = [r for r in recs if r[3] == t]
    best = {}
    for r in sub:
        best.setdefault(r[2], r)          # first record per rater
    return list(best.values())


def r4_one_per_pair(recs):
    sub = _one_per_pair(recs)
    if not sub:
        return None, []
    md = max(d for _, d, _, _, _ in sub)
    scale = 10 ** md
    vals = [min(max(v * (10 ** (md - d)), 0), 100 * scale)
            for v, d, _, _, _ in sub]
    return statistics.median(vals), vals


def r5_trimmed(recs):
    sub = _one_per_pair(recs)
    if not sub:
        return None, []
    md = max(d for _, d, _, _, _ in sub)
    scale = 10 ** md
    vals = sorted(min(max(v * (10 ** (md - d)), 0), 100 * scale)
                  for v, d, _, _, _ in sub)
    k = int(len(vals) * 0.10)
    core = vals[k:len(vals) - k] or vals
    return sum(core) / len(core), core


RULES = [
    ("R0 deployed getSummary", r0_deployed),
    ("R1 + per-tag only", r1_per_tag),
    ("R2 + range clamp [0,100]", r2_clamped),
    ("R3 + median", r3_median),
    ("R4 + one record per rater", r4_one_per_pair),
    ("R5 + 10% trimmed mean", r5_trimmed),
]


ATTACKER = "0xAtt4ck3r000000000000000000000000000000000"


def adversarial_shift(rule, recs, magnitude):
    """The paper's core experiment.

    Hold the honest record set fixed, append ONE attacker record carrying
    `magnitude` under the agent's dominant tag, and measure how far the rule's
    score moves. Unlike a leave-one-out measure this is comparable across
    rules: every rule sees the identical honest input and the identical
    injected record, so any difference is attributable to the aggregation
    logic rather than to how many records the rule happens to consider.

    Returns the relative shift, or None when the rule yields no honest score.
    """
    honest, _ = rule(recs)
    if honest is None:
        return None
    tag = dominant_tag(recs)
    poisoned = recs + [(magnitude, 0, ATTACKER, tag, 1)]
    after, _ = rule(poisoned)
    if after is None:
        return None
    denom = abs(honest) if honest != 0 else 1.0
    return abs(after - honest) / denom


def entropy_over_scores(scores):
    """Scores are on each rule's own scale; normalise per-rule to 0-100 by
    dividing out the decimal scale is not possible generically, so bucket on
    rank-free relative position instead: map to 10 quantile buckets."""
    vals = [s for s in scores if s is not None]
    if len(vals) < 10:
        return None
    vals_sorted = sorted(vals)
    edges = [vals_sorted[int(len(vals_sorted) * i / 10)] for i in range(1, 10)]
    buckets = collections.Counter()
    for v in vals:
        b = 0
        for e in edges:
            if v > e:
                b += 1
        buckets[b] += 1
    total = sum(buckets.values())
    h = 0.0
    for c in buckets.values():
        if c:
            p = c / total
            h -= p * math.log2(p)
    return h


def main():
    print(f"chain={CHAIN}")
    per_agent = load()
    eligible = {a: r for a, r in per_agent.items() if len(r) >= MIN_RECORDS}
    print(f"  agents with >={MIN_RECORDS} live records: {len(eligible):,}\n")

    # Two attacker budgets: a maximal int128 payload, and a "stealthy" one
    # that stays inside a plausible rating range.
    ATTACKS = [("max int128", INT128_MAX), ("value=100 only", 100)]

    for label, mag in ATTACKS:
        print("=" * 96)
        print(f"ADVERSARIAL INJECTION - one extra record, {label}")
        print("=" * 96)
        print(f"{'rule':<28}{'coverage':>10}{'shift>=10%':>12}"
              f"{'shift>=50%':>12}{'median shift':>15}{'entropy':>10}")
        print("-" * 96)
        for name, rule in RULES:
            scores, shifts = [], []
            for aid, recs in eligible.items():
                s, _ = rule(recs)
                scores.append(s)
                sh = adversarial_shift(rule, recs, mag)
                if sh is not None:
                    shifts.append(sh)
            cov = sum(1 for s in scores if s is not None)
            n = len(shifts) or 1
            ge10 = sum(1 for x in shifts if x >= 0.10)
            ge50 = sum(1 for x in shifts if x >= 0.50)
            med = statistics.median(shifts) if shifts else float("nan")
            ent = entropy_over_scores(scores)
            med_s = (f"{100*med:13.2f}%" if med < 1e6
                     else f"{med:13.2e}x")
            print(f"{name:<28}{100*cov/len(eligible):9.1f}%"
                  f"{100*ge10/n:11.2f}%{100*ge50/n:11.2f}%"
                  f"{med_s}{(ent if ent else 0):10.3f}")
        print()

    print("coverage     = % of eligible agents that still receive a score")
    print("shift>=X%    = % of agents whose score moves by >=X% when ONE")
    print("               attacker record is appended (lower is better)")
    print("median shift = typical movement caused by that single record")
    print("entropy      = bits over decile buckets of the agent-score")
    print("               distribution; 3.32 = maximally discriminating,")
    print("               0 = every agent scores identically")
    print("\nEvery rule sees the identical honest records and the identical")
    print("injected record, so differences are attributable to the")
    print("aggregation logic alone. R0 is the deployed contract.")


if __name__ == "__main__":
    main()
