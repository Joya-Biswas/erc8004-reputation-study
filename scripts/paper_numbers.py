"""
Single source of truth for every number in the paper
====================================================
Computes each figure once, from the frozen dataset, and writes
`paper_numbers.json` plus a readable report. The manuscript and audit.py both
read from this file, so a statistic can never be quoted from a stale or partial
run again.

FROZEN SNAPSHOT. All three event streams on each chain terminated at the same
block, so the snapshot is internally consistent:

    Base     blocks [41,663,783 .. 49,819,859]
    Ethereum blocks [24,339,873 .. 25,729,914]

Statistics below are computed strictly from events at or below those heights.
Live eth_call observations are timestamped separately, because the chain
continues to advance and those values drift.

RUN:  python paper_numbers.py
"""

import collections
import csv
import datetime as dt
import json
import math
import os
import statistics

FREEZE = {"base": 49_819_859, "ethereum": 25_729_914}
DEPLOY = {"base": 41_663_783, "ethereum": 24_339_873}

OUT_JSON = "paper_numbers.json"
N = {}


def load(kind, chain):
    path = f"{kind}_{chain}.csv"
    if not os.path.exists(path):
        return []
    rows, seen = [], set()
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r.get("txHash"), r.get("logIndex"))
            if key != (None, None):
                if key in seen:
                    continue
                seen.add(key)
            try:
                r["blockNumber"] = int(r["blockNumber"])
            except (ValueError, TypeError, KeyError):
                continue
            if r["blockNumber"] > FREEZE[chain]:
                continue          # enforce the freeze
            rows.append(r)
    return rows


def gini(values):
    v = sorted(x for x in values if x > 0)
    n, tot = len(v), sum(v)
    if not n or not tot:
        return None
    cum = sum((i + 1) * x for i, x in enumerate(v))
    return (2 * cum) / (n * tot) - (n + 1) / n


