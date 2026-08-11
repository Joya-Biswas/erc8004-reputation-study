"""
ERC-8004 Reputation Signal Analysis
===================================
Consumes the CSVs produced by pull_data.py and produces the paper's numbers.

Sections mirror the paper's research questions:
  RQ1  How much reputation data exists, and how is it distributed?
  RQ2  Does the rating signal carry information, or is it saturated?
  RQ3  How concentrated is feedback production (who actually rates)?
  RQ4  What fraction of feedback shows verifiable common-control indicators?
  RQ5  Does the off-chain filtering the spec assumes actually recover signal?

Every statistic printed here is computed from on-chain data only. Nothing is
estimated or imputed. Where a value cannot be computed it is reported as such
rather than substituted.

RUN:  python analyze.py            (defaults to chain=base)
      python analyze.py ethereum
"""

import collections
import csv
import json
import math
import os
import sys
from urllib.parse import urlparse

CHAIN = sys.argv[1] if len(sys.argv) > 1 else "base"
OUT_JSON = f"findings_{CHAIN}.json"

ZERO = "0x0000000000000000000000000000000000000000"


def load(name):
    path = f"{name}_{CHAIN}.csv"
    if not os.path.exists(path):
        print(f"  (missing {path})")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # de-duplicate on (txHash, logIndex) - the canonical unique key for a log
    seen, out = set(), []
    for r in rows:
        k = (r.get("txHash"), r.get("logIndex"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    if len(out) != len(rows):
        print(f"  {path}: dropped {len(rows)-len(out):,} duplicate rows")
    return out


def gini(values):
    """Gini coefficient. 0 = perfectly even, 1 = one actor holds everything."""
    v = sorted(x for x in values if x > 0)
    n = len(v)
    if n == 0:
        return None
    total = sum(v)
    if total == 0:
        return None
    cum = sum((i + 1) * x for i, x in enumerate(v))
    return (2 * cum) / (n * total) - (n + 1) / n


def entropy_bits(counter):
    """Shannon entropy of a distribution, in bits."""
    total = sum(counter.values())
    if total == 0:
        return None
    h = 0.0
    for c in counter.values():
        if c:
            p = c / total
            h -= p * math.log2(p)
    return h


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def domain_of(uri):
    try:
        if uri.startswith("data:"):
            return "(inline data: URI)"
        host = urlparse(uri).netloc.lower()
        return host or "(unparseable)"
    except Exception:
        return "(unparseable)"


def head(title):
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def main():
    F = {}

    head(f"LOADING  chain={CHAIN}")
    regs = load("registered")
    fbs = load("feedback")
    xfers = load("transfer")
    print(f"  registrations={len(regs):,}  feedback={len(fbs):,}  "
          f"transfers={len(xfers):,}")
    if not regs and not fbs:
        print("\nNo data yet - run pull_data.py first.")
        return

    for r in regs + fbs + xfers:
        r["blockNumber"] = int(r["blockNumber"])
        r["timestamp"] = int(r["timestamp"]) if r.get("timestamp") else None
    for r in fbs:
        r["value"] = int(r["value"])
        r["valueDecimals"] = int(r["valueDecimals"])
        r["feedbackIndex"] = int(r["feedbackIndex"])

    # ---------------------------------------------------------------- RQ1 --
    head("RQ1  SCALE AND COVERAGE")
    ts = [r["timestamp"] for r in regs + fbs if r["timestamp"]]
    if ts:
        span_days = (max(ts) - min(ts)) / 86400
        print(f"  observation window: {span_days:.1f} days")
        F["window_days"] = round(span_days, 1)
    owners = {r["owner"] for r in regs}
    raters = {r["clientAddress"] for r in fbs}
    rated_agents = {r["agentId"] for r in fbs}
    print(f"  agents registered           {len(regs):,}")
    print(f"  distinct owner addresses    {len(owners):,}")
    print(f"  feedback events             {len(fbs):,}")
    print(f"  distinct rater addresses    {len(raters):,}")
    print(f"  agents that received >=1    {len(rated_agents):,} "
          f"({pct(len(rated_agents), len(regs)):.1f}% of registered)")
    F.update(agents=len(regs), owners=len(owners), feedback=len(fbs),
             raters=len(raters), rated_agents=len(rated_agents))

    # ---------------------------------------------------------------- RQ2 --
    head("RQ2  DOES THE RATING SIGNAL CARRY INFORMATION?")
    if fbs:
        decs = collections.Counter(r["valueDecimals"] for r in fbs)
        print(f"  valueDecimals seen: {dict(decs)}")
        scores = [r["value"] / (10 ** r["valueDecimals"]) for r in fbs]
        scores_sorted = sorted(scores)
        n = len(scores_sorted)

        def q(p):
            return scores_sorted[min(n - 1, int(p * n))]

        mean = sum(scores) / n
        print(f"  n={n:,}  min={scores_sorted[0]:g}  max={scores_sorted[-1]:g}")
        print(f"  mean={mean:.2f}  median={q(0.50):g}")
        print(f"  p05={q(0.05):g}  p25={q(0.25):g}  p75={q(0.75):g}  p95={q(0.95):g}")

        buckets = collections.Counter()
        for s in scores:
            buckets[min(int(s // 10) * 10, 100)] += 1
        print("\n  score histogram (bucket -> share):")
        for b in sorted(buckets):
            share = pct(buckets[b], n)
            bar = "#" * int(share / 2)
            print(f"    {b:>3}-{b+9:<3} {share:6.2f}%  {bar}")

        h = entropy_bits(buckets)
        hmax = math.log2(len(buckets)) if len(buckets) > 1 else 0
        print(f"\n  Shannon entropy over 10-point buckets: {h:.3f} bits "
              f"(max possible {hmax:.3f})")
        top_share = pct(sum(c for b, c in buckets.items() if b >= 90), n)
        print(f"  share of all ratings >= 90 : {top_share:.2f}%")
        print(f"  share of all ratings >= 80 : "
              f"{pct(sum(c for b, c in buckets.items() if b >= 80), n):.2f}%")
        F.update(score_mean=round(mean, 3), score_median=q(0.50),
                 score_entropy_bits=round(h, 4) if h else None,
                 share_ge_90=round(top_share, 2),
                 histogram={str(k): v for k, v in sorted(buckets.items())})

        print("\n  INTERPRETATION: if the mass sits in one or two buckets the")
        print("  score cannot discriminate between agents - that is the finding.")

    # ---------------------------------------------------------------- RQ3 --
    head("RQ3  WHO PRODUCES THE FEEDBACK?")
    if fbs:
        per_rater = collections.Counter(r["clientAddress"] for r in fbs)
        per_agent = collections.Counter(r["agentId"] for r in fbs)
        g_rater = gini(list(per_rater.values()))
        g_agent = gini(list(per_agent.values()))
        print(f"  Gini of feedback per rater : {g_rater:.4f}")
        print(f"  Gini of feedback per agent : {g_agent:.4f}")

        tot = len(fbs)
        for k in (1, 10, 100):
            top = sum(c for _, c in per_rater.most_common(k))
            print(f"  top {k:>3} raters produce {pct(top, tot):6.2f}% of all feedback")
        print("\n  top 15 raters:")
        for addr, c in per_rater.most_common(15):
            agents_rated = len({r["agentId"] for r in fbs
                                if r["clientAddress"] == addr})
            print(f"    {addr}  {c:>7,} events  across {agents_rated:>6,} agents")
        F.update(gini_rater=round(g_rater, 4), gini_agent=round(g_agent, 4),
                 top10_rater_share=round(
                     pct(sum(c for _, c in per_rater.most_common(10)), tot), 2))

    # ---------------------------------------------------------------- RQ4 --
    head("RQ4  VERIFIABLE COMMON-CONTROL INDICATORS")
    print("  These are facts on chain, not statistical resemblance.\n")

    # (a) batch registration in a single transaction
    by_tx = collections.Counter(r["txHash"] for r in regs)
    batched = {tx: c for tx, c in by_tx.items() if c > 1}
    n_batched = sum(batched.values())
    print(f"  (a) registrations sharing a transaction with another: "
          f"{n_batched:,} ({pct(n_batched, len(regs)):.2f}%)")
    if batched:
        big = sorted(batched.items(), key=lambda kv: -kv[1])[:5]
        for tx, c in big:
            print(f"        {tx}  {c} agents in one tx")

    # (b) one owner controlling many agents
    per_owner = collections.Counter(r["owner"] for r in regs)
    multi = {o: c for o, c in per_owner.items() if c > 1}
    print(f"\n  (b) owners holding >1 agent: {len(multi):,} owners covering "
          f"{sum(multi.values()):,} agents "
          f"({pct(sum(multi.values()), len(regs)):.2f}%)")
    for o, c in collections.Counter(multi).most_common(10):
        print(f"        {o}  {c:,} agents")

    # (c) agentURI domain concentration - same host means same operator
    doms = collections.Counter(domain_of(r["agentURI"]) for r in regs)
    print(f"\n  (c) distinct agentURI hosts: {len(doms):,}")
    for d, c in doms.most_common(12):
        print(f"        {d:<52} {c:>7,} ({pct(c, len(regs)):5.2f}%)")
    F["top_domains"] = doms.most_common(12)

    # (d) byte-identical feedback content
    if fbs:
        fh = collections.Counter(r["feedbackHash"] for r in fbs
                                 if r.get("feedbackHash") and
                                 int(r["feedbackHash"], 16) != 0)
        dupes = {h: c for h, c in fh.items() if c > 1}
        n_dupe = sum(dupes.values())
        print(f"\n  (d) feedback events whose content hash is not unique: "
              f"{n_dupe:,} ({pct(n_dupe, len(fbs)):.2f}%) "
              f"across {len(dupes):,} repeated hashes")
        for h, c in collections.Counter(dupes).most_common(5):
            print(f"        {h}  x{c}")

    # (e) raters who also own agents - self-dealing surface
    if fbs and regs:
        overlap = raters & owners
        ov_events = sum(1 for r in fbs if r["clientAddress"] in overlap)
        print(f"\n  (e) addresses that both own an agent and rate agents: "
              f"{len(overlap):,}")
        print(f"      feedback events from those addresses: {ov_events:,} "
              f"({pct(ov_events, len(fbs)):.2f}%)")

        # direct reciprocity: owner A rates owner B's agent and vice versa
        agent_owner = {r["agentId"]: r["owner"] for r in regs}
        edges = set()
        for r in fbs:
            o = agent_owner.get(r["agentId"])
            if o and r["clientAddress"] in owners and o != r["clientAddress"]:
                edges.add((r["clientAddress"], o))
        recip = {(a, b) for (a, b) in edges if (b, a) in edges}
        print(f"      reciprocal owner-pairs (A rates B AND B rates A): "
              f"{len(recip)//2:,}")
        F["reciprocal_pairs"] = len(recip) // 2

    # (f) feedback for agents that were never registered on this chain
    if fbs and regs:
        known = {r["agentId"] for r in regs}
        orphan = sum(1 for r in fbs if r["agentId"] not in known)
        print(f"\n  (f) feedback referencing an agentId with no Registered "
              f"event here: {orphan:,} ({pct(orphan, len(fbs)):.2f}%)")

    # ---------------------------------------------------------------- RQ5 --
    head("RQ5  DOES THE SPEC'S ASSUMED OFF-CHAIN FILTERING RECOVER SIGNAL?")
    print("  ERC-8004 delegates Sybil resistance to off-chain aggregators.")
    print("  Here we apply the obvious filters and see what survives.\n")
    if fbs and regs:
        agent_owner = {r["agentId"]: r["owner"] for r in regs}

        def summarize(label, subset):
            if not subset:
                print(f"    {label:<44} 0 events")
                return
            sc = [r["value"] / (10 ** r["valueDecimals"]) for r in subset]
            b = collections.Counter(min(int(s // 10) * 10, 100) for s in sc)
            print(f"    {label:<44} {len(subset):>8,} events  "
                  f"mean={sum(sc)/len(sc):6.2f}  "
                  f"H={entropy_bits(b):.3f} bits  "
                  f"agents={len({r['agentId'] for r in subset}):,}")

        summarize("raw (no filtering)", fbs)
        f1 = [r for r in fbs if r["clientAddress"] != agent_owner.get(r["agentId"])]
        summarize("F1: drop self-owned ratings", f1)
        per_rater = collections.Counter(r["clientAddress"] for r in fbs)
        f2 = [r for r in f1 if per_rater[r["clientAddress"]] <= 100]
        summarize("F2: + drop raters with >100 events", f2)
        f3 = [r for r in f2 if r["clientAddress"] not in owners]
        summarize("F3: + drop raters who own agents", f3)
        seen_pair = set()
        f4 = []
        for r in sorted(f3, key=lambda x: (x["blockNumber"], x["logIndex"])):
            k = (r["clientAddress"], r["agentId"])
            if k in seen_pair:
                continue
            seen_pair.add(k)
            f4.append(r)
        summarize("F4: + one rating per (rater, agent) pair", f4)

        survivors = len({r["agentId"] for r in f4})
        print(f"\n  agents still holding >=1 rating after all filters: "
              f"{survivors:,} of {len(regs):,} registered "
              f"({pct(survivors, len(regs)):.2f}%)")
        by_agent_f4 = collections.Counter(r["agentId"] for r in f4)
        for k in (3, 5, 10):
            c = sum(1 for v in by_agent_f4.values() if v >= k)
            print(f"  agents with >={k:>2} independent surviving ratings: {c:,} "
                  f"({pct(c, len(regs)):.2f}% of registered)")
        F.update(survivors_any=survivors,
                 survivors_ge5=sum(1 for v in by_agent_f4.values() if v >= 5))

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(F, f, indent=2, default=str)
    print(f"\nmachine-readable findings written to {OUT_JSON}")


if __name__ == "__main__":
    main()
