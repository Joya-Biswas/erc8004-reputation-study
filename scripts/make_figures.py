"""
Figures for the ERC-8004 paper
==============================
Produces publication-ready PDF + PNG figures from the collected data.
Greyscale-safe (Springer proceedings are often printed in mono): series are
distinguished by marker and linestyle as well as colour.

RUN:  python make_figures.py [chain]
"""

import collections
import csv
import datetime as dt
import os
import statistics
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

CHAIN = sys.argv[1] if len(sys.argv) > 1 else "base"
OUT = "figures"
os.makedirs(OUT, exist_ok=True)

plt.rcParams.update({
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "legend.fontsize": 8,
    "figure.dpi": 200,
    "savefig.bbox": "tight",
    "axes.grid": True,
    "grid.alpha": 0.3,
    "grid.linewidth": 0.5,
})


def save(fig, name):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(OUT, f"{name}.{ext}"))
    plt.close(fig)
    print(f"  wrote {OUT}/{name}.pdf / .png")


def load_feedback(chain):
    rows, seen = [], set()
    path = f"feedback_{chain}.csv"
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r["txHash"], r["logIndex"])
            if k in seen:
                continue
            seen.add(k)
            try:
                r["value"] = int(r["value"])
                r["valueDecimals"] = int(r["valueDecimals"])
                r["agentId"] = int(r["agentId"])
                r["timestamp"] = int(r["timestamp"]) if r["timestamp"] else None
            except (ValueError, TypeError):
                continue
            rows.append(r)
    return rows


def load_regs(chain):
    rows, seen = [], set()
    path = f"registered_{chain}.csv"
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r["txHash"], r["logIndex"])
            if k in seen:
                continue
            seen.add(k)
            try:
                r["timestamp"] = int(r["timestamp"]) if r["timestamp"] else None
            except (ValueError, TypeError):
                continue
            rows.append(r)
    return rows


# ---------------------------------------------------------------- FIG 1 ----
def fig_gas():
    """The headline: aggregation cost grows linearly with reviewer count, so
    readability is purchasable. Layout keeps every label in clear space."""
    measured = [(1, 44_486), (10, 115_093), (100, 779_213),
                (500, 3_743_680), (1000, 7_524_169), (2000, 14_909_257)]
    xs = np.array([m[0] for m in measured])
    ys = np.array([m[1] for m in measured])
    slope, intercept = np.polyfit(xs[2:], ys[2:], 1)

    fig, ax = plt.subplots(figsize=(5.4, 3.4), layout="constrained")
    grid = np.linspace(0, 4600, 200)
    ax.plot(grid, (slope * grid + intercept) / 1e6, "-", lw=1.3, color="0.4",
            label=f"linear fit: {slope:,.0f} gas per reviewer", zorder=2)
    ax.plot(xs, ys / 1e6, "o", ms=5.5, color="0.1", zorder=3,
            label="measured (eth_estimateGas)")

    # Budget lines: labels sit at the LEFT, just above each line, where the
    # fitted curve has not yet risen - no collision with data or legend.
    for budget, lab in [(1e6, "1M gas: on-chain consumer"),
                        (10e6, "10M gas"),
                        (30e6, "30M gas: generous off-chain read")]:
        ax.axhline(budget / 1e6, ls="--", lw=0.9, color="0.6", zorder=1)
        ax.text(60, budget / 1e6 + 0.7, lab, ha="left", va="bottom",
                fontsize=7.2, color="0.3", zorder=4)

    # Failure boundary: short tick at the bottom, label below the axis area.
    ax.axvline(4075, ls=":", lw=1.2, color="0.15", zorder=1)
    ax.text(4020, 1.2, "measured failure\nboundary at 30M\n(4,075 reviewers)",
            ha="right", va="bottom", fontsize=7.2, color="0.15", zorder=4)

    ax.set_xlabel("reviewers included in getSummary()")
    ax.set_ylabel("gas consumed (millions)")
    ax.set_title("Aggregation cost is linear in reviewer count")
    ax.set_xlim(0, 4600)
    ax.set_ylim(0, 36)
    # Legend below the axes: the plot interior is fully occupied by the fitted
    # line, three budget rules and the boundary marker, so an inset legend
    # cannot avoid colliding with one of them.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.20), ncol=2,
              frameon=False, fontsize=7.6)
    save(fig, "fig1_gas_vs_reviewers")


# ---------------------------------------------------------------- FIG 2 ----
def fig_scores(fbs):
    """Score distribution: where the mass actually sits."""
    scaled = [r["value"] / (10 ** r["valueDecimals"]) for r in fbs]
    inrange = [s for s in scaled if 0 <= s <= 100]
    out_hi = sum(1 for s in scaled if s > 100)
    out_lo = sum(1 for s in scaled if s < 0)

    fig, ax = plt.subplots(figsize=(5.0, 3.1))
    ax.hist(inrange, bins=np.arange(0, 105, 5), color="0.45",
            edgecolor="0.15", linewidth=0.6)
    ax.set_xlabel("reported value (decimals applied)")
    ax.set_ylabel("feedback records")
    ax.set_title(f"Value distribution, {CHAIN} (n={len(fbs):,})")
    ax.set_xlim(0, 100)
    txt = (f"outside [0,100]:\n  >100: {out_hi:,}\n  <0: {out_lo:,}\n"
           f"  max: {max(scaled):.3g}")
    ax.text(0.97, 0.95, txt, transform=ax.transAxes, ha="right", va="top",
            fontsize=7.5, bbox=dict(boxstyle="round,pad=0.4", fc="white",
                                    ec="0.6", lw=0.6))
    save(fig, "fig2_value_distribution")


