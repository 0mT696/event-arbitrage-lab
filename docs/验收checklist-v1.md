# 验收 Checklist v1 — 供独立 Verifier 使用

> 角色边界：Builder（本仓库 commit 作者）不得自行宣布以下节点 PASS。
> Verifier 应是独立 agent 实例（隔离契约 §3，N0.1 Agent Registry 注册），
> N2/N4 建议走强推理模型。验收证据：`bash scripts/check.sh` 全绿 + 本表逐项签字。

## 验收状态登记（Verifier 签字区）

| 节点 | 状态 | 轮次 | 日期 | 证据 |
|---|---|---|---|---|
| N0 | ✅ CLOSED | — | — | 项目隔离契约生效，双向 DENY 运行中 |
| N0.1 | ✅ CLOSED | — | 2026-10-06 | 服务器隔离按实际部署状态确认（45.207.205.129 专用机） |
| N1A / N1B / N1C / N1D / N1E | ✅ PASS | 二轮 | 2026-10-06 | Verifier 解包 zip 复跑：124 passed / 0 failed / GATE PASSED |
| N2 | ✅ PASS | 二轮 | 2026-10-06 | VWAP/fee/slippage/net edge 链路成立 |
| N3 | ✅ PASS | 二轮 | 2026-10-06 | P0 Rule Gate 已修，7 条攻击用例 + 阳性对照通过 |
| N4 | ✅ PASS | 三轮 | 2026-10-06 | unwind 身份不变量 fail-closed；Verifier 攻击复现已钉死 |
| N5 | ✅ PASS | 三轮 | 2026-10-06 | SCAN/EXECUTION 快照分离，ReplayFrame schema v1，往返一致 |
| N6 | ✅ PASS | 二轮 | 2026-10-06 | N6-P0-001 已修：PRE_TRADE+EXECUTION_TIME 双闸门，6 条执行时对抗回归，阶段区分证据；205 passed 双侧 GATE PASSED |
| N7 | 🟢 IN PROGRESS（观察窗口运行中） | — | 2026-10-06 | N7-BLOCKER-001/002 已修（checkpoint 恢复 + feed 证据 + cumulative 自账本重建，232 passed）；另修两个实测缺陷（重启续读偏移、404 闭市退市）。pre-fix 证据已归档隔离；N7D-OBS-001 正式启动 T0=2026-10-07T02:36:23Z（启动 Gate 全绿：envelope v2 完整性硬化 + 篡改回归 + 240 passed + 冻结清单 docs/n7d-freeze-manifest.md，freeze commit 42c4dca）；T+24h=10-08 02:36Z，T+72h=10-10 02:36Z，T+168h=10-14 02:36Z |
| N8 | OUT OF SCOPE（Human Gate） | — | — | 实盘前费率须按官方规则重新核验（Verifier 长期要求） |

**N1–N6 CLOSED（2026-10-06）。** N1B 的 PASS 语义为"当前研究/开发基线可用"，
非对外部 venue 规则永久不变的保证。

**N6 非阻断观察（Verifier，2026-10-06，后续架构加固，不影响 N6 PASS）**：
EXECUTION_TIME 闸门当前镜像 execute_pair() 的定价/fee/limit 逻辑。今后修改
execute_pair() 成交算法时，必须同步检查 Risk Gate simulation 是否发生
interface/semantic drift；长期应让两者共享同一个 execution quotation
primitive，而不是维护两份相似计算逻辑。

**N7 修复与窗口重置记录（Builder，2026-10-06）**：

- N7-BLOCKER-001（风控状态重启持久化）：版本化 checkpoint + 原子完整性哈希文件，
  携带 (sweep_file, sweep_offset, ledger_root)；checkpoint/ledger root 分叉 = torn write
  = RuntimeError fail-closed。回归：TestRestartDurability（trip/residual → restart →
  状态一致 + RISK_STATE_RESTORED 证据；篡改 → ValueError）、TestServiceResume
  （重启只处理积压帧不重复；分叉 → RuntimeError）。实测：systemctl restart eal-paper
  后 RISK_STATE_RESTORED=1、账本 0 重复帧、checkpoint root == ledger root、verify() True。
- N7-BLOCKER-002（feed 失败进入账本溯源 + ticks_total_cumulative）：MARKET_FRAME
  （每市场每 sweep 一帧，ok 或失败）+ FEED_RESTORED（gap 时长）证据；
  cumulative 指标全部由 cumulative_counts(ledger) 重建，restart-safe；
  N7C 从账本统计 availability/失败原因/gap 时长/每市场可靠度。
