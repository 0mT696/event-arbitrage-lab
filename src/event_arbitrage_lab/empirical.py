"""N7C: empirical statistics over evidence ledgers.

Everything is computed FROM THE LEDGER ALONE (Evidence Provenance): the
report an auditor rebuilds from records must equal the report the
runner produced. Metrics answer the N7 research questions:

- opportunity frequency            (OPPORTUNITY / TICK_SCANNED)
- gate pass rates                  (RISK_DECISION by phase, approved)
- block taxonomy                   (rejection reasons, both phases)
- edge decay scan -> execution     (exec_net - scan net per opportunity)
- executable capacity              (Opportunity.shares, N3 formal capacity)
- execution outcome taxonomy       (clean / aborted / residual / unwind loss)
- paper PnL                        (sum net_pnl; residual basis separate)
- feed reliability (N7-BLOCKER-002): availability, failure counts, gap
  durations, per-market reliability — MARKET_FRAME gives the complete
  denominator (every tracked market in every sweep leaves exactly one
  frame), FEED_RESTORED carries gap durations, MARKET_RETIRED removes
  permanently-gone markets from the denominator.

Determinism: pure function of ledger records.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from .ledger import EvidenceLedger, LedgerRecord


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def _pct(num: int | float, den: int | float) -> float | None:
    return round(100.0 * num / den, 2) if den else None


def summarize(records: Iterable[LedgerRecord]) -> dict[str, Any]:
    scans = opps = pre_ok = pre_no = exe_ok = exe_no = 0
    execs = skipped = 0
    reject_reasons: dict[str, int] = {}
    block_reasons: dict[str, int] = {}  # gate rejections by phase:reason
    breaker_events: list[dict[str, Any]] = []
    restores = 0
    edge_pairs: list[tuple[Decimal, Decimal]] = []  # (scan_net, exec_net)
    capacities: list[Decimal] = []
    latencies: list[float] = []
    pnl_total = Decimal(0)
    residual_basis = Decimal(0)
    outcome_taxonomy = {"clean": 0, "aborted": 0, "residual_held": 0,
                        "unwind_loss": 0}
    per_market: dict[str, dict[str, Any]] = {}
    feed = {"frames": 0, "ok": 0, "failed": 0}
    gap_durations: list[float] = []
    failure_reasons: dict[str, int] = {}
    retired_markets: list[str] = []

    def pm(m: str) -> dict[str, Any]:
        return per_market.setdefault(
            m, {"frames": 0, "frames_failed": 0, "scans": 0, "opps": 0,
                "execs": 0, "gap_count": 0, "downtime_s": 0.0,
                "retired": False}
        )

    pending_opp_net: Decimal | None = None  # stream-order pairing
    for r in records:
        p = json.loads(r.payload_json)
        if r.kind == "MARKET_FRAME":
            feed["frames"] += 1
            m = p["market"]
            pm(m)["frames"] += 1
            if p["ok"]:
                feed["ok"] += 1
            else:
                feed["failed"] += 1
                pm(m)["frames_failed"] += 1
                err = str(p.get("error", "unknown")).split(":")[0]
                failure_reasons[err] = failure_reasons.get(err, 0) + 1
        elif r.kind == "FEED_RESTORED":
            dur = float(p["duration_s"])
            gap_durations.append(dur)
            m = p["market"]
            pm(m)["gap_count"] += 1
            pm(m)["downtime_s"] += dur
        elif r.kind == "MARKET_RETIRED":
            retired_markets.append(p["market"])
            pm(p["market"])["retired"] = True
        elif r.kind in ("TICK_SCANNED", "SCAN_SNAPSHOT"):
            # TICK_SCANNED: paper runner; SCAN_SNAPSHOT: N5 replay
            scans += 1
            if "latency_interval_s" in p:
                latencies.append(float(p["latency_interval_s"]))
            m = p.get("market")
            if m:
                pm(m)["scans"] += 1
        elif r.kind == "OPPORTUNITY":
            opps += 1
            opp = p["opportunity"]
            pending_opp_net = _dec(opp["net_profit"])
            capacities.append(_dec(opp["shares"]))  # capacity == shares (N3)
            m = opp["legs"][0]["market_id"]
            pm(m)["opps"] += 1
        elif r.kind == "REJECTION":
            reject_reasons[p["reason"]] = reject_reasons.get(p["reason"], 0) + 1
        elif r.kind == "RISK_DECISION":
            phase = p.get("phase", "PRE_TRADE")
            if p["approved"]:
                if phase == "PRE_TRADE":
                    pre_ok += 1
                else:
                    exe_ok += 1
                    if pending_opp_net is not None and p.get("exec_net") is not None:
                        edge_pairs.append((pending_opp_net, _dec(p["exec_net"])))
                    pending_opp_net = None
            else:
                if phase == "PRE_TRADE":
                    pre_no += 1
                    pending_opp_net = None  # blocked: no exec decision follows
                else:
                    exe_no += 1
                    if pending_opp_net is not None and p.get("exec_net") is not None:
                        edge_pairs.append((pending_opp_net, _dec(p["exec_net"])))
                    pending_opp_net = None
                for rej in p.get("rejections", []):
                    key = f"{phase}:{rej['reason']}"
                    block_reasons[key] = block_reasons.get(key, 0) + 1
        elif r.kind == "EXECUTION_SKIPPED":
            skipped += 1
            pending_opp_net = None
        elif r.kind == "EXECUTION":
            execs += 1
            res = p["result"]
            net = _dec(res["net_pnl"])
            pnl_total += net
            residual_basis += _dec(res.get("residual_cost_basis", "0"))
            state = res["final_state"]
            if state == "ABORTED":
                outcome_taxonomy["aborted"] += 1
            elif _dec(res["residual_shares"]) > 0:
                outcome_taxonomy["residual_held"] += 1
            elif _dec(res["unwind_realized_pnl"]) < 0:
                outcome_taxonomy["unwind_loss"] += 1
            else:
                outcome_taxonomy["clean"] += 1
            m = res["leg_a"]["fills"][0]["market_id"] if res["leg_a"]["fills"] \
                else None
            if m:
                pm(m)["execs"] += 1
        elif r.kind == "CIRCUIT_BREAKER":
            breaker_events.append(p)
        elif r.kind == "RISK_STATE_RESTORED":
            restores += 1

    decays = [float(scan - exe) for scan, exe in edge_pairs]
    vanished = sum(1 for scan, exe in edge_pairs if scan > 0 and exe <= 0)

    return {
        "ticks_scanned": scans,
        "opportunities": opps,
        "opportunity_rate_pct": _pct(opps, scans),
        "gates": {
            "pre_trade": {"approved": pre_ok, "rejected": pre_no,
                          "pass_rate_pct": _pct(pre_ok, pre_ok + pre_no)},
            "execution_time": {"approved": exe_ok, "rejected": exe_no,
                               "pass_rate_pct": _pct(exe_ok, exe_ok + exe_no)},
        },
        "block_reasons": dict(sorted(block_reasons.items())),
        "scan_rejections": dict(sorted(reject_reasons.items())),
        "executions": {
            "total": execs, "skipped": skipped, **outcome_taxonomy,
            "fill_rate_pct": _pct(outcome_taxonomy["clean"], execs),
        },
        "edge_decay": {
            "pairs": len(edge_pairs),
            "mean_decay": round(mean(decays), 6) if decays else None,
            "median_decay": round(median(decays), 6) if decays else None,
            "max_decay": round(max(decays), 6) if decays else None,
            "vanished_at_execution_pct": _pct(vanished, len(edge_pairs)),
        },
        "latency": {
            "samples": len(latencies),
            "mean_s": round(mean(latencies), 3) if latencies else None,
            "max_s": round(max(latencies), 3) if latencies else None,
        },
        "capacity": {
            "samples": len(capacities),
            "mean": round(float(mean(capacities)), 4) if capacities else None,
            "median": round(float(median(capacities)), 4) if capacities else None,
            "max": str(max(capacities)) if capacities else None,
        },
        "pnl": {
            "net_total": str(pnl_total),
            "residual_cost_basis_open": str(residual_basis),
        },
        "feed": {
            "frames": feed["frames"],
            "ok": feed["ok"],
            "failed": feed["failed"],
            "availability_pct": _pct(feed["ok"], feed["frames"]),
            "failure_reasons": dict(sorted(failure_reasons.items())),
            "gaps": len(gap_durations),
            "gap_duration_s": {
                "mean": round(mean(gap_durations), 1) if gap_durations else None,
                "max": round(max(gap_durations), 1) if gap_durations else None,
                "total": round(sum(gap_durations), 1) if gap_durations else 0.0,
            },
            "retired": len(retired_markets),
        },
        "circuit_breaker_events": breaker_events,
        "risk_state_restores": restores,
        "per_market": dict(sorted(per_market.items())),
    }


def render_markdown(summary: dict[str, Any], *, title: str) -> str:
    g = summary["gates"]
    e = summary["edge_decay"]
    c = summary["capacity"]
    x = summary["executions"]
    f = summary["feed"]
    lines = [
        f"# {title}",
        "",
        f"- ticks scanned: **{summary['ticks_scanned']}** "
        f"(mean latency {summary['latency']['mean_s']}s)",
        f"- opportunities: **{summary['opportunities']}** "
        f"({summary['opportunity_rate_pct']}% of ticks)",
        f"- PRE_TRADE pass: {g['pre_trade']['approved']}/"
        f"{g['pre_trade']['approved'] + g['pre_trade']['rejected']} "
        f"({g['pre_trade']['pass_rate_pct']}%)",
        f"- EXECUTION_TIME pass: {g['execution_time']['approved']}/"
        f"{g['execution_time']['approved'] + g['execution_time']['rejected']} "
        f"({g['execution_time']['pass_rate_pct']}%)",
        f"- executions: {x['total']} (clean {x['clean']}, aborted "
        f"{x['aborted']}, residual {x['residual_held']}, unwind-loss "
        f"{x['unwind_loss']}; fill rate {x['fill_rate_pct']}%)",
        f"- edge decay: {e['pairs']} pairs, mean {e['mean_decay']}, "
        f"median {e['median_decay']}, max {e['max_decay']}, "
        f"vanished at exec {e['vanished_at_execution_pct']}%",
        f"- capacity: mean {c['mean']} / median {c['median']} / max {c['max']}",
        f"- paper net PnL: **{summary['pnl']['net_total']}** "
        f"(open residual basis {summary['pnl']['residual_cost_basis_open']})",
        f"- feed availability: **{f['availability_pct']}%** "
        f"({f['ok']}/{f['frames']} frames ok; {f['gaps']} gaps, "
        f"total downtime {f['gap_duration_s']['total']}s; "
        f"{f['retired']} markets retired)",
        f"- risk state restores (restarts): {summary['risk_state_restores']}",
        "",
        "## block reasons",
        "",
    ]
    for k, v in summary["block_reasons"].items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## scan rejections", ""]
    for k, v in summary["scan_rejections"].items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## feed failure reasons", ""]
    for k, v in summary["feed"]["failure_reasons"].items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## per market", "",
              "| market | frames | failed | availability | scans | opps | "
              "execs | gaps | downtime s | retired |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for m, d in summary["per_market"].items():
        avail = _pct(d["frames"] - d["frames_failed"], d["frames"])
        lines.append(
            f"| {m} | {d['frames']} | {d['frames_failed']} | {avail}% "
            f"| {d['scans']} | {d['opps']} | {d['execs']} | {d['gap_count']} "
            f"| {round(d['downtime_s'], 1)} | {'yes' if d['retired'] else ''} |"
        )
    lines += ["", "## circuit breaker", ""]
    for ev in summary["circuit_breaker_events"]:
        lines.append(f"- `{json.dumps(ev, sort_keys=True)}`")
    return "\n".join(lines) + "\n"


def main(argv: Iterable[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="N7C empirical report from ledger")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--out", default=None, help="markdown output path")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(list(argv) if argv is not None else None)
    ledger = EvidenceLedger.load(args.ledger)
    summary = summarize(ledger.records)
    day = Path(args.ledger).stem.replace("ledger-", "")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2, sort_keys=True))
    md = render_markdown(summary, title=f"N7 empirical report — {day}")
    if args.out:
        Path(args.out).write_text(md)
    else:
        print(md)


if __name__ == "__main__":
    main()
