"""
Aggregation saturation measurement for ERC-8004's Reputation Registry
=====================================================================
Faithfully re-implements the deployed getSummary() aggregation and measures how
many agents currently hold a saturated or distorted on-chain reputation summary.

The aggregation logic mirrors ReputationRegistry.sol exactly:

    pass 1:  maxDecimals = max(valueDecimals) over non-revoked matching feedback
    pass 2:  normalized = value * 10 ** (maxDecimals - valueDecimals)
             totalValue += normalized
    clamp :  totalValue > int128.max  -> int128.max
             totalValue < int128.min  -> int128.min

The clamp is the vulnerability: `value` is an unbounded int128 (giveFeedback
validates only `valueDecimals <= 18`), so a single feedback record can drive the
sum past the bound and pin the summary to a fixed extreme, in either direction.
The targeted agent cannot remove it - revokeFeedback authorises only the
original client.

Requires feedback_<chain>.csv from pull_data.py. Collects FeedbackRevoked
events itself (they are not part of the main pull) unless the CSV already
exists.

RUN:  python saturation.py [chain]
"""

import collections
import csv
import os
import sys

import pull_data as P

CHAIN = sys.argv[1] if len(sys.argv) > 1 else "base"

INT128_MAX = 2 ** 127 - 1
INT128_MIN = -(2 ** 127)

SIG_REVOKED = "FeedbackRevoked(uint256,address,uint64)"
T_REVOKED = P.topic0(SIG_REVOKED)
EXPECTED_REVOKED = ("0x25156fd3288212246d8b008d5921fde3"
                    "76c71ed14ac2e072a506eb06fde6d09d")


def collect_revocations(chain):
    """FeedbackRevoked(uint256 indexed, address indexed, uint64 indexed) -
    all three parameters are indexed, so the payload is entirely in topics."""
    path = f"revoked_{chain}.csv"
    if os.path.exists(path):
        print(f"  using existing {path}")
        return path
    if T_REVOKED != EXPECTED_REVOKED:
        raise SystemExit(f"topic0 drift for FeedbackRevoked: {T_REVOKED}")

    url = P.CHAINS[chain]
    head = P.latest_block(url)
    dep = P.find_deployment_block(url, P.REPUTATION_REGISTRY, head)
    if dep is None:
        raise SystemExit(f"reputation registry not deployed on {chain}")
    print(f"  scanning {dep:,}-{head:,} for FeedbackRevoked")

    rows, b = [], head
    while b > dep:
        lo = max(dep, b - 1_000_000)
        logs, err = P.rpc(url, "eth_getLogs", [{
            "address": P.REPUTATION_REGISTRY, "topics": [T_REVOKED],
            "fromBlock": hex(lo), "toBlock": hex(b)}])
        if err:
            print(f"    {lo:,}-{b:,} error {err}")
            b = lo - 1
            continue
        for l in logs or []:
            rows.append({
                "agentId": int(l["topics"][1], 16),
                "clientAddress": P._addr(l["topics"][2]),
                "feedbackIndex": int(l["topics"][3], 16),
                "blockNumber": int(l["blockNumber"], 16),
                "txHash": l["transactionHash"],
            })
        b = lo - 1

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["agentId", "clientAddress",
                                          "feedbackIndex", "blockNumber",
                                          "txHash"])
        w.writeheader()
        w.writerows(rows)
    print(f"  {len(rows):,} revocations -> {path}")
    return path


def get_summary(entries):
    """Exact re-implementation of the deployed getSummary() aggregation.
    entries: list of (value:int, valueDecimals:int).
    Returns (summaryValue, count, maxDecimals, rawTotal, saturated)."""
    if not entries:
        return 0, 0, 0, 0, None
    max_dec = max(d for _, d in entries)
    total = 0
    for v, d in entries:
        total += v * (10 ** (max_dec - d))
    if total > INT128_MAX:
        return INT128_MAX, len(entries), max_dec, total, "high"
    if total < INT128_MIN:
        return INT128_MIN, len(entries), max_dec, total, "low"
    return total, len(entries), max_dec, total, None


