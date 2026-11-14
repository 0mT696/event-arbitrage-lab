# 专用服务器准备清单 v1

> 依据隔离契约：独立机器 ≥ 共享 Hermes 的 Runtime Domain 隔离。本机只服务于
> `event-arbitrage-lab`，不得出现任何 GPTHEIST 数据、配置或进程（双向隔离 §8）。

## 1. 规格建议（研究阶段足够）

- 2 vCPU / 4 GB RAM / 40 GB SSD；Ubuntu 24.04 LTS
- 出口需可达：`gamma-api.polymarket.com`、`clob.polymarket.com`（+ 后续 venue）
- 系统时区 UTC（staleness 判断依赖时区正确的时钟，NTP 必开）

## 2. 上线步骤

```bash
# root 或 sudo 执行；默认从 GitHub 公开仓库克隆
bash scripts/bootstrap_server.sh

# 如果 GitHub 不便，用 bundle 离线初始化：
git clone event-arbitrage-lab-n1-n3.bundle event-arbitrage-lab
```

bootstrap 做的事：装 Python 3.12 + git → 建 `eventarb` 独立服务账户（契约 §6 推荐）→
克隆并安装 → 跑 `scripts/check.sh` 门禁 → 跑 Polymarket live smoke 验证出网。

## 3. 安全基线（研究阶段）

- **本机不放任何交易凭证/私钥**——契约 §14，N8 之前没有例外
- 只跑 `eventarb` 非 root 账户；不开除 SSH 外的入站端口
- 与 GPTHEIST 基础设施零共享：不同机器、不同账户、不同日志/备份路径
- 长期任务（N7 soak test）后续用独立 systemd unit：`eventarb-scanner.service`

## 4. 验收这台机器就绪的标准

1. `bash scripts/check.sh` → GATE PASSED
2. `EAL_LIVE_SMOKE=1 .venv/bin/python -m pytest tests/test_polymarket_live.py` → 1 passed
   （证明 Gamma/CLOB 出网正常、canonical 映射对真实数据成立）
3. `ps aux | grep -i gptheist` → 空（双向隔离的机器级确认）

**Version: Server Bootstrap v1**
