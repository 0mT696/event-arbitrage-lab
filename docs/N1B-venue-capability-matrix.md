# N1B — Venue Capability Matrix（第一轮研究）

> 研究日期：2026-10-06。机器可读版本：`research/venue_capabilities.json`
> （schema 由 `adapter.VenueCapability` 校验，未知一律 `null`，未知不等于 yes）。
> 本文不做出 N1C 选择——只提供决策输入；选择权在 Alex。

## 1. 矩阵总览

| Venue | REST | WS | 订单簿 | 历史数据 | 费用形态 | 真实结算 | 备注 |
|---|---|---|---|---|---|---|---|
| Polymarket | ✅ | ✅ | ✅ | ✅ | 分品类 taker 费 `fee=C·rate·p(1−p)`（**官方已核验 2026-10-06**），maker 0+返佣；地缘/世界事件类 **零费** | ✅ UMA / USDC | 读取全免认证，限流慷慨；Gamma 逐市场下发 feeSchedule |
| Kalshi | ✅ | ✅ | ✅ | ✅ | taker `roundup(M·0.07·C·P·(1−P))`；2026-08-19 起部分系列收 maker 费 | ✅ CFTC DCM | 公式与 core `QuadraticTakerFee` 吻合，但多了系列乘数 M 和 maker 腿 |
| Limitless | ❓ | ❓ | ✅ | ❓ | AMM 0.25% 平费；订单簿 0.03%–3% 逆概率 | ❓ | 多处研究缺口，需第二轮 |
| Manifold | ✅ | ❓ | ❌(AMM) | ✅ | 游戏币 Mana | ❌ | **N1B 排除**：无真实价值结算 |
| SX Bet | ✅ | ✅ | ✅ | ❓ | 宣传直投 0%、串关盈利 5%；**按结算利润计费**且可账户定制 | ✅ USDC 托管合约 | 纯体育；费用形状不同（需新 FeeModel） |

## 2. 关键发现（影响 N1C / Adapter 设计）

1. **Polymarket 不再零费（2026-10-06 已经官方文档核验）。** 公式
   `fee = C × feeRate × p × (1−p)`，taker-only，maker 免费且有返佣；费用舍入到
   5 位小数（最小 0.00001 USDC）。官方品类费率：crypto 0.07、sports 0.05、
   finance/politics/mentions/tech 0.04、economics/culture/weather/general 0.05、
   geopolitics/world **0（官方明确零费）**。Gamma API 逐市场下发
   `feeSchedule{rate, exponent, takerOnly, rebateRate}` + `feesEnabled`，
   adapter 优先按市场读取、品类表兜底、未知 fail-closed 取 MAX(0.07)。
   来源：docs.polymarket.com/trading/fees.md 与 polymarket.com/fees（2026-10-06 抓取）。
   修正初稿两处偏差：sports 0.03→**0.05**（v3 已覆盖 13159 个市场；0.03 仅存于
   542 个 v2 遗留市场及 NFL/NCAAF 促销至 2026-10 底）、crypto 0.072→**0.07**。
   "Polymarket 零费"是过期假设。
2. **Kalshi 费用公式与 core 模型吻合但有扩展。** 标准 taker `roundup(M·0.07·C·P·(1−P))`
   （M 默认 1，S&P/Nasdaq 0.035，封顶 $1.75/100 @ 50¢）；2026-08-19 起
   `quadratic_with_maker_fees` 系列收 maker 费（0.0175 同型）；无结算费。
   → core 需支持：per-market 乘数 M + maker/taker 双费率。`QuadraticTakerFee`
   加 `multiplier` 参数即可覆盖，属 Adapter 绑定范畴。
3. **SX Bet 的费用是另一种形状**：`charge_basis = settlement_profit`（按盈利结算时抽成），
   不是进场名义本金计费。若未来接入，core 需要新的 FeeModel 形状。
4. **Kalshi 结算源是"市场规则指定源 + 交易所裁定"，Polymarket 是 UMA 乐观预言机**——
   跨 venue 套利的 semantic equivalence 难点（官方 roadmap 已预判）就在这里：
   同一事件两边结算口径可能不同（例如"官方宣布" vs "美联社口径"），N1C 若选双 venue，
   该维度必须在 N4 之前建立判定流程。
5. **Polymarket 读侧对 Scanner 最友好**：Gamma/CLOB/Data API 全部免认证，
   WS 行情频道公开，`/book` 1500 次/10s；Kalshi 具体限流数字本轮未捕获（null）。

## 3. N1C 决策输入（非决定）

- **单 venue 内部套利先行**（官方判断：开发明显更简单）：首选 **Polymarket**
  （免认证读侧 + negRisk 多结果市场 = completeness 套利天然场景 + 零费品类存在）。
- **跨 venue 套利**：Polymarket ↔ Kalshi 是流动性与事件重叠度最高的组合，
  但以 semantic equivalence 判定流程为前提。
- **Manifold 已排除**；Limitless 待第二轮补研究缺口；SX Bet 仅当体育方向立项。

## 4. 来源

- Polymarket 限流与 API：resolvedmarkets.com Polymarket API Guide（2026-09-27）、
  blog.predictefy.com（2026-09-24/26）、tradoxvps.com（2026-07-11）、parlay.run（2026-05-16）
- Polymarket 费用：predictionhero.com（2026-07-10）、bettorsinsider.com（2026-04-30）
- Kalshi 费用：coinstats.app（2026-08-28，引 Kalshi 2026-07-07 费率表）、
  kalshiview.com（2026-07-17）、polymarket-kalshi.com（2026-09-12）、
  tech-insider.org（2026-09-29）、predictionsmarketfans.com（2026-09-30）
- Kalshi 结算与监管：tech-insider.org（2026-09-29）、predictionhunt.com（2025-08-13）
- Limitless / Opinion / Predict.Fun：predictionhero.com（2026-07-10）
- Manifold：predictionsmarketfans.com（2026-08-22）、risingmax.com（2026-05-08）
- SX Bet：alphascope.app 对比页、docs.predictorsdk.com changelog（2026-09-19）、
  SX Bet 官方 API 文档（2026-08-14）

**Version: N1B Research Round 1 · 缺口项已按 fail-closed 标 null**