def main():
    print(f"chain={CHAIN}")
    print(f"FeedbackRevoked topic0 {T_REVOKED}")

    rev_path = collect_revocations(CHAIN)
    revoked = set()
    with open(rev_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            revoked.add((int(r["agentId"]), r["clientAddress"],
                         int(r["feedbackIndex"])))
    print(f"  revoked records: {len(revoked):,}")

    fb_path = f"feedback_{CHAIN}.csv"
    if not os.path.exists(fb_path):
        raise SystemExit(f"missing {fb_path}")

    per_agent = collections.defaultdict(list)
    total_rows = skipped = 0
    with open(fb_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                aid = int(r["agentId"])
                val = int(r["value"])
                dec = int(r["valueDecimals"])
                idx = int(r["feedbackIndex"])
            except (ValueError, TypeError, KeyError):
                continue
            total_rows += 1
            if (aid, r["clientAddress"], idx) in revoked:
                skipped += 1
                continue
            per_agent[aid].append((val, dec, r["clientAddress"], r["tag1"]))

    print(f"  feedback rows {total_rows:,}; excluded as revoked {skipped:,}")
    print(f"  agents with >=1 live feedback: {len(per_agent):,}")

    sat_high = sat_low = normal = 0
    sat_agents = []
    # Leverage: how much does the single most extreme record move the mean?
    # This is the meaningful measure of unbounded per-record influence. An
    # earlier version compared one contribution against the sum of absolute
    # contributions, which merely detected agents whose other feedback carried
    # value 0 - that is a separate phenomenon, counted below.
    leverage = []
    zero_valued = 0

    for aid, ents in per_agent.items():
        pairs = [(v, d) for v, d, _, _ in ents]
        summary, count, max_dec, raw, sat = get_summary(pairs)
        if sat == "high":
            sat_high += 1
            sat_agents.append((aid, count, raw, "high", ents))
            continue
        if sat == "low":
            sat_low += 1
            sat_agents.append((aid, count, raw, "low", ents))
            continue
        normal += 1

        nonzero = [(v, d) for v, d in pairs if v != 0]
        if len(pairs) >= 3 and len(nonzero) == 0:
            zero_valued += 1
        if len(pairs) < 3 or not nonzero:
            continue

        norm = [v * (10 ** (max_dec - d)) for v, d in pairs]
        mean_all = sum(norm) / len(norm)
        drop = max(range(len(norm)), key=lambda i: abs(norm[i]))
        rest = norm[:drop] + norm[drop + 1:]
        mean_rest = sum(rest) / len(rest)
        if mean_rest != 0:
            shift = abs(mean_all - mean_rest) / abs(mean_rest)
            leverage.append((shift, aid, len(pairs), ents[drop]))

    n = len(per_agent)
    print("\n" + "=" * 70)
    print("SUMMARY STATE OF EVERY RATED AGENT")
    print("=" * 70)
    print(f"  saturated at int128.max (inflated to ceiling) : "
          f"{sat_high:,} ({100*sat_high/n:.3f}%)")
    print(f"  saturated at int128.min (pinned to floor)     : "
          f"{sat_low:,} ({100*sat_low/n:.3f}%)")
    print(f"  not saturated                                 : "
          f"{normal:,} ({100*normal/n:.3f}%)")
    print(f"  agents (>=3 records) whose feedback is all 0  : {zero_valued:,}")

    if leverage:
        leverage.sort(reverse=True)
        m = len(leverage)
        print("\n" + "=" * 70)
        print("SINGLE-RECORD LEVERAGE  (agents with >=3 live records)")
        print("=" * 70)
        print("  Relative shift in the mean caused by the single most extreme")
        print("  record. Measures unbounded per-record influence directly.")
        print(f"  evaluated agents: {m:,}")
        for thr in (10.0, 1.0, 0.5, 0.25):
            c = sum(1 for s, *_ in leverage if s >= thr)
            print(f"    shift >= {thr*100:6.0f}% of the remaining mean : "
                  f"{c:,} ({100*c/m:.2f}%)")
        print("\n  most-leveraged agents:")
        for shift, aid, cnt, worst in leverage[:10]:
            v, d, cl, tag = worst
            print(f"    agent {aid:<7} {cnt:>4} records  shift={shift*100:12.1f}%"
                  f"  value={v} dec={d} tag={tag[:18]!r}")

    if sat_agents:
        print("\n  SATURATED AGENTS:")
        for aid, count, raw, kind, ents in sat_agents[:15]:
            print(f"    agent {aid}: {count} live records, raw total {raw:.3e}, "
                  f"clamped {kind}")
            worst = sorted(ents, key=lambda e: -abs(e[0]))[:3]
            for v, d, cl, tag in worst:
                print(f"        value={v} dec={d} tag={tag[:24]!r} client={cl}")

    # how cheap is the attack? one giveFeedback call.
    print("\n  Attack requires ONE giveFeedback call. The only input check on")
    print("  that path is `require(valueDecimals <= 18)`; `value` is an")
    print("  unbounded int128. The victim cannot revoke - revokeFeedback")
    print("  authorises only the original client.")


if __name__ == "__main__":
    main()
