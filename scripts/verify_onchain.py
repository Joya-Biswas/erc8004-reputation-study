"""
Ground-truth verification against the deployed contract
=======================================================
Calls the real ReputationRegistry.getSummary() on Base via eth_call and
compares the returned aggregate to our local re-implementation.

Two purposes:
  1. Validate that saturation.py reproduces the deployed aggregation exactly.
     If the two disagree on any agent, the local model is wrong and every
     number derived from it is suspect.
  2. Provide on-chain evidence for the saturated agent: if the live contract
     returns int128.max, the condition is real on mainnet, not simulated.

Read-only: eth_call only, no transactions, no state change.

    getSummary(uint256 agentId, address[] clientAddresses,
               string tag1, string tag2)
      returns (uint64 count, int128 summaryValue, uint8 summaryValueDecimals)

RUN:  python verify_onchain.py [chain] [agentId ...]
"""

import collections
import csv
import os
import sys

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

import pull_data as P

INT128_MAX = 2 ** 127 - 1
INT128_MIN = -(2 ** 127)

SIG = "getSummary(uint256,address[],string,string)"
SELECTOR = Web3.keccak(text=SIG)[:4]


def call_get_summary(url, agent_id, clients=None, tag1="", tag2=""):
    payload = SELECTOR + abi_encode(
        ["uint256", "address[]", "string", "string"],
        [agent_id, clients or [], tag1, tag2])
    result, err = P.rpc(url, "eth_call", [{
        "to": P.REPUTATION_REGISTRY,
        "data": "0x" + payload.hex(),
    }, "latest"], 60)
    if err:
        return None, err
    raw = bytes.fromhex(result[2:])
    count, value, decimals = abi_decode(["uint64", "int128", "uint8"], raw)
    return (count, value, decimals), None


def local_summary(records):
    """Mirror of the deployed aggregation, for comparison."""
    if not records:
        return 0, 0, 0
    max_dec = max(d for _, d in records)
    total = sum(v * (10 ** (max_dec - d)) for v, d in records)
    if total > INT128_MAX:
        total = INT128_MAX
    elif total < INT128_MIN:
        total = INT128_MIN
    return len(records), total, max_dec


def load_agents_with_clients(chain):
    """Same as load_agents but retains each record's client address, because
    the deployed getSummary() requires an explicit clientAddresses list."""
    revoked = set()
    rp = f"revoked_{chain}.csv"
    if os.path.exists(rp):
        with open(rp, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                revoked.add((int(r["agentId"]), r["clientAddress"],
                             int(r["feedbackIndex"])))
    per_agent = collections.defaultdict(list)
    seen = set()
    with open(f"feedback_{chain}.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r["txHash"], r["logIndex"])
            if k in seen:
                continue
            seen.add(k)
            try:
                aid = int(r["agentId"])
                v, d = int(r["value"]), int(r["valueDecimals"])
                idx = int(r["feedbackIndex"])
            except (ValueError, TypeError, KeyError):
                continue
            if (aid, r["clientAddress"], idx) in revoked:
                continue
            per_agent[aid].append((v, d, r["clientAddress"]))
    return per_agent


def load_agents(chain):
    revoked = set()
    rp = f"revoked_{chain}.csv"
    if os.path.exists(rp):
        with open(rp, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                revoked.add((int(r["agentId"]), r["clientAddress"],
                             int(r["feedbackIndex"])))
    per_agent = collections.defaultdict(list)
    seen = set()
    with open(f"feedback_{chain}.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r["txHash"], r["logIndex"])
            if k in seen:
                continue
            seen.add(k)
            try:
                aid = int(r["agentId"])
                v, d = int(r["value"]), int(r["valueDecimals"])
                idx = int(r["feedbackIndex"])
            except (ValueError, TypeError, KeyError):
                continue
            if (aid, r["clientAddress"], idx) in revoked:
                continue
            per_agent[aid].append((v, d))
    return per_agent


def main():
    chain = sys.argv[1] if len(sys.argv) > 1 else "base"
    url = P.CHAINS[chain]
    print(f"chain={chain}")
    print(f"{SIG}")
    print(f"selector 0x{SELECTOR.hex()}\n")

    full = load_agents_with_clients(chain)
    per_agent = {a: [(v, d) for v, d, _ in recs] for a, recs in full.items()}
    print(f"local model: {len(per_agent):,} agents with live feedback")

    # The deployed contract rejects an empty clientAddresses array, unlike the
    # GitHub HEAD source. Surface the exact revert string before proceeding.
    probe, err = call_get_summary(url, next(iter(per_agent)))
    print(f"\nempty-clientAddresses probe -> {err if err else probe}")

    explicit = [int(a) for a in sys.argv[2:]]
    if explicit:
        targets = explicit
    else:
        # the saturated agent, plus a spread of ordinary ones as controls
        saturated = [a for a, recs in per_agent.items()
                     if local_summary(recs)[1] in (INT128_MAX, INT128_MIN)]
        ordinary = sorted(per_agent, key=lambda a: -len(per_agent[a]))[:8]
        targets = saturated + [a for a in ordinary if a not in saturated]
        print(f"locally-saturated agents: {saturated}")

    print("\n" + "=" * 88)
    print(f"{'agent':>8} {'src':>6} {'count':>7} {'decimals':>9}  summaryValue")
    print("=" * 88)

    agree = disagree = failed = 0
    for aid in targets:
        recs = per_agent.get(aid, [])
        lc, lv, ld = local_summary(recs)
        clients = sorted({c for _, _, c in full.get(aid, [])})
        got, err = call_get_summary(url, aid, clients)
        if err:
            print(f"{aid:>8} {'CHAIN':>6}  eth_call failed: "
                  f"{str(err)[:60]}")
            failed += 1
            continue
        cc, cv, cd = got
        match = (cc == lc and cv == lv and cd == ld)
        sat = ""
        if cv == INT128_MAX:
            sat = "   <-- SATURATED AT int128.max"
        elif cv == INT128_MIN:
            sat = "   <-- SATURATED AT int128.min"
        print(f"{aid:>8} {'chain':>6} {cc:>7} {cd:>9}  {cv}{sat}")
        print(f"{'':>8} {'local':>6} {lc:>7} {ld:>9}  {lv}"
              f"{'   MATCH' if match else '   *** MISMATCH ***'}")
        if match:
            agree += 1
        else:
            disagree += 1

    print("=" * 88)
    print(f"agreement: {agree} match, {disagree} mismatch, {failed} call failures")
    if disagree:
        print("\n!! Local model diverges from the deployed contract. Do not")
        print("   report any derived statistic until this is reconciled.")
    elif agree:
        print("\nLocal re-implementation reproduces the deployed aggregation")
        print("exactly on every agent tested.")


if __name__ == "__main__":
    main()
