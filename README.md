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

## Status

Early scaffold — architecture and roadmap initialized.

See [ROADMAP.md](ROADMAP.md) for milestones.
