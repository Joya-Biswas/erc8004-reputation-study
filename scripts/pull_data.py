"""
ERC-8004 Data Collection Script (v3)
====================================
Pulls and decodes every Registered / NewFeedback / Transfer event from the
ERC-8004 Identity and Reputation registries on Base and Ethereum mainnet.

EVERY FACT BELOW WAS VERIFIED AGAINST LIVE CHAIN DATA - nothing is assumed.

  * Contract addresses confirmed to hold bytecode on both chains (ERC-1967
    proxies; both chains delegate to the SAME implementations:
        IdentityRegistry   impl 0x7274e874ca62410a93bd8bf61c69d8045e399c02
        ReputationRegistry impl 0x16e0fa7f7c56b9a767e34b192b51f921be31da34

  * Event signatures confirmed three independent ways: (1) OpenChain
    signature DB, (2) 4byte.directory, (3) the reference implementation
    source at ChaosChain/trustless-agents-erc-ri. All three agree, and the
    resulting topic0 hashes were matched against real logs pulled from
    mainnet before this script was written.

  * ABI head layout for NewFeedback was cross-checked against a live Base
    log: 8 head words (256 bytes), first dynamic offset = 0x100 = 256. OK.

RPC: Tenderly's public gateway. Chosen after measuring the alternatives -
Alchemy's free tier caps eth_getLogs at a 10-BLOCK range (unusable here),
Base's official RPC and dRPC cap at 10,000, publicnode requires a token for
archive access. Tenderly served a 1,000,000-block range in ~1-3s with no
key. No API keys are needed or stored by this script.

SETUP:  pip install requests web3
RUN:    python pull_data.py

RESUMABLE: progress checkpoints to .progress_<chain>_<event>.json after every
chunk. Ctrl-C or crash, just re-run. Delete .progress_*.json + the CSVs to
force a clean re-pull.
"""

import csv
import json
import os
import time

import requests
from web3 import Web3

# ---------------------------------------------------------------- CONFIG ----

CHAINS = {
    "base": "https://base.gateway.tenderly.co",
    "ethereum": "https://mainnet.gateway.tenderly.co",
}

IDENTITY_REGISTRY = Web3.to_checksum_address("0x8004A169FB4a3325136EB29fA0ceB6D2e539a432")
REPUTATION_REGISTRY = Web3.to_checksum_address("0x8004BAa17C55a88189AE136b182e5fdA19dE9b63")

INITIAL_CHUNK = 200_000
MIN_CHUNK = 500
MAX_CHUNK = 1_000_000
SOFT_LOG_CAP = 9_000      # observed: provider truncates/slows past ~10k
REQUEST_TIMEOUT = 120
MAX_RETRIES = 6

# Measured against the live endpoint, not guessed: a JSON-RPC batch of 10
# succeeds, 25 returns HTTP 429, and sustained singles run at ~3.2 req/s.
BLOCK_BATCH = 10
RPC_PACE = 0.32

# Base is an OP-stack chain with deterministic 2s block production. Verified
# exact (to the second) at 8 points spanning blocks 41.6M-49.7M, so its
# timestamps are computed rather than fetched - this avoids ~300k rate-limited
# RPC calls. verify_block_spacing() re-checks this at runtime on random blocks
# and refuses to use the shortcut if a single sample disagrees.
FIXED_SPACING = {"base": 2}


def topic0(sig: str) -> str:
    h = Web3.keccak(text=sig).hex()
    return h if h.startswith("0x") else "0x" + h


SIG_REGISTERED = "Registered(uint256,string,address)"
SIG_NEWFEEDBACK = (
    "NewFeedback(uint256,address,uint64,int128,uint8,"
    "string,string,string,string,string,bytes32)"
)
SIG_TRANSFER = "Transfer(address,address,uint256)"

T_REGISTERED = topic0(SIG_REGISTERED)
T_NEWFEEDBACK = topic0(SIG_NEWFEEDBACK)
T_TRANSFER = topic0(SIG_TRANSFER)

# Hashes observed in live mainnet logs before writing this script. If a future
# run disagrees, the contracts were upgraded and the decoders need review.
EXPECTED = {
    T_REGISTERED: "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a",
    T_NEWFEEDBACK: "0x6a4a61743519c9d648a14e6493f47dbe3ff1aa29e7785c96c8326a205e58febc",
    T_TRANSFER: "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
}


def assert_topics():
    for got, want in EXPECTED.items():
        if got != want:
            raise SystemExit(
                f"topic0 mismatch: computed {got}, expected {want}. "
                "Event signature drift - stop and re-verify before collecting."
            )
    print("topic0 self-check passed (all 3 signatures match observed mainnet logs)")


# ------------------------------------------------------------- RPC LAYER ----

