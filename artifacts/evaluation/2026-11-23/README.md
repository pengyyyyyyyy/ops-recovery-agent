# 0.2.1 回归与云端验收归档

本目录保存完整失败轮次和修复后回归，不筛选成功运行。案例已用于缺陷分析，所有诊断成绩均为历史回归，不能当作未见案例泛化成绩。

| 轨道 | 入口 |
|---|---|
| 改动前 60 次真实模型运行 | [汇总](before/summary.json)、[逐运行](before/results.jsonl)、[失败分析](before/failure-analysis.json)、[HTML](before/report.html) |
| 0.2.1 的 60 次真实模型运行 | [汇总](after/summary.json)、[逐运行](after/results.jsonl)、[HTML](after/report.html) |
| 完整模型与工具 trace | [改动前 JSON/HTML 压缩包](before/traces.tar.gz)、[修复后 JSON/HTML 压缩包](after/traces.tar.gz) |
| 压缩包成员校验 | [改动前](before/trace-manifest.json)、[修复后](after/trace-manifest.json) |
| 72 小时连续性与业务负载 | [汇总](pilot/summary.json)、[时间线](pilot/samples.json)、[HTML](pilot/report.html) |
| 上下文、skill、10 会话并发 | [上下文](supporting/context.json)、[skill](supporting/skills.json)、[并发](supporting/concurrency.json)、[skill 生命周期](supporting/skill-lifecycle.json) |
| 云端受控故障与恢复 | [数据库升级与重启提案](supporting/cloud-faults.txt)、[worker 恢复](supporting/cloud-worker-recovery.txt)、[应用配置回滚](supporting/cloud-config-recovery.txt) |

两个变体各 6 次本地、24 次公开，固定 60/40 诊断权重。公开静态回放只验证诊断；本地修复有实际 HTTP 探针，使用明确标记的实验自动审批。云端修复经过批准，观察期使用离线模型；付费模型结果来自独立本机实验。

修复后两个变体均 24/24 公开有效诊断、6/6 本地修复。前一轮分别 5/24、3/24 公开有效诊断，保留全部失败。该变化主要体现证据和输出契约修复。双角色未显示准确率优势。skill 三组均 3/3 修复；分层上下文初始字节减少约 75.8%，但总输入 token 增加，这些限制均保留。

云端固定窗口共 6,080 个样本，含 27 个受控故障期间失败，4 个告警全部关闭。最大采样间隔 63.177 秒，故障后连续健康约 100.274 小时。时间线仅保留采样时间、健康标记与事件；源审计全字段 SHA 在 samples.json 中，公开缩减文件自身 SHA 在 [manifest.json](manifest.json)。采样连续性不等于生产 SLA。

完整 trace 包按运行 ID 命名；解压后可以浏览 HTML 或审查 JSON。每个成员含模型请求/响应事件、工具调用与状态推进。实验路径替换为 `$PROJECT`，不包含本机配置、原始数据目录、API 密钥、主机地址或控制令牌。

从保留的本机实验状态重建：

```bash
python scripts/archive_release.py --before reports/v020-final-unified-d22e9481 --after reports/v021-recovery-regression-20dd4610 --output artifacts/evaluation/2026-11-23
```

此命令需要原实验 SQLite 状态，不会调用模型。新机器应按 README 导入固定版本数据并重新运行评测；远端模型别名可能变化，不承诺重新调用得到相同数值。归档文件和压缩包成员均提供 SHA-256 校验。
