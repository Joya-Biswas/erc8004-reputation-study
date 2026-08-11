# Reading the Score: Integrity and Availability Failures in a Live On-Chain Agent Reputation Registry

Measurement study of the deployed [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004)
("Trustless Agents") Identity, Reputation and Validation registries on Base and
Ethereum, covering **712,987 on-chain events**.

Prior work asked whether ERC-8004's reputation score can be *trusted*. We ask
whether it can be **read at all** — and we implement and evaluate the mitigations
that prior work recommended but did not build.

> **Status:** manuscript under preparation for submission and **not included
> here**. This repository holds the collection, analysis and verification code,
> the scarce datasets, and the results. The paper will be added once it has a
> venue. Every figure below is reproducible from these scripts and validated
> against the deployed contract by `eth_call`.

---

## Findings

**1. An availability failure.** `getSummary()` iterates over every supplied
reviewer, costing a measured **7,441 gas per reviewer**. Past the caller's gas
budget the call reverts and the agent's reputation becomes unreadable. Because
anyone may add reviewers and nobody may remove them, this is purchasable:

| Consumer gas budget | Reviewers affordable | Adversary cost |
|---|---|---|
| 1M (on-chain contract) | 128 | **USD 0.35** |
| 10M | 1,338 | USD 3.61 |
| 30M (generous off-chain) | 4,025 | **USD 10.87** |
| 100M | 13,432 | USD 36.27 |

One agent on Base (`agentId` 55985, 8,926 reviewers) is **already in this state**
and has no recovery action: `revokeFeedback` authorises only a record's original
author.

**2. A negative result on a published recommendation.** Prior work recommends
typing the `value` field. Implemented in isolation it makes robustness *worse*.
Range enforcement is the load-bearing fix:

| Rule | Agents moved ≥50% | Median shift | Entropy (bits) |
|---|---|---|---|
| R0 deployed `getSummary` | 100.00% | 4.05×10³⁵ × | 3.307 |
| R1 + per-tag only | 100.00% | **7.88×10³⁵ ×** | 2.829 |
| R2 + range clamp [0,100] | 20.23% | 8.82% | 2.803 |
| **R3 + median** | **15.06%** | **0.51%** | 2.505 |
| R4 + one record per reviewer | 25.14% | 8.82% | 2.515 |
| R5 + 10% trimmed mean | 28.17% | 8.82% | 2.502 |

**3. Unbounded per-record influence.** `giveFeedback` validates only
`valueDecimals <= 18`; `value` accepts any `int128`. Injecting one record at
`int128.max` shifts **100.00%** of rated agents' scores by at least half. Agent
54330's live on-chain summary is presently
`19310950387549289834657363672971570728`, where a 0–100 rating is expected.

**4. The Validation Registry is deployed and unused.** Prior work deferred this
component as undeployed. It is now live, with **74 `ValidationRequest`** and
**68 `ValidationResponse`** events in total against 451,846 feedback records —
and they come from **nine distinct contracts**, not the per-chain singleton the
specification describes. The `0x8004…` vanity address matching the other two
registries accounts for only 12 of the 74.

Additionally: **66.4% of all feedback on Base** is a single tag
(`miner-vouch`) carrying the constant value `1`, and **the median rated agent has
exactly one reviewer**.

---

## Two things that will waste your time if you don't know them

**1. The deployed contract is not the published reference implementation.**
The reference source returns a raw *sum* and falls back to a stored client list.
The deployed contract returns the **mean** (integer division, truncating toward
zero) and **reverts with `"clientAddresses required"`** on an empty array.
Modelling the reference source gives wrong answers. `scripts/verify_onchain.py`
validates the local model against the chain — currently exact on every sampled
agent (41/41 and 25/25).

**2. Alchemy's free tier caps `eth_getLogs` at a 10-block range**, which makes a
full historical scan impossible. These scripts use Tenderly's public gateway,
which serves 1M-block ranges and needs **no API key**.

---

## Repository layout

```
scripts/     collection, analysis and verification code
data/        small scarce datasets + full manifest with checksums
results/     paper_numbers.json — every statistic, machine-readable
figures/     generated plots (regenerate with scripts/make_figures.py)
```

### The large datasets are not committed

The raw event CSVs total roughly 240 MB, and `feedback_base.csv` alone is 184 MB
— over GitHub's 100 MB file limit. Only the small, hard-to-reacquire files are
committed here (validation events, revocations). Everything else is regenerated
exactly by the scripts; `data/DATA_MANIFEST.md` records row counts, SHA-256
prefixes and the frozen block range.

---

## Reproducing

```bash
pip install requests web3 matplotlib numpy eth-abi

python scripts/pull_data.py            # all events, resumable
python scripts/pull_validation.py      # Validation Registry
python scripts/saturation.py base      # also collects revocations
python scripts/paper_numbers.py        # -> results/paper_numbers.json
python scripts/evaluate_fixes.py base  # mitigation evaluation
python scripts/dos_threshold.py base 30000000
python scripts/verify_onchain.py base  # ground truth vs deployed contract
python scripts/audit.py                # re-checks every claim in the paper
python scripts/make_figures.py base    # -> figures/
```

`pull_data.py` checkpoints after every block range, so it can be interrupted and
resumed. Delete `.progress_*.json` to force a clean re-pull. Expect the Base
feedback stream to take a while — it is ~450k events.

### Verification

`scripts/audit.py` re-derives every quantitative claim in the manuscript from
source and reports PASS/FAIL per claim, reading expected values from
`results/paper_numbers.json` so the audit cannot drift from the paper. Current
state: **49 checks, 0 failures.**

### Frozen snapshot

The chain advances continuously, so all statistics come from a frozen block
range. All three event streams on each chain terminated at the same block, so
the snapshot is internally consistent:

| Chain | Blocks | Dates |
|---|---|---|
| Base | 41,663,783 – 49,819,859 | 3 Feb – 11 Aug 2026 |
| Ethereum | 24,339,873 – 25,729,914 | 29 Jan – 11 Aug 2026 |

---

## Contracts studied

| Registry | Address (both chains) |
|---|---|
| Identity | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |
| Reputation | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` |

Both are ERC-1967 proxies, and both chains delegate to the same implementations.
The Validation Registry has no canonical deployment; see finding 4.

---

## Ethics

All data analysed is public on-chain data. No personal information is processed
and no deanonymisation is attempted. **All measurement was strictly read-only:
no transactions were issued and no described weakness was exercised against any
live agent.** The weaknesses reported arise from behaviour fully visible in
open-source, publicly deployed contract code; the reference implementation's own
documentation warns that unfiltered aggregation is subject to denial of service,
and the specification's Security Considerations already state that Sybil attacks
are possible. No exploit code or operational tooling is provided here.

---

## Related work

This study builds directly on:

> Xiong, Li, Wei, Wang, Knottenbelt, Wang. *Can Trustless Agents Be Trusted? An
> Empirical Study of the ERC-8004 Decentralized AI Agent Ecosystem.*
> arXiv:2606.26028 (2026).

Their window closed 13 May 2026 and covered integrity. This work extends the
window by three months, adds the availability dimension, measures the Validation
Registry they deferred, and implements and evaluates their recommendations.

---