# ---------------------------------------------------------------- FIG 3 ----
def fig_rules():
    """Mitigation comparison.

    Values re-derived by evaluate_fixes.py on the frozen snapshot. Log scale is
    unavoidable (R0/R1 are ~1e35), so each bar is labelled with its value -
    on a log axis spanning 36 decades the small bars are otherwise
    indistinguishable by eye.
    """
    short = ["R0", "R1", "R2", "R3", "R4", "R5"]
    key = ("R0 deployed getSummary   R1 +per-tag only   R2 +range clamp\n"
           "R3 +median   R4 +one record per reviewer   R5 +10% trimmed mean")
    base_shift = [4.05e35, 7.88e35, 0.0882, 0.0051, 0.0882, 0.0882]
    shift_lab = ["4.1e35", "7.9e35", "8.8%", "0.51%", "8.8%", "8.8%"]
    moved50 = [100.00, 100.00, 20.23, 15.06, 25.14, 28.17]
    # dark = vulnerable, light = mitigated
    cols = ["0.25", "0.25", "0.62", "0.80", "0.62", "0.62"]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.0, 3.2), layout="constrained")

    a1.bar(range(6), base_shift, color=cols, edgecolor="0.1", linewidth=0.6)
    a1.set_yscale("log")
    a1.set_ylim(1e-3, 1e40)
    a1.set_xticks(range(6))
    a1.set_xticklabels(short)
    a1.set_ylabel("median score shift (multiples)")
    a1.set_title("Impact of one injected record", pad=8)
    a1.axhline(1.0, ls="--", lw=0.8, color="0.35")
    a1.text(-0.45, 1.7, "100% shift", ha="left", fontsize=7, color="0.35")
    for i, (v, lab) in enumerate(zip(base_shift, shift_lab)):
        a1.text(i, v * 3.2, lab, ha="center", fontsize=7, color="0.1")

    a2.bar(range(6), moved50, color=cols, edgecolor="0.1", linewidth=0.6)
    a2.set_xticks(range(6))
    a2.set_xticklabels(short)
    a2.set_ylabel("agents moved $\\geq$50% (%)")
    a2.set_title("Agents materially affected", pad=8)
    a2.set_ylim(0, 118)
    for i, v in enumerate(moved50):
        a2.text(i, v + 3, f"{v:g}", ha="center", fontsize=7, color="0.1")

    fig.suptitle("Range enforcement is load-bearing; per-tag alone backfires",
                 fontsize=9.5)
    fig.text(0.5, -0.06, key, ha="center", va="top", fontsize=7, color="0.3")
    save(fig, "fig3_mitigations")


# ---------------------------------------------------------------- FIG 4 ----
def fig_growth(regs, fbs):
    """Ecosystem growth, with the prior study's cutoff marked."""
    def series(rows):
        ts = sorted(r["timestamp"] for r in rows if r.get("timestamp"))
        if not ts:
            return [], []
        days = [dt.datetime.fromtimestamp(t, dt.UTC).date() for t in ts]
        c = collections.Counter(days)
        xs = sorted(c)
        cum, running = [], 0
        for d in xs:
            running += c[d]
            cum.append(running)
        return xs, cum

    rx, rc = series(regs)
    fx, fc = series(fbs)

    fig, ax = plt.subplots(figsize=(5.4, 3.1))
    if rx:
        ax.plot(rx, rc, "-", lw=1.4, color="0.2", label="registrations")
    if fx:
        ax.plot(fx, fc, "--", lw=1.4, color="0.5", label="feedback records")

    cutoff = dt.date(2026, 5, 13)
    ax.axvline(cutoff, ls=":", lw=1.2, color="0.1")
    ax.annotate("prior study cutoff\n13 May 2026", xy=(cutoff, max(fc or [1]) * 0.55),
                xytext=(-95, 0), textcoords="offset points", fontsize=7.5,
                arrowprops=dict(arrowstyle="->", lw=0.8, color="0.2"))

    ax.set_ylabel("cumulative events")
    ax.set_title(f"Ecosystem growth, {CHAIN}")
    ax.legend(loc="upper left", frameon=False)
    fig.autofmt_xdate(rotation=30)
    save(fig, "fig4_growth")


def main():
    print(f"chain={CHAIN}")
    fbs = load_feedback(CHAIN)
    regs = load_regs(CHAIN)
    print(f"  feedback={len(fbs):,}  registrations={len(regs):,}")

    fig_gas()
    fig_rules()
    if fbs:
        fig_scores(fbs)
    if fbs or regs:
        fig_growth(regs, fbs)
    print("done")


if __name__ == "__main__":
    main()
