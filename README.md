# event-arbitrage-lab

Independent research and paper-trading framework for complementary-outcome event-market arbitrage.

> This repository is completely independent from GPTHEIST. It is not a branch, module, or sub-project of GPTHEIST.

## Scope

The first release focuses on detecting and validating opportunities where two mutually exclusive outcomes can be bought together below their guaranteed settlement value:

`net_edge = settlement_value - executable_cost_a - executable_cost_b - fees - slippage_buffer`

The initial system is research-only and paper-trading only. It must not place live orders, manage private keys, or move real funds.

## Initial architecture

- **Market adapters** — normalized market metadata and order books
- **Rule validator** — verifies that outcomes are mutually exclusive and exhaustive
- **Opportunity scanner** — computes executable VWAP and net edge
- **Paper executor** — simulates simultaneous/partial fills and hedging
- **Risk gate** — rejects stale books, insufficient depth, high fees, and unhedged exposure
- **Evidence ledger** — records inputs, decisions, fills, and settlement assumptions

## Development rule

No live execution is permitted until the project has passed historical replay, paper trading, partial-fill simulation, fee/slippage validation, and an explicit human approval review.

## Status — PAUSED (2026-11-14)

**Project paused by owner instruction. No further development, collection, paper execution, or live trading is authorized.**

Previous independent review in the project conversation recorded N1–N6 as accepted and N7A–N7C as implemented/reviewed. N7D's seven-day observation was scheduled for 2026-10-07 through 2026-10-14; its actual completion and final empirical results have **not been independently verified**.

**VPS source verified 2026-11-14:** VPS `/root/event-arbitrage-lab` HEAD `52259e14fca1aacec4417132ae6b41e7525a4a40`, clean tracked files (`.venv/` untracked); independent Git Bundle contains matching commit. GitHub SSH authentication from VPS failed (`Permission denied (publickey)`). N7 services `eal-collector` and `eal-paper` were stopped and disabled. VPS `/root/eal-data/n7` still holds approximately 50GB (42GB sweeps, 7.8GB paper ledger), not yet archived or deleted. **GitHub code is NOT synchronized to the VPS commit.**

**Repository synchronization is not yet verified:** the default-branch README/ROADMAP previously described only the initial scaffold. Do not assume this repository contains the latest VPS code or data. Before deleting VPS assets, reconcile code commits, experiment manifests, ledgers, raw sweeps, checkpoints and reports with durable storage; verify hashes and restore/replay capability.

**Operational hold:** stop collectors, paper runners and scheduled jobs. N8/live execution remains out of scope. Preserve the repository and its history. Restart requires explicit owner approval.

See [ROADMAP.md](ROADMAP.md) for milestones.
