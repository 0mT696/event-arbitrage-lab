# Roadmap — event-arbitrage-lab

Status: **Initial scaffold**  
Owner: Alex  
Repository: standalone project; no dependency on GPTHEIST.

## Product boundary

Build a venue-agnostic research and paper-trading system for complementary-outcome event-market arbitrage. The system identifies opportunities only when both outcomes are mutually exclusive/exhaustive and the executable combined cost remains below settlement value after all costs.

Live trading, private-key management, leverage, and autonomous capital movement are explicitly out of scope for the initial phases.

## Development nodes

| Node | Focus | Exit evidence |
|---|---|---|
| N0 | Repository and contract baseline | README, roadmap, package layout, contribution rules |
| N1 | Market model and rule validation | normalized schemas plus tests for valid/invalid outcome sets |
| N2 | Order-book and VWAP engine | depth-aware executable cost calculations with fixtures |
| N3 | Opportunity scanner | net-edge filter including fees, slippage, staleness, and minimum depth |
| N4 | Paper execution | equal-quantity two-leg fills, partial-fill and timeout simulation |
| N5 | Replay and evidence | historical/replayed books, deterministic runs, ledger and reports |
| N6 | Risk review | kill switches, exposure caps, settlement assumptions, failure matrix |
| N7 | Small-scale controlled validation | paper-only soak test and independent review |
| N8 | Optional live-execution design | design document only; requires separate human approval |

## Milestones

### M0 — Scaffold (now)

- [x] Create independent GitHub repository
- [x] Define research-only boundary
- [x] Define architecture and development nodes
- [ ] Add Python package skeleton and test harness
- [ ] Add CI for linting and unit tests

### M1 — Deterministic core

- [ ] Define Market, Outcome, Quote, OrderBook, Opportunity, and Fill schemas
- [ ] Implement market-rule validator
- [ ] Implement depth-aware VWAP calculator
- [ ] Add fee and slippage models
- [ ] Add fixture-based unit tests

### M2 — Scanner and paper executor

- [ ] Implement net-edge scanner
- [ ] Reject stale or insufficient-depth books
- [ ] Simulate simultaneous two-leg execution
- [ ] Simulate partial fills, timeout, cancellation, and unwind
- [ ] Track paired and residual exposure separately

### M3 — Replay and evidence

- [ ] Define replay input format
- [ ] Build deterministic replay runner
- [ ] Produce opportunity, fill, PnL, and failure reports
- [ ] Add benchmark scenarios and adverse execution cases

### M4 — Risk and validation

- [ ] Add capital, notional, and unhedged-exposure limits
- [ ] Add settlement-rule checklist
- [ ] Add kill switch and circuit breakers
- [ ] Run paper-only soak tests
- [ ] Complete independent review before any live design work

## Non-negotiable gates

1. No prediction-based trade is called arbitrage.
2. Mid-price and last-trade price cannot be used as executable cost.
3. An opportunity is invalid if one leg cannot be filled at the required size.
4. Partial fills create exposure and must be handled explicitly.
5. Net edge must include fees, slippage, settlement, and capital-lock assumptions.
6. No live order path is merged into the initial release.

## Suggested initial stack

- Python 3.12+
- pydantic for schemas
- pytest for tests
- polars or pandas for replay analysis
- Adapter interfaces kept separate from venue-specific clients

## Definition of done for v0.1

A deterministic paper-trading run can ingest a normalized two-outcome order book, verify the resolution rule, calculate executable VWAP for both legs, reject unsafe opportunities, simulate fills and partial fills, and emit an auditable result without touching real funds.