- 实测发现并已修的两个缺陷：
  1. 重启续读偏移错误（重启后从 0 重读 → 证据重复）→ checkpoint 携带读取位置。
  2. 闭市市场无限累积 absence 失败帧（404 市场被 collector 退市后，runner 每 sweep
     记一帧失败，污染可用性统计）→ MARKET_RETIRED：连续 retire_after=30 sweep
     不可用且缺席后退市出分母；重现则另起新链（保守，不跨缺口链式执行）。
- 证据隔离：paper-pre-fix-20261006/（BLOCKER 修复前）、
  paper-postfix-draft1-offset-bug/（续读缺陷）、
  paper-postfix-draft2-absence-spam/（退市缺陷）、
  paper-postfix-draft3-fieldname/（证据字段命名）均已归档并附 INVALID-NOTE.md；
  原始 sweep 数据全程有效未重置。
- 正式观察窗口：2026-10-06 16:19 UTC 起（df79594 统一语义，回放覆盖 15:10 起
  原始 sweep）；N7D Gate 不早于 7 天（2026-10-13 16:19 UTC）。

## N1A Canonical Domain Model

- [x] `schemas.py` 全部字段无平台语义（grep 无 venue 专有名词）
- [x] 非法构造全部拒绝：越界价、乱序、交叉盘、naive 时间戳、重复 outcome
- [x] float 经 str() 归一化，无二进制浮点污染（test_float_coerced）

## N1D Adapter Contract

- [x] core 包不 import adapters（方向唯一：adapters → core）
- [x] VenueCapability 三态语义：None=未知，未知不得当 yes
- [x] adapter 输出直接可进 N2 数学（test_adapter 端到端用例）

## N2 Order Book & Pricing Engine

- [x] 走簿数值与手工核算一致（fixtures 精确到分）
- [x] 前缀确定性：小规模走簿是大规模的前缀
- [x] 双腿撮合取 min 深度，深腿重报价，无未对冲残仓
- [x] net edge = payout − gross − fees − slippage，逐项可拆
- [x] 费用模型必须显式传入，无静默零费路径

## N3 Arbitrage Scanner

- [x] 未验证映射 / 结算源不等价 / 市场不匹配 → 只产 Rejection
- [x] **Rule Gate 在 production path 上**：`scan_pair` 产出任何 Opportunity 前必须过 `validate_pair_rules`（P0 修复点）
- [x] 对抗性 E2E 套件 `test_scanner_adversarial.py` 全绿（exhaustive=False / ME=False / event 不匹配 / payout 不匹配×2 / 未验证映射 / negRisk 未确认 + 阳性对照）
- [x] staleness（含未来时间戳）拒绝；最小深度过滤生效
- [x] capacity 定义已正式化（capacity_units == 走簿深度下最大可撮合单位）；confidence 明确降级 N7

## N4 Paper Execution

- [x] 状态机非法迁移全拒绝（test_illegal_transitions）
- [x] limit 价保护：**fee-aware**——盘口移动后不再"净"盈利则不追单
  （gross 为正但费用转负的场景必须拒单，test_gross_breakeven_net_loser_rejected）
- [x] 双腿都收费时净为负必须拒单（test_both_legs_fee_turns_pair_negative）
- [x] 部分成交 → 残仓 → unwind 记账精确（成本基础只计已卖部分）
- [x] unwind 深度不足时残仓敞口如实保留上报：`residual_shares` +
  `residual_cost_basis` 分离披露，`net_pnl` 绝不含未平仓残仓本金
- [x] depth_ratio 缩放 ROUND_FLOOR，永不向上虚增深度；floor 到零的档位丢弃不崩溃
- [x] leg B 全拒 → 全仓 unwind，事件时间线完整
- [x] **unwind 身份不变量（N4-BLOCKER-001）**：unwind_book 的
  venue/market/outcome 必须与残仓资产完全一致；不一致 → fail-closed
  （不 unwind、不记 realized PnL、残仓带成本基础保持开放）
- [x] 对抗套件 `test_execution_adversarial.py` 全绿（含 wrong venue /
  wrong market / wrong outcome / valid identity / Verifier 攻击复现）
- [x] 确定性：同输入双跑结果全等（含残仓场景与 events 时间线）

## N5 Replay & Evidence Ledger

- [x] 篡改检测：payload 位翻转 / 中间删除 / 乱序 / genesis 锚改 → verify() False
- [x] 外部锚：尾部截断与全链重哈希改写 → `verify_root()` False
  （verify() 仅证内部一致性，范围已在 docstring 声明）
