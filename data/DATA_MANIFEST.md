# Data manifest

The raw event CSVs total roughly 350 MB and are not bundled in this package.
They are reproduced exactly by `scripts/pull_data.py`, `scripts/saturation.py`
(which collects revocations) and `scripts/pull_validation.py`.

## Frozen snapshot

The chain advances continuously, so all statistics in the paper are computed
from an explicitly frozen block range. All three event streams on each chain
terminated at the same block, so the snapshot is internally consistent.

| Chain | First block | Last block (freeze) | Calendar range |
|---|---|---|---|
| Base | 41,663,783 | 49,819,859 | 2026-02-03 – 2026-08-11 |
| Ethereum | 24,339,873 | 25,729,914 | 2026-01-29 – 2026-08-11 |

`scripts/paper_numbers.py` enforces the freeze by discarding any row with
`blockNumber` above the limit. The CSV files on disk contain a small number of
rows beyond it, captured by the final resumed collection run; the table below
therefore lists both the raw file contents and the post-freeze counts used in
the paper. **Only the post-freeze counts appear in the manuscript.**

| File | Bytes | Raw rows | Post-freeze rows | SHA-256 (first 16) |
|---|---|---|---|---|
| `feedback_base.csv` | 193,699,288 | 451,875 | **451,846** | C07851B41BA7CFB5 |
| `registered_base.csv` | 19,105,891 | 61,429 | **61,425** | D65BBDBF5FD27A6F |
| `transfer_base.csv` | 14,878,457 | 80,932 | **80,932** | 774ADF3AFCC46DB4 |
| `revoked_base.csv` | 10,528 | 82 | **82** | 6995F5DF0E59A40F |
| `feedback_ethereum.csv` | 1,631,920 | 3,215 | **3,215** | FAA580E102523A11 |
| `registered_ethereum.csv` | 12,055,775 | 49,620 | **49,613** | A609BC8CE23BC7B5 |
| `transfer_ethereum.csv` | 12,086,171 | 65,732 | **65,732** | 7641F96E8C6D09C9 |
| `revoked_ethereum.csv` | 56 | 0 | **0** | 87450BAD8BA7C851 |
| `validation_requests_base.csv` | 119,837 | 74 | **74** | 6DDC56814972ED62 |
| `validation_responses_base.csv` | 24,638 | 68 | **68** | 65EDC8EBF3B89817 |

Checksums describe the files as collected on 11 August 2026. Re-running
collection at a later date produces a superset, not an identical file, because
the chain has advanced. Reproduction should therefore compare post-freeze
counts, not file hashes.

## Contracts

| Registry | Address (both chains) | Implementation |
|---|---|---|
| Identity | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | `0x7274e874ca62410a93bd8bf61c69d8045e399c02` |
| Reputation | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | `0x16e0fa7f7c56b9a767e34b192b51f921be31da34` |

Both are ERC-1967 proxies, and both chains delegate to the same
implementations. The Validation Registry has no single canonical deployment:
nine distinct contracts emit its events on Base, resolving to six
implementations. The address matching the `0x8004` vanity pattern
(`0x8004Cc8439f36fd5F9F049D9fF86523Df6dAAB58`) accounts for only 12 of the 74
requests.

## Event signatures

Confirmed against the reference interfaces and two independent public signature
databases, then matched against live logs before decoding.

| Event | topic0 |
|---|---|
| `Registered(uint256,string,address)` | `0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a` |
| `NewFeedback(uint256,address,uint64,int128,uint8,string,string,string,string,string,bytes32)` | `0x6a4a61743519c9d648a14e6493f47dbe3ff1aa29e7785c96c8326a205e58febc` |
| `FeedbackRevoked(uint256,address,uint64)` | `0x25156fd3288212246d8b008d5921fde376c71ed14ac2e072a506eb06fde6d09d` |
| `Transfer(address,address,uint256)` | `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef` |
| `ValidationRequest(address,uint256,string,bytes32)` | `0x530436c3634a98e1e626b0898be2f1e9980cc1bd2a78c07a0aba52d0a48a5059` |
| `ValidationResponse(address,uint256,bytes32,uint8,string,bytes32,string)` | `0xafddf629e874ccc3963b6a888c477bd464a6c8525024fc88759ea3b2326349ae` |

## Data source

Tenderly's public gateway (`base.gateway.tenderly.co`,
`mainnet.gateway.tenderly.co`), which serves 1M-block `eth_getLogs` ranges
without an API key. Alchemy's free tier caps the same call at a **10-block**
range, which makes a full historical scan infeasible.

Base timestamps are computed from a verified anchor rather than fetched: OP-stack
block production is deterministic at two seconds, verified exact at thirteen
points across the full range. The collector aborts the shortcut if any sample
disagrees.
