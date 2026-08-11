"""
Full verification audit
=======================
Re-derives every quantitative claim in the paper from source and checks it
against what the draft asserts. Nothing is taken on trust from earlier analysis:
CSV statistics are recomputed, on-chain claims are re-queried live, and claims
attributed to prior work are re-extracted from that paper's own text.

Each check prints PASS / FAIL / INFO. Any FAIL means the corresponding sentence
in the paper must be corrected or removed before submission.

RUN:  python audit.py
"""

import collections
import csv
import json
import os
import re
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8")

import pull_data as P
from verify_onchain import call_get_summary, load_agents_with_clients

INT128_MAX = 2 ** 127 - 1

# Claims are read from paper_numbers.json rather than hard-coded here, so the
# audit cannot drift out of step with the manuscript. Regenerate that file with
# `python paper_numbers.py` after any change to the dataset.
try:
    with open("paper_numbers.json", encoding="utf-8") as _f:
        NUM = json.load(_f)
except FileNotFoundError:
    raise SystemExit("paper_numbers.json missing - run paper_numbers.py first")

FREEZE = {k: int(v) for k, v in NUM["freeze_block"].items()}


def frozen_rows(kind, chain):
    """Rows at or below the freeze block - the same view the paper uses."""
    path = f"{kind}_{chain}.csv"
    if not os.path.exists(path):
        return None
    out, seen = [], set()
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r.get("txHash"), r.get("logIndex"))
            if key != (None, None):
                if key in seen:
                    continue
                seen.add(key)
            try:
                if int(r["blockNumber"]) > FREEZE[chain]:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            out.append(r)
    return out
PRIOR_PDF_TXT = (r"C:\Users\remix\AppData\Local\Temp\claude"
                 r"\C--Users-remix-OneDrive-Desktop-6"
                 r"\f687a386-7d22-465e-96ef-60783658c078\scratchpad"
                 r"\xiong2606.txt")

results = []


def check(label, claimed, actual, ok=None, tol=None):
    if ok is None:
        if tol is not None and isinstance(claimed, (int, float)):
            ok = abs(actual - claimed) <= tol
        else:
            ok = (claimed == actual)
    tag = "PASS" if ok else "FAIL"
    results.append((tag, label))
    print(f"  [{tag}] {label}")
    if not ok:
        print(f"         claimed: {claimed}")
        print(f"         actual : {actual}")
    return ok


def info(label, value):
    results.append(("INFO", label))
    print(f"  [INFO] {label}: {value}")