def main():
    N["freeze_block"] = FREEZE
    N["deploy_block"] = DEPLOY

    print("FROZEN SNAPSHOT")
    for c in FREEZE:
        print(f"  {c}: blocks {DEPLOY[c]:,} .. {FREEZE[c]:,}")

    # ---------------------------------------------------------- dataset ----
    print("\nDATASET (post-freeze)")
    N["counts"] = {}
    data = {}
    for chain in ("base", "ethereum"):
        data[chain] = {}
        N["counts"][chain] = {}
        for kind in ("registered", "feedback", "transfer"):
            rows = load(kind, chain)
            data[chain][kind] = rows
            N["counts"][chain][kind] = len(rows)
        rev = []
        p = f"revoked_{chain}.csv"
        if os.path.exists(p):
            with open(p, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if int(r["blockNumber"]) <= FREEZE[chain]:
                        rev.append(r)
        data[chain]["revoked"] = rev
        N["counts"][chain]["revoked"] = len(rev)
        c = N["counts"][chain]
        print(f"  {chain:9} reg={c['registered']:>7,} fb={c['feedback']:>8,} "
              f"xfer={c['transfer']:>7,} revoked={c['revoked']:>4,}")

    # observation window
    for chain in ("base", "ethereum"):
        ts = [int(r["timestamp"]) for r in data[chain]["feedback"] + data[chain]["registered"]
              if r.get("timestamp")]
        if ts:
            lo = dt.datetime.fromtimestamp(min(ts), dt.UTC).date()
            hi = dt.datetime.fromtimestamp(max(ts), dt.UTC).date()
            N.setdefault("window", {})[chain] = [str(lo), str(hi)]
            print(f"  {chain:9} window {lo} .. {hi}")

    # ------------------------------------------------------- base stats ----
    print("\nBASE FEEDBACK STATISTICS (full frozen set)")
    fbs = data["base"]["feedback"]
    revoked_keys = {(int(r["agentId"]), r["clientAddress"], int(r["feedbackIndex"]))
                    for r in data["base"]["revoked"]}
    live = []
    for r in fbs:
        try:
            r["value"] = int(r["value"])
            r["valueDecimals"] = int(r["valueDecimals"])
            r["agentId"] = int(r["agentId"])
            idx = int(r["feedbackIndex"])
        except (ValueError, TypeError):
            continue
        if (r["agentId"], r["clientAddress"], idx) in revoked_keys:
            continue
        live.append(r)
    N["base_live_feedback"] = len(live)
    print(f"  live (non-revoked) feedback: {len(live):,}")

    tags = collections.Counter(r["tag1"] for r in live)
    N["distinct_tags"] = len(tags)
    per_tag_dec = collections.defaultdict(set)
    for r in live:
        per_tag_dec[r["tag1"]].add(r["valueDecimals"])
    mixed = {t: sorted(d) for t, d in per_tag_dec.items() if len(d) > 1}
    N["tags_mixed_decimals"] = len(mixed)
    N["reliability_decimals"] = sorted(per_tag_dec.get("reliability", []))
    print(f"  distinct tag1 values          {len(tags):,}")
    print(f"  tags with >1 decimal scale    {len(mixed):,}")
    print(f"  'reliability' decimals        {N['reliability_decimals']}")

    raws = [r["value"] for r in live]
    scaled = [r["value"] / (10 ** r["valueDecimals"]) for r in live]
    N["value_min_raw"] = min(raws)
    N["value_max_scaled"] = max(scaled)
    N["raw_gt_100"] = sum(1 for v in raws if v > 100)
    N["scaled_gt_100"] = sum(1 for s in scaled if s > 100)
    N["scaled_lt_0"] = sum(1 for s in scaled if s < 0)
    N["scaled_in_range"] = sum(1 for s in scaled if 0 <= s <= 100)
    print(f"  raw value min                 {min(raws):,}")
    print(f"  scaled value max              {max(scaled):.6g}")
    print(f"  raw > 100                     {N['raw_gt_100']:,}")
    print(f"  scaled outside [0,100]        "
          f"{N['scaled_gt_100']:,} high / {N['scaled_lt_0']:,} low")

    per_rater = collections.Counter(r["clientAddress"] for r in live)
    N["distinct_raters"] = len(per_rater)
    N["gini_rater"] = round(gini(list(per_rater.values())), 4)
    N["top10_share"] = round(
        100 * sum(c for _, c in per_rater.most_common(10)) / len(live), 2)
    N["top1_share"] = round(
        100 * per_rater.most_common(1)[0][1] / len(live), 2)
    pair = collections.Counter((r["clientAddress"], r["agentId"]) for r in live)
    top_pair, top_pair_n = pair.most_common(1)[0]
    N["largest_pair"] = top_pair_n
    N["largest_pair_rater"] = top_pair[0]
    N["largest_pair_agent"] = top_pair[1]
    print(f"  distinct raters               {len(per_rater):,}")
    print(f"  Gini per reviewer             {N['gini_rater']}")
    print(f"  top-1 / top-10 share          {N['top1_share']}% / {N['top10_share']}%")
    print(f"  largest reviewer-agent pair   {top_pair_n:,} "
          f"(rater {top_pair[0][:10]}... agent {top_pair[1]})")

    by_agent = collections.defaultdict(set)
    for r in live:
        by_agent[r["agentId"]].add(r["clientAddress"])
    rc = sorted(len(v) for v in by_agent.values())
    N["rated_agents"] = len(by_agent)
    N["median_reviewers"] = statistics.median(rc)
    N["p99_reviewers"] = rc[int(0.99 * len(rc))]
    N["max_reviewers"] = rc[-1]
    N["max_reviewers_agent"] = max(by_agent, key=lambda a: len(by_agent[a]))
    print(f"  rated agents                  {len(by_agent):,}")
    print(f"  reviewers/agent median        {N['median_reviewers']}")
    print(f"  reviewers/agent p99           {N['p99_reviewers']}")
    print(f"  reviewers/agent max           {N['max_reviewers']:,} "
          f"(agent {N['max_reviewers_agent']})")

    recs_per_agent = collections.Counter(r["agentId"] for r in live)
    N["max_reviewers_agent_records"] = recs_per_agent[N["max_reviewers_agent"]]
    print(f"  that agent's record count     "
          f"{N['max_reviewers_agent_records']:,}")

    # registration side
    regs = data["base"]["registered"]
    owners = collections.Counter(r["owner"] for r in regs)
    N["base_owners"] = len(owners)
    N["base_top_owner_agents"] = owners.most_common(1)[0][1]
    by_tx = collections.Counter(r["txHash"] for r in regs)
    N["base_batch_registrations"] = sum(c for c in by_tx.values() if c > 1)
    N["base_max_per_tx"] = max(by_tx.values())
    print(f"\n  distinct owners               {len(owners):,}")
    print(f"  largest owner holds           {N['base_top_owner_agents']:,} agents")
    print(f"  registrations sharing a tx    {N['base_batch_registrations']:,} "
          f"(max {N['base_max_per_tx']} in one tx)")

    # Validation Registry (Base only; scanned chain-wide, no singleton exists)
    for kind in ("validation_requests", "validation_responses"):
        p = f"{kind}_base.csv"
        if os.path.exists(p):
            with open(p, newline="", encoding="utf-8") as f:
                rows = [r for r in csv.DictReader(f)
                        if int(r["blockNumber"]) <= FREEZE["base"]]
            N["counts"]["base"][kind] = len(rows)
            N.setdefault("validation_contracts", {})[kind] = len(
                {r["contract"] for r in rows})

    N["total_events"] = sum(v for c in N["counts"].values() for v in c.values())
    print(f"\n  TOTAL EVENTS ACROSS ALL STREAMS: {N['total_events']:,}")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(N, f, indent=2, default=str)
    print(f"\nwritten -> {OUT_JSON}")
    print("The manuscript must cite only values from this file.")


if __name__ == "__main__":
    main()