- [x] 落盘篡改（JSONL 字节级编辑）→ load 后 verify() False
- [x] 确定性回放：跨实例同输入 → 记录流逐字节相同 + root hash 相同
- [x] exec_books 按腿身份匹配，位置交换/外来簿 → 正确匹配或 fail-closed 跳过
- [x] **执行时证据完整（N5-BLOCKER-001）**：Ledger 区分 SCAN_SNAPSHOT /
  EXECUTION_SNAPSHOT；每次执行前钉入实际喂给执行器的两本簿，
  fill/reject/unwind 的依据可从证据独立重建
- [x] **ReplayFrame 持久化 schema 正式定义**（replay.py docstring，版本化，
  版本不匹配 fail-closed）：scan books + exec books 完整保存
- [x] latency replay：save_ticks → load_ticks → replay →
  report / executions / root hash 完全一致
- [x] degraded 分类完整：abort / 残仓未平 / unwind 亏损（即使残仓清零）均计入
- [x] 对抗套件 `test_ledger_adversarial.py` + `test_replay_adversarial.py` 全绿

## N6 Risk Engine & Circuit Breaker

- [x] RiskPolicy 全字段强制显式 + 构造期校验（NaN/Inf/零/负值全拒）
- [x] 九类检查各有触发测试：stale / edge / liquidity / exposure×2 /
  leg risk / abnormal market（单边簿+宽价差）/ data feed / consecutive failure / breaker
- [x] Fail-closed：簿缺失、求值异常、breaker 跳闸 → 一律拒单
- [x] **生产路径**：replay 中 gate 位于 Scanner 与 Executor 之间，
  拒单的 opportunity 不产生 EXECUTION 记录（P0 教训：不能只活保单测）
- [x] Asset Identity：敞口按 (venue, market_id, outcome_id) 键控；
  同 market 不同 venue 隔离；结算后敞口清零
- [x] Provenance：每次决策（含批准）带 measured vs threshold 入账；
  breaker TRIPPED/RESET 均为证据，reset 必须带 operator 且不清敞口
- [x] Replayability：带 gate 的 replay 持久化往返 → report/root hash 一致
- [x] 连续失败熔断：N 次 degraded 后跳闸，后续机会全部被拒并留痕
- [x] 对抗套件 `test_risk_adversarial.py` 全绿
- [x] **执行时风控（N6-P0-001）**：PRE_TRADE 批准绝不蕴含 EXECUTION_TIME
      批准；执行前最后一刻对实际喂给执行器的簿重检 身份/新鲜度/价差/
      可执行流动性/当前净额/feed 完整性（`RiskGate.evaluate_execution`，
      与执行器共享 `leg_b_limit` + `fills_fees`，同一净额公式）
- [x] **执行时对抗回归 6 条**（`TestExecutionTimeGate`）：fresh scan+stale
      exec→BLOCK(STALE_DATA) / normal scan+abnormal exec spread→
      BLOCK(ABNORMAL_MARKET) / scan 有 edge+exec edge 消失→
      BLOCK(EDGE_BELOW_THRESHOLD) / scan 流动性足+exec 不足→
      BLOCK(INSUFFICIENT_LIQUIDITY) / foreign exec identity→BLOCK
      （replay 层 EXECUTION_SKIPPED + gate 层 DATA_FEED_FAILURE）/
      valid+valid→PASS
- [x] **Risk Evidence 区分阶段**：RISK_DECISION 带 `"phase":
      "PRE_TRADE"|"EXECUTION_TIME"`；执行时决策钉入 exec_books 顶档、
      limit_b、exec_net，可从账本独立重建决策依据

## Polymarket Adapter（N1C 后新增）

- [x] 只读：无下单/签名/私钥路径（grep 验证：无 POST、无签名调用）
- [x] 品类费率绑定；未知品类取最高费率
- [x] negRisk 市场 exhaustive=False 直至人工确认
- [x] live smoke test（EAL_LIVE_SMOKE=1）结果附证据

## 全局

- [x] `bash scripts/check.sh` → GATE PASSED
- [x] 六条不可协商门禁逐条有测试落点（见 架构对齐说明-v2.md §3）

**Verifier 签字：独立 Verifier 日期：2026-10-06 结论：N1–N6 PASS（N4/N5 三轮，N6 二轮含 P0 修复）；N7 进入长期实证观察，N8 维持 OUT OF SCOPE**
