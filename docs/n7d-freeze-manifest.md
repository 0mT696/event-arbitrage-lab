# N7D-OBS-001 冻结清单（Experiment Freeze Manifest）

本清单在 T0 前固定本次正式观察窗口的全部实验参数。T0 → T+168h 期间冻结；
任何改变实验语义的修复都会使原窗口失效并以新版本重启 168h。

## 代码

- 代码 commit：`428752e`（Harden checkpoint envelope: sha256 over full canonical payload v2）
- 冻结 commit（含本清单）：见本文件提交 SHA（checklist/本文件变更不影响执行语义）
- 测试基线：240 passed / 1 skipped（ruff 未安装，gate 跳过），双侧 check.sh PASS

## 部署单元（systemd，sha256）

| 单元 | sha256 |
|---|---|
| /etc/systemd/system/eal-collector.service | f2de5a7189f350239ef41863634c0a0941b0ab0e5b8e5e4a802c92eaf48484ce |
| /etc/systemd/system/eal-paper.service | 00b8e11a704d398fb9ce31293b8d88468f0a742258e581a640a82f6c3cf7b17b |

- collector：`--out /root/eal-data/n7/sweeps --interval 10 --targets 30 --min-volume 50000`
- runner：`--data /root/eal-data/n7/sweeps --out /root/eal-data/n7/paper --size 100`
  （默认 poll=5s，--retire-after 30，--max-staleness-s 45，--max-consecutive-failures 5）

## Venue（只读）

- polymarket：gamma-api.polymarket.com（发现/meta/feeSchedule）+ clob.polymarket.com（订单簿）
- 无凭证、无签名、无下单路径；Adapter 只读（发现、订单簿、历史）。

## 市场筛选规则（market-selection semantics）

- 按 volume24hr 降序分页（page=50）取前 30 个 ≥ 50000 的市场
- 仅二元 MECE：恰好 2 个 outcome 的 CTF 市场（非 negRisk）即互斥穷尽；negRisk 一律 exhaustive=False
- collector 侧 404（闭市）记录失败帧后移出目标；runner 侧连续 30 sweep 不可用且缺席 → MARKET_RETIRED
- 每 6h 重新发现（rediscovery）

## 采样周期

- sweep 间隔 10s；runner poll 5s；延迟模型：exec 簿 = 同一市场的下一帧 sweep（edge decay 直接测量）；
  闸门时钟取自订单簿 fetch 时间戳，不取处理时间；批次最后一帧不交易（无零延迟偏差）

## RiskPolicy（冻结值）

| 参数 | 值 |
|---|---|
| max_staleness | 45s |
| min_net_profit | 0.01 |
| min_net_edge_per_share | 0.0001 |
| min_capacity | 1 |
| max_exposure_per_asset | 10000 |
| max_total_exposure | 100000 |
| max_leg_risk_shares | 10000 |
| max_spread | 0.2 |
| max_consecutive_failures | 5 |
| size（每笔 paper 规模） | 100 |

## fee / slippage 参数

- fee：每市场 Gamma feeSchedule 优先；公式 fee = C × feeRate × p × (1−p)，taker-only，5dp 舍入；
  exponent≠1 或缺失 → feeRate 取 MAX 0.07（费率规则已于 2026-10-06 对官方文档核验）
- slippage：SlippageBuffer per_unit = 0（纸面成交按簿价，无额外缓冲；延迟成本由
  下一帧执行簿模型承担）
- processor min_profit = 0（闸口利润门槛在 RiskPolicy 侧）

## Checkpoint 语义

- envelope v2：sha256 覆盖完整 canonical body（v / state / ledger_root / sweep_file /
  sweep_offset）；任一字段篡改、结构畸形、v1 旧格式均 fail closed
- checkpoint/ledger root 分叉 = torn write = RuntimeError，拒绝 clean state
- 允许 systemd 自动重启：必须从 durable checkpoint 恢复 Risk State + Ledger + sweep offset

## 失效与续用规则（冻结期间）

- 冻结：策略、Scanner、Risk Policy、fee/slippage、execution semantics、market-selection
  semantics、统计口径。禁止为改善结果调参。
- P0/P1 修复若改变实验语义 → 原窗口标记 invalid，新版本重启 168h。
- 纯基础设施恢复（不改语义）→ 原窗口继续，但必须留下 downtime/feed-gap evidence。
