"""
Reputation readability under gas limits (denial-of-service measurement)
=======================================================================
The deployed getSummary() loops over every supplied client and every feedback
index for that client. Cost therefore grows with an agent's reviewer count.
Past some size the call exceeds the caller's gas allowance and reverts, and
the agent's on-chain reputation becomes unreadable.

The contract's own NatSpec acknowledges this:
    "For agents with many feedback entries, calling without filters may exceed
     gas limits. ALWAYS use the clientAddresses filter for popular agents to
     prevent DoS."
But the prescribed mitigation - pass only trusted clients - presumes the caller
already knows which reviewers to trust, which is the question reputation is
meant to answer. A caller who wants the honest aggregate over all reviewers has
no gas-safe way to obtain it.

This script measures where the boundary actually falls, at an explicit gas cap
so the result does not depend on a provider's default eth_call limit.

Read-only: eth_call only.

RUN:  python dos_threshold.py [chain] [gas]
"""

import bisect
import collections
import sys

from eth_abi import encode as abi_encode
from web3 import Web3

import pull_data as P
from verify_onchain import SELECTOR, load_agents_with_clients

CHAIN = sys.argv[1] if len(sys.argv) > 1 else "base"
GAS_CAP = int(sys.argv[2]) if len(sys.argv) > 2 else 30_000_000


def try_summary(url, agent_id, clients, gas):
    payload = SELECTOR + abi_encode(
        ["uint256", "address[]", "string", "string"],
        [agent_id, clients, "", ""])
    _, err = P.rpc(url, "eth_call", [{
        "to": P.REPUTATION_REGISTRY,
        "data": "0x" + payload.hex(),
        "gas": hex(gas),
    }, "latest"], 90)
    if err is None:
        return True, None
    return False, str(err.get("message", err))[:70]


def main():
    url = P.CHAINS[CHAIN]
    print(f"chain={CHAIN}  gas cap={GAS_CAP:,}")
    print(f"(Base block gas limit is far above this; 30M is a generous "
          f"allowance for a single read)\n")

    full = load_agents_with_clients(CHAIN)
    profile = {}
    for aid, recs in full.items():
        profile[aid] = (len({c for _, _, c in recs}), len(recs))
    by_clients = sorted(profile, key=lambda a: profile[a][0])
    counts = [profile[a][0] for a in by_clients]
    print(f"agents with feedback: {len(full):,}")
    print(f"reviewer-count distribution: min={counts[0]} "
          f"median={counts[len(counts)//2]} max={counts[-1]}")

    # Probe across the reviewer-count range to locate the failure boundary.
    print("\nprobing readability across reviewer counts:")
    probes, seen_sizes = [], set()
    for frac in [i / 40 for i in range(1, 40)] + [0.995, 0.999, 1.0]:
        idx = min(len(by_clients) - 1, int(frac * len(by_clients)))
        aid = by_clients[idx]
        if profile[aid][0] in seen_sizes:
            continue
        seen_sizes.add(profile[aid][0])
        probes.append(aid)

    results = []
    for aid in probes:
        nclients, nrecs = profile[aid]
        ok, msg = try_summary(url, aid, sorted({c for _, _, c in full[aid]}),
                              GAS_CAP)
        results.append((nclients, nrecs, aid, ok))
        flag = "readable" if ok else f"UNREADABLE ({msg})"
        print(f"  agent {aid:<7} reviewers={nclients:<6} records={nrecs:<6} {flag}")

    ok_sizes = [c for c, _, _, ok in results if ok]
    bad_sizes = [c for c, _, _, ok in results if not ok]
    if not bad_sizes:
        print("\nno agent failed at this gas cap; the ceiling is above the "
              "largest agent currently on chain")
        return
    boundary_lo = max(ok_sizes) if ok_sizes else 0
    boundary_hi = min(bad_sizes)
    print(f"\nfailure boundary between {boundary_lo:,} and "
          f"{boundary_hi:,} reviewers")

    # Count how many agents sit above the observed failure point.
    threshold = boundary_hi
    over = sum(1 for a in profile if profile[a][0] >= threshold)
    print(f"agents with >= {threshold:,} reviewers: {over:,} "
          f"({100*over/len(profile):.3f}% of rated agents)")

    # And how much of the ecosystem's feedback those agents hold.
    tot_recs = sum(n for _, n in profile.values())
    over_recs = sum(n for c, n in profile.values() if c >= threshold)
    print(f"they hold {over_recs:,} of {tot_recs:,} feedback records "
          f"({100*over_recs/tot_recs:.2f}%)")

    print("\nNote: the boundary scales with the caller's gas allowance, so this")
    print("is a property of the caller, not a fixed protocol constant. An")
    print("on-chain consumer (a contract calling getSummary within a")
    print("transaction) has far less headroom than 30M and hits it sooner.")


if __name__ == "__main__":
    main()