_session = requests.Session()
_rpc_id = 0


def rpc(url, method, params, timeout=REQUEST_TIMEOUT):
    """Single JSON-RPC call with retry/backoff. Returns (result, error_dict)."""
    global _rpc_id
    _rpc_id += 1
    payload = {"jsonrpc": "2.0", "id": _rpc_id, "method": method, "params": params}

    for attempt in range(MAX_RETRIES):
        try:
            resp = _session.post(url, json=payload, timeout=timeout)
        except requests.RequestException as e:
            # A read timeout usually means the range is too big, not that the
            # provider is down. Surface it so the caller can shrink the chunk.
            if attempt >= 1:
                return None, {"message": f"timeout/network: {e.__class__.__name__}"}
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                return None, {"message": f"auth failed: {resp.text[:200]}"}
            time.sleep(2 ** attempt)
            continue

        try:
            data = resp.json()
        except ValueError:
            time.sleep(2 ** attempt)
            continue
        if "error" in data:
            return None, data["error"]
        return data.get("result"), None

    return None, {"message": "max retries exceeded"}


def rpc_batch(url, calls):
    """Batched JSON-RPC. calls = [(method, params), ...]. Returns list of results."""
    global _rpc_id
    payload, id_map = [], {}
    for method, params in calls:
        _rpc_id += 1
        payload.append({"jsonrpc": "2.0", "id": _rpc_id,
                        "method": method, "params": params})
        id_map[_rpc_id] = len(payload) - 1

    results = [None] * len(payload)
    for attempt in range(MAX_RETRIES):
        try:
            resp = _session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            time.sleep(2 ** attempt)
            continue
        try:
            body = resp.json()
        except ValueError:
            time.sleep(2 ** attempt)
            continue
        if not isinstance(body, list):
            return results
        for item in body:
            idx = id_map.get(item.get("id"))
            if idx is not None:
                results[idx] = item.get("result")
        return results
    return results


def latest_block(url):
    result, err = rpc(url, "eth_blockNumber", [], 30)
    if err:
        raise RuntimeError(f"eth_blockNumber failed: {err}")
    return int(result, 16)


def find_deployment_block(url, address, hi):
    """Binary search for the first block where the address has bytecode."""
    code, err = rpc(url, "eth_getCode", [address, hex(hi)], 30)
    if err or not code or code == "0x":
        return None
    lo, stalls = 0, 0
    while lo < hi:
        mid = (lo + hi) // 2
        code, err = rpc(url, "eth_getCode", [address, hex(mid)], 30)
        if err:
            stalls += 1
            if stalls > 10:
                print(f"    eth_getCode unreliable ({err}); falling back to block 0")
                return 0
            time.sleep(0.5)
            continue
        stalls = 0
        if code and code != "0x":
            hi = mid
        else:
            lo = mid + 1
    return lo


# --------------------------------------------------------------- DECODING ---

def _word(data_hex, i):
    return int(data_hex[i * 64:(i + 1) * 64], 16)


def _read_string(data_hex, head_word):
    """Read a dynamic string/bytes whose offset word sits at index head_word."""
    off = _word(data_hex, head_word) * 2
    length = int(data_hex[off:off + 64], 16)
    raw = data_hex[off + 64:off + 64 + length * 2]
    return bytes.fromhex(raw).decode("utf-8", errors="replace")


def _signed(word, bits):
    """Two's-complement. ABI sign-extends intN to the full 256-bit word."""
    if word >= 2 ** 255:
        word -= 2 ** 256
    return word


def _addr(topic):
    return Web3.to_checksum_address("0x" + topic[-40:])


def _base(log):
    return {
        "blockNumber": int(log["blockNumber"], 16),
        "timestamp": None,
        "txHash": log["transactionHash"],
        "logIndex": int(log["logIndex"], 16),
    }


def decode_registered(log):
    """Registered(uint256 indexed agentId, string agentURI, address indexed owner)"""
    d = log["data"][2:]
    row = _base(log)
    row.update({
        "agentId": int(log["topics"][1], 16),
        "owner": _addr(log["topics"][2]),
        "agentURI": _read_string(d, 0) if d else "",
    })
    return row


