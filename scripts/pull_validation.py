"""
ERC-8004 Validation Registry collection
=======================================
The Validation Registry is the third ERC-8004 registry. Xiong et al. (arXiv
2606.26028) could not study it - "no mainnet deployment of this component was
observed during our data collection window" (through 13 May 2026) - and named
it as future work. It has since been deployed and used, so this script
captures it.

Event signatures taken from the reference implementation
(ChaosChain/trustless-agents-erc-ri, src/interfaces/IValidationRegistry.sol):

  ValidationRequest(address indexed validatorAddress, uint256 indexed agentId,
                    string requestURI, bytes32 indexed requestHash)
  ValidationResponse(address indexed validatorAddress, uint256 indexed agentId,
                     bytes32 indexed requestHash, uint8 response,
                     string responseURI, bytes32 responseHash, string tag)

Note: unlike Identity/Reputation, validation events on Base come from MULTIPLE
contract addresses rather than a single per-chain singleton. This script does
NOT filter by address - it scans for the event topics chain-wide and records
which contract emitted each one, because the fragmentation is itself a finding.

OUTPUT: validation_requests_<chain>.csv, validation_responses_<chain>.csv
"""

import csv
import sys

from web3 import Web3

import pull_data as P

SIG_REQUEST = "ValidationRequest(address,uint256,string,bytes32)"
SIG_RESPONSE = ("ValidationResponse(address,uint256,bytes32,uint8,"
                "string,bytes32,string)")

T_REQUEST = P.topic0(SIG_REQUEST)
T_RESPONSE = P.topic0(SIG_RESPONSE)

# Verified against live Base logs before this script was written.
EXPECTED = {
    T_REQUEST: "0x530436c3634a98e1e626b0898be2f1e9980cc1bd2a78c07a0aba52d0a48a5059",
    T_RESPONSE: "0xafddf629e874ccc3963b6a888c477bd464a6c8525024fc88759ea3b2326349ae",
}

ERC1967_IMPL_SLOT = ("0x360894a13ba1a3210667c828492db98d"
                     "ca3e2076cc3735a920a3ca505d382bbc")

REQ_FIELDS = ["contract", "validatorAddress", "agentId", "requestHash",
              "requestURI", "blockNumber", "timestamp", "txHash", "logIndex"]
RES_FIELDS = ["contract", "validatorAddress", "agentId", "requestHash",
              "response", "responseURI", "responseHash", "tag",
              "blockNumber", "timestamp", "txHash", "logIndex"]


def decode_request(log):
    d = log["data"][2:]
    return {
        "contract": Web3.to_checksum_address(log["address"]),
        "validatorAddress": P._addr(log["topics"][1]),
        "agentId": int(log["topics"][2], 16),
        "requestHash": log["topics"][3],
        "requestURI": P._read_string(d, 0) if d else "",
        "blockNumber": int(log["blockNumber"], 16),
        "timestamp": None,
        "txHash": log["transactionHash"],
        "logIndex": int(log["logIndex"], 16),
    }


def decode_response(log):
    """head: [0]=response(uint8) [1]=off(responseURI) [2]=responseHash
             [3]=off(tag)"""
    d = log["data"][2:]
    return {
        "contract": Web3.to_checksum_address(log["address"]),
        "validatorAddress": P._addr(log["topics"][1]),
        "agentId": int(log["topics"][2], 16),
        "requestHash": log["topics"][3],
        "response": P._word(d, 0),
        "responseURI": P._read_string(d, 1),
        "responseHash": "0x" + d[2 * 64:3 * 64],
        "tag": P._read_string(d, 3),
        "blockNumber": int(log["blockNumber"], 16),
        "timestamp": None,
        "txHash": log["transactionHash"],
        "logIndex": int(log["logIndex"], 16),
    }


def scan(url, topic, lo, hi, window=1_000_000):
    """Chain-wide scan for a topic, no address filter."""
    out, b = [], hi
    while b > lo:
        start = max(lo, b - window)
        logs, err = P.rpc(url, "eth_getLogs", [{
            "topics": [topic], "fromBlock": hex(start), "toBlock": hex(b)}])
        if err:
            print(f"    window {start:,}-{b:,} error: {err}")
            b = start - 1
            continue
        out.extend(logs or [])
        b = start - 1
    return out


def check_proxy(url, addr, block):
    val, err = P.rpc(url, "eth_getStorageAt",
                     [addr, ERC1967_IMPL_SLOT, hex(block)], 30)
    if err or not val or int(val, 16) == 0:
        return None
    return Web3.to_checksum_address("0x" + val[-40:])


def main():
    for got, want in EXPECTED.items():
        if got != want:
            raise SystemExit(f"topic0 drift: {got} != {want}")
    print("topic0 self-check passed")
    print(f"  ValidationRequest  {T_REQUEST}")
    print(f"  ValidationResponse {T_RESPONSE}")

    chains = sys.argv[1:] or list(P.CHAINS)
    for chain in chains:
        url = P.CHAINS[chain]
        print(f"\n{'='*70}\nCHAIN: {chain.upper()}\n{'='*70}")
        try:
            head = P.latest_block(url)
        except Exception as e:
            print(f"  cannot reach RPC: {e}")
            continue
        dep = P.find_deployment_block(url, P.IDENTITY_REGISTRY, head)
        if dep is None:
            print("  identity registry absent; skipping")
            continue
        print(f"  scanning blocks {dep:,} - {head:,}")
        spacing = P.verify_block_spacing(chain, url, dep, head)

        for label, topic, decoder, fields in (
            ("validation_requests", T_REQUEST, decode_request, REQ_FIELDS),
            ("validation_responses", T_RESPONSE, decode_response, RES_FIELDS),
        ):
            logs = scan(url, topic, dep, head)
            rows = []
            for l in logs:
                try:
                    rows.append(decoder(l))
                except Exception as e:
                    print(f"    decode failed {l.get('transactionHash')}: {e}")
            if rows:
                P.attach_timestamps(url, rows, {}, spacing)
            rows.sort(key=lambda r: (r["blockNumber"], r["logIndex"]))

            path = f"{label}_{chain}.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            print(f"  {label}: {len(rows)} events -> {path}")

            by_contract = {}
            for r in rows:
                by_contract[r["contract"]] = by_contract.get(r["contract"], 0) + 1
            for addr, n in sorted(by_contract.items(), key=lambda kv: -kv[1]):
                impl = check_proxy(url, addr, head)
                tag = f"ERC-1967 proxy -> {impl}" if impl else "not a 1967 proxy"
                print(f"      {addr}  x{n:<4} {tag}")


if __name__ == "__main__":
    main()
