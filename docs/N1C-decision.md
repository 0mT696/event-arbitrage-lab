# N1C — First Venue Selection 决策记录

> 决策人：Alex · 日期：2026-10-06 · 依据：N1B Venue Capability Matrix 第一轮

## 决策

**首个接入 venue：Polymarket。第一阶段做单 venue 内部套利
（YES+NO / negRisk 多 outcome completeness），跨 venue 套利暂缓。**

## 依据（来自 N1B 矩阵）

- 读侧全免认证：Gamma / CLOB / Data API 公开，WebSocket 行情频道公开，
  Scanner 原型不需要任何凭证或账户。
- 限流按 IP 计算且慷慨（`/book` 1500 次/10s），超限仅节流不拒绝。
- negRisk 多结果市场 = completeness 套利的天然场景；地缘/世界事件类零费。
- 费用模型为分品类 taker 二次型费率，core `QuadraticTakerFee` 加品类映射即可覆盖。

## 约束（随决策生效）

1. Adapter 只读：发现、订单簿、历史数据。**禁止出现下单、签名、私钥路径。**
2. 费用按品类绑定；品类未知 → 取最高费率（fail-closed，宁可低估 edge）。
3. negRisk 多 outcome 市场的 `exhaustive` 语义未经人工确认前一律 False，
   Scanner 不得对其输出机会。
4. 跨 venue（Polymarket ↔ Kalshi）在 semantic equivalence 判定流程建立前不启动。
5. Kalshi / Limitless / SX Bet 保留在矩阵中，第二轮研究视第一阶段结果决定。

## 后续

- N1E / N2 / N3 / N4 验收由独立 Verifier 执行（验收包见 docs/验收checklist-v1.md）。
- N5 Replay & Evidence Ledger 仍为 venue-agnostic，可与 Adapter 并行推进。

**Version: N1C Decision v1**