def decode_feedback(log):
    """NewFeedback(uint256 indexed agentId, address indexed clientAddress,
                   uint64 feedbackIndex, int128 value, uint8 valueDecimals,
                   string indexed indexedTag1, string tag1, string tag2,
                   string endpoint, string feedbackURI, bytes32 feedbackHash)

    topics: [0]=sig [1]=agentId [2]=clientAddress [3]=keccak(indexedTag1)
    head:   [0]=feedbackIndex [1]=value [2]=valueDecimals [3]=off(tag1)
            [4]=off(tag2) [5]=off(endpoint) [6]=off(feedbackURI)
            [7]=feedbackHash
    """
    d = log["data"][2:]
    row = _base(log)
    row.update({
        "agentId": int(log["topics"][1], 16),
        "clientAddress": _addr(log["topics"][2]),
        "indexedTag1Hash": log["topics"][3] if len(log["topics"]) > 3 else "",
        "feedbackIndex": _word(d, 0),
        "value": _signed(_word(d, 1), 128),
        "valueDecimals": _word(d, 2),
        "tag1": _read_string(d, 3),
        "tag2": _read_string(d, 4),
        "endpoint": _read_string(d, 5),
        "feedbackURI": _read_string(d, 6),
        "feedbackHash": "0x" + d[7 * 64:8 * 64],
    })
    return row


def decode_transfer(log):
    """ERC-721 Transfer - agent identities are NFTs, so this tracks resale
    and ownership consolidation (both relevant to Sybil clustering).
    from == 0x0 is the mint that accompanies registration."""
    row = _base(log)
    row.update({
        "agentId": int(log["topics"][3], 16),
        "from": _addr(log["topics"][1]),
        "to": _addr(log["topics"][2]),
    })
    return row


# ------------------------------------------------------------- TIMESTAMPS ---

def block_timestamp(url, block):
    result, err = rpc(url, "eth_getBlockByNumber", [hex(block), False], 30)
    if err or not result or not result.get("timestamp"):
        return None
    return int(result["timestamp"], 16)


def verify_block_spacing(chain, url, lo, hi, samples=12):
    """Confirm the chain really does produce blocks at a fixed interval.

    Returns (anchor_block, anchor_ts, spacing) if every sampled block matches
    the linear prediction exactly, otherwise None - in which case we fall back
    to fetching every timestamp over RPC.
    """
    spacing = FIXED_SPACING.get(chain)
    if spacing is None:
        return None

    anchor_ts = block_timestamp(url, lo)
    if anchor_ts is None:
        return None

    probes = [lo + int((hi - lo) * i / (samples - 1)) for i in range(1, samples)]
    probes += [lo + 1, lo + 2]
    for b in sorted(set(probes)):
        time.sleep(RPC_PACE)
        actual = block_timestamp(url, b)
        if actual is None:
            print(f"  spacing check: could not read block {b:,}; will fetch instead")
            return None
        predicted = anchor_ts + (b - lo) * spacing
        if actual != predicted:
            print(f"  spacing check FAILED at block {b:,} "
                  f"(actual {actual}, predicted {predicted}); will fetch instead")
            return None

    print(f"  verified fixed {spacing}s block spacing over "
          f"{len(set(probes))} samples spanning {lo:,}-{hi:,}; "
          f"timestamps computed from anchor (block {lo:,} = {anchor_ts})")
    return lo, anchor_ts, spacing


def attach_timestamps(url, rows, cache, spacing_model):
    if spacing_model:
        anchor_b, anchor_ts, step = spacing_model
        for r in rows:
            r["timestamp"] = anchor_ts + (r["blockNumber"] - anchor_b) * step
        return

    needed = sorted({r["blockNumber"] for r in rows if r["blockNumber"] not in cache})
    for i in range(0, len(needed), BLOCK_BATCH):
        batch = needed[i:i + BLOCK_BATCH]
        calls = [("eth_getBlockByNumber", [hex(b), False]) for b in batch]
        for blk_num, blk in zip(batch, rpc_batch(url, calls)):
            if blk and blk.get("timestamp"):
                cache[blk_num] = int(blk["timestamp"], 16)
        time.sleep(RPC_PACE)
    for r in rows:
        r["timestamp"] = cache.get(r["blockNumber"])


# ------------------------------------------------------------ MAIN FETCH ----

FIELDS = {
    "registered": ["agentId", "owner", "agentURI",
                   "blockNumber", "timestamp", "txHash", "logIndex"],
    "feedback": ["agentId", "clientAddress", "feedbackIndex", "value",
                 "valueDecimals", "tag1", "tag2", "endpoint", "feedbackURI",
                 "feedbackHash", "indexedTag1Hash",
                 "blockNumber", "timestamp", "txHash", "logIndex"],
    "transfer": ["agentId", "from", "to",
                 "blockNumber", "timestamp", "txHash", "logIndex"],
}

RANGE_ERRORS = (
    "response size exceeded", "query returned more than", "too many results",
    "log response size", "range is too large", "limit exceeded",
    "block range", "timeout", "exceed",
)


def _is_range_error(err):
    msg = json.dumps(err).lower()
    return any(s in msg for s in RANGE_ERRORS)