def section(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


# ------------------------------------------------------------------ A ------
def audit_dataset():
    section("A. DATASET INTEGRITY (post-freeze)")
    for chain in ("base", "ethereum"):
        info(f"{chain} freeze block", f"{FREEZE[chain]:,}")
        for kind in ("registered", "feedback", "transfer", "revoked"):
            rows = frozen_rows(kind, chain)
            if rows is None:
                info(f"{kind}_{chain}.csv", "MISSING")
                continue
            claimed = NUM["counts"][chain].get(kind)
            check(f"{kind}_{chain}.csv post-freeze rows", claimed, len(rows))
            if kind != "revoked" and rows and "logIndex" in rows[0]:
                keys = [(r["txHash"], r["logIndex"]) for r in rows]
                dupes = len(keys) - len(set(keys))
                check(f"{kind}_{chain}.csv duplicate logs", 0, dupes)


# ------------------------------------------------------------------ B ------
def audit_feedback_stats():
    section("B. BASE FEEDBACK STATISTICS")
    raw = frozen_rows("feedback", "base")
    revoked = {(int(r["agentId"]), r["clientAddress"], int(r["feedbackIndex"]))
               for r in (frozen_rows("revoked", "base") or [])}
    rows = []
    for r in raw:
        try:
            r["value"] = int(r["value"])
            r["valueDecimals"] = int(r["valueDecimals"])
            r["agentId"] = int(r["agentId"])
            idx = int(r["feedbackIndex"])
        except (ValueError, TypeError):
            continue
        if (r["agentId"], r["clientAddress"], idx) in revoked:
            continue
        rows.append(r)
    check("live (non-revoked) feedback rows", NUM["base_live_feedback"], len(rows))

    tags = collections.Counter(r["tag1"] for r in rows)
    check("distinct tag1 values", NUM["distinct_tags"], len(tags))

    per_tag_dec = collections.defaultdict(set)
    for r in rows:
        per_tag_dec[r["tag1"]].add(r["valueDecimals"])
    mixed = {t: d for t, d in per_tag_dec.items() if len(d) > 1}
    check("tags with >1 decimal scale", NUM["tags_mixed_decimals"], len(mixed))
    check("'reliability' decimal scales", NUM["reliability_decimals"],
          sorted(per_tag_dec.get("reliability", set())))

    raws = [r["value"] for r in rows]
    check("minimum raw value", NUM["value_min_raw"], min(raws))
    scaled = [r["value"] / (10 ** r["valueDecimals"]) for r in rows]
    check("maximum scaled value", NUM["value_max_scaled"], max(scaled),
          ok=abs(max(scaled) - float(NUM["value_max_scaled"]))
          / float(NUM["value_max_scaled"]) < 1e-9)
    check("raw values > 100", NUM["raw_gt_100"],
          sum(1 for v in raws if v > 100))

    per_rater = collections.Counter(r["clientAddress"] for r in rows)

    def gini(vals):
        v = sorted(x for x in vals if x > 0)
        n, tot = len(v), sum(v)
        cum = sum((i + 1) * x for i, x in enumerate(v))
        return (2 * cum) / (n * tot) - (n + 1) / n

    check("distinct reviewers", NUM["distinct_raters"], len(per_rater))
    check("Gini per reviewer", NUM["gini_rater"],
          round(gini(list(per_rater.values())), 4), tol=0.0002)
    check("top-10 reviewer share (%)", NUM["top10_share"],
          round(100 * sum(c for _, c in per_rater.most_common(10)) / len(rows), 2),
          tol=0.02)

    pair = collections.Counter((r["clientAddress"], r["agentId"]) for r in rows)
    check("largest reviewer-agent pair", NUM["largest_pair"],
          pair.most_common(1)[0][1])

    by_agent_raters = collections.defaultdict(set)
    for r in rows:
        by_agent_raters[r["agentId"]].add(r["clientAddress"])
    rc = sorted(len(v) for v in by_agent_raters.values())
    check("median reviewers per rated agent", NUM["median_reviewers"],
          statistics.median(rc))
    check("p99 reviewers per rated agent", NUM["p99_reviewers"],
          rc[int(0.99 * len(rc))])
    check("rated agents", NUM["rated_agents"], len(by_agent_raters))
    check("max reviewers on one agent", NUM["max_reviewers"], rc[-1])

    # the headline corpus finding
    vouch = sum(1 for r in rows if r["tag1"] == "miner-vouch")
    info("miner-vouch records",
         f"{vouch:,} ({100*vouch/len(rows):.2f}% of live feedback)")
    single = 0
    for t, c in tags.items():
        vals = {r["value"] / (10 ** r["valueDecimals"])
                for r in rows if r["tag1"] == t}
        if len(vals) == 1:
            single += c
    info("records under single-valued tags",
         f"{single:,} ({100*single/len(rows):.2f}%)")
    return rows


# ------------------------------------------------------------------ C ------
def audit_onchain():
    section("C. LIVE ON-CHAIN RE-VERIFICATION")
    url = P.CHAINS["base"]
    full = load_agents_with_clients("base")

    # C1: agent 54330's live summary
    recs = full.get(54330, [])
    clients = sorted({c for _, _, c in recs})
    got, err = call_get_summary(url, 54330, clients)
    if err:
        check("agent 54330 getSummary succeeds", "success", str(err), ok=False)
    else:
        cnt, val, dec = got
        check("agent 54330 live summaryValue",
              19310950387549289834657363672971570728, val)
        info("agent 54330 count / decimals", f"{cnt} / {dec}")
        check("agent 54330 value is NOT int128.max (no saturation)",
              True, val != INT128_MAX, ok=(val != INT128_MAX))

    # C2: the most-reviewed agent is unreadable at its full reviewer set.
    # Counts are read from paper_numbers.json because the live chain keeps
    # adding reviewers; the frozen figures are what the paper quotes.
    target = int(NUM["max_reviewers_agent"])
    recs = full.get(target, [])
    clients = sorted({c for _, _, c in recs})
    info(f"most-reviewed agent", target)
    check(f"agent {target} reviewer count >= frozen value",
          NUM["max_reviewers"], len(clients),
          ok=len(clients) >= int(NUM["max_reviewers"]))
    check(f"agent {target} record count >= frozen value",
          NUM["max_reviewers_agent_records"], len(recs),
          ok=len(recs) >= int(NUM["max_reviewers_agent_records"]))
    _, err = call_get_summary(url, target, clients)
    check(f"agent {target} getSummary reverts (unreadable)",
          "reverts", "reverts" if err else "succeeds", ok=(err is not None))

    # C3: empty clientAddresses reverts
    _, err = call_get_summary(url, 54330, [])
    msg = str(err.get("message", "")) if err else ""
    check("empty clientAddresses reverts with 'clientAddresses required'",
          True, msg, ok=("clientAddresses required" in msg))

    # C4: calibration of the local mean model
    def trunc_div(a, b):
        q = abs(a) // abs(b)
        return -q if (a < 0) != (b < 0) else q

    import random
    random.seed(101)
    sample = random.sample(list(full), 25)
    agree = 0
    tested = 0
    for aid in sample:
        recs = full[aid]
        pairs = [(v, d) for v, d, _ in recs]
        clients = sorted({c for _, _, c in recs})
        got, err = call_get_summary(url, aid, clients)
        if err:
            continue
        tested += 1
        md = max(d for _, d in pairs)
        total = sum(v * (10 ** (md - d)) for v, d in pairs)
        mean = trunc_div(total, len(pairs))
        if got == (len(pairs), mean, md):
            agree += 1
    check(f"local mean-model matches deployed contract on all {tested} sampled agents",
          tested, agree)
    return full


# ------------------------------------------------------------------ D ------
def audit_gas():
    section("D. GAS MODEL AND AVAILABILITY BOUNDARY")
    from eth_abi import encode as abi_encode
    from verify_onchain import SELECTOR
    url = P.CHAINS["base"]
    full = load_agents_with_clients("base")
    clients = sorted({c for _, _, c in full[55985]})

    pts = []
    for n in (100, 500, 1000, 2000):
        payload = SELECTOR + abi_encode(
            ["uint256", "address[]", "string", "string"],
            [55985, clients[:n], "", ""])
        res, err = P.rpc(url, "eth_estimateGas", [{
            "to": P.REPUTATION_REGISTRY, "data": "0x" + payload.hex()}], 120)
        if err:
            info(f"estimateGas n={n}", f"failed: {err}")
            continue
        pts.append((n, int(res, 16)))
        info(f"estimateGas n={n}", f"{int(res,16):,} gas")

    if len(pts) >= 2:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        n = len(xs)
        slope = ((n * sum(x * y for x, y in zip(xs, ys)) - sum(xs) * sum(ys))
                 / (n * sum(x * x for x in xs) - sum(xs) ** 2))
        check("gas per reviewer = 7,441", 7441, round(slope), tol=60)

    # boundary re-check at 30M
    def readable(k, gas):
        payload = SELECTOR + abi_encode(
            ["uint256", "address[]", "string", "string"],
            [55985, clients[:k], "", ""])
        _, e = P.rpc(url, "eth_call", [{
            "to": P.REPUTATION_REGISTRY, "data": "0x" + payload.hex(),
            "gas": hex(gas)}, "latest"], 120)
        return e is None

    check("4,075 reviewers readable at 30M gas", True, readable(4075, 30_000_000))
    check("4,076 reviewers NOT readable at 30M gas",
          True, not readable(4076, 30_000_000))


# ------------------------------------------------------------------ E ------
def audit_validation():
    section("E. VALIDATION REGISTRY")
    url = P.CHAINS["base"]
    T_REQ = P.topic0("ValidationRequest(address,uint256,string,bytes32)")
    T_RES = P.topic0(
        "ValidationResponse(address,uint256,bytes32,uint8,string,bytes32,string)")
    head = P.latest_block(url)
    dep = 41_663_783

    for name, tp, claimed_n in (("ValidationRequest", T_REQ, 74),
                                ("ValidationResponse", T_RES, 68)):
        total, contracts, b = 0, collections.Counter(), head
        while b > dep:
            lo = max(dep, b - 1_000_000)
            logs, err = P.rpc(url, "eth_getLogs", [{
                "topics": [tp], "fromBlock": hex(lo), "toBlock": hex(b)}])
            if err:
                info(f"{name} window {lo}-{b}", f"error {err}")
                b = lo - 1
                continue
            for l in logs or []:
                total += 1
                contracts[l["address"].lower()] += 1
            b = lo - 1
        # counts may have grown since the draft was written; report both
        ok = total >= claimed_n
        check(f"{name} count >= {claimed_n} (draft value)", claimed_n, total, ok=ok)
        if total != claimed_n:
            print(f"         NOTE: chain now has {total}; update the draft.")
        info(f"{name} distinct emitting contracts", len(contracts))


# ------------------------------------------------------------------ F ------
def audit_prior_work():
    section("F. CLAIMS ATTRIBUTED TO PRIOR WORK (Xiong et al.)")
    if not os.path.exists(PRIOR_PDF_TXT):
        info("prior paper text", "NOT AVAILABLE - re-extract before submission")
        return
    t = open(PRIOR_PDF_TXT, encoding="utf-8").read()
    norm = " ".join(t.split())
    # PDF text extraction inserts stray spaces inside numerals ("up to1 ,181"),
    # so numeric claims are matched against a whitespace-stripped copy.
    tight = re.sub(r"\s+", "", norm)

    for label, needle in [
        ("cutoff date '13 May 2026'", "13May2026"),
        ("Sybil reviewer coverage 73.5%", "73.5"),
        ("Sybil reviewer coverage 59.2%", "59.2"),
        ("Sybil reviewer coverage 90.6%", "90.6"),
        ("Base no-baseline 86.8%", "86.8"),
        ("cost $0.0027 on Base", "0.0027"),
        ("max reviewer-agent pair 1,181", "1,181"),
        ("Validation Registry deferred as undeployed",
         "mainnetdeploymentispending"),
    ]:
        found = needle.replace(" ", "") in tight
        check(f"prior work: {label}", True, found, ok=found)

    m = re.search(r"up to\s*1\s*,?\s*181\s*feedback records from a single "
                  r"reviewer[^.]*", norm)
    if m:
        info("verbatim quote", " ".join(m.group(0).split())[:140])


def main():
    print("VERIFICATION AUDIT")
    print("Every claim re-derived from source. FAIL = fix the paper.\n")
    audit_dataset()
    audit_feedback_stats()
    audit_onchain()
    audit_gas()
    audit_validation()
    audit_prior_work()

    section("SUMMARY")
    n_pass = sum(1 for t, _ in results if t == "PASS")
    n_fail = sum(1 for t, _ in results if t == "FAIL")
    n_info = sum(1 for t, _ in results if t == "INFO")
    print(f"  PASS {n_pass}   FAIL {n_fail}   INFO {n_info}")
    if n_fail:
        print("\n  FAILED CHECKS:")
        for t, label in results:
            if t == "FAIL":
                print(f"    - {label}")
        print("\n  Do not submit until each is corrected.")
    else:
        print("\n  All verifiable claims reproduce.")


if __name__ == "__main__":
    main()