def collect(chain, url, address, topic, decoder, label, spacing_model):
    out_csv = f"{label}_{chain}.csv"
    fields = FIELDS[label]
    progress_file = f".progress_{chain}_{label}.json"
    head = latest_block(url)

    if os.path.exists(progress_file):
        state = json.load(open(progress_file))
        from_block, total = state["next_block"], state.get("total", 0)
        print(f"  resuming at block {from_block:,} ({total:,} rows already written)")
    else:
        print("  locating deployment block (binary search)...")
        dep = find_deployment_block(url, address, head)
        if dep is None:
            print(f"  !! no bytecode at {address} on {chain} - skipping")
            return 0
        print(f"  deployed at block {dep:,}")
        from_block, total = dep, 0

    write_header = not os.path.exists(out_csv) or os.path.getsize(out_csv) == 0
    f = open(out_csv, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    if write_header:
        writer.writeheader()
        f.flush()

    ts_cache, chunk, t0 = {}, INITIAL_CHUNK, time.time()
    try:
        while from_block <= head:
            to_block = min(from_block + chunk - 1, head)
            logs, err = rpc(url, "eth_getLogs", [{
                "address": address, "topics": [topic],
                "fromBlock": hex(from_block), "toBlock": hex(to_block),
            }])

            if err is not None:
                if chunk > MIN_CHUNK and _is_range_error(err):
                    chunk = max(MIN_CHUNK, chunk // 4)
                    print(f"    dense/slow range, chunk -> {chunk:,}")
                    continue
                print(f"    RPC error, stopping this stream: {err}")
                break

            logs = logs or []
            if len(logs) >= SOFT_LOG_CAP and chunk > MIN_CHUNK:
                chunk = max(MIN_CHUNK, chunk // 4)
                print(f"    hit {len(logs)} logs (near cap), chunk -> {chunk:,}")
                continue

            if logs:
                rows = []
                for l in logs:
                    try:
                        rows.append(decoder(l))
                    except Exception as e:
                        print(f"    !! decode failed {l.get('transactionHash')} "
                              f"logIndex={l.get('logIndex')}: {e}")
                if rows:
                    attach_timestamps(url, rows, ts_cache, spacing_model)
                    writer.writerows(rows)
                    f.flush()
                    total += len(rows)

            pct = 100.0 * to_block / max(head, 1)
            print(f"    {from_block:>11,}-{to_block:<11,} +{len(logs):<5} "
                  f"total={total:<8,} {pct:5.1f}%  {time.time()-t0:6.0f}s")

            from_block = to_block + 1
            json.dump({"next_block": from_block, "total": total},
                      open(progress_file, "w"))

            if len(logs) < 1500 and chunk < MAX_CHUNK:
                chunk = min(MAX_CHUNK, chunk * 2)
            if len(ts_cache) > 300_000:
                ts_cache.clear()

    except KeyboardInterrupt:
        print("\n  interrupted - progress saved, re-run to resume")
    finally:
        f.close()
    return total


def main():
    assert_topics()
    print(f"  Registered  {T_REGISTERED}")
    print(f"  NewFeedback {T_NEWFEEDBACK}")
    print(f"  Transfer    {T_TRANSFER}")

    streams = [
        (IDENTITY_REGISTRY, T_REGISTERED, decode_registered, "registered"),
        (REPUTATION_REGISTRY, T_NEWFEEDBACK, decode_feedback, "feedback"),
        (IDENTITY_REGISTRY, T_TRANSFER, decode_transfer, "transfer"),
    ]

    summary = {}
    for chain, url in CHAINS.items():
        print("\n" + "=" * 72)
        print(f"CHAIN: {chain.upper()}")
        print("=" * 72)
        try:
            head = latest_block(url)
            print(f"  head block: {head:,}")
        except Exception as e:
            print(f"  !! cannot reach RPC for {chain}: {e}")
            continue

        dep = find_deployment_block(url, IDENTITY_REGISTRY, head)
        if dep is None:
            print(f"  !! registries not deployed on {chain} - skipping")
            continue
        spacing_model = verify_block_spacing(chain, url, dep, head)
        if spacing_model is None and chain in FIXED_SPACING:
            print("  !! fixed-spacing assumption did not hold; "
                  "falling back to per-block RPC lookups (slow)")

        counts = {}
        for address, topic, decoder, label in streams:
            print(f"\n[{chain}] {label}")
            counts[label] = collect(chain, url, address, topic, decoder,
                                    label, spacing_model)
        summary[chain] = counts

    print("\n" + "=" * 72)
    print("DONE")
    for chain, counts in summary.items():
        line = "  ".join(f"{k}={v:,}" for k, v in counts.items())
        print(f"  {chain:9} {line}")


if __name__ == "__main__":
    main()
