# Ops Recovery Agent

基于 **smolagents** 的运维排障工程项目：通过只读工具收集证据，使用隔离上下文的诊断 Agent 和复核 Agent形成结论，经人工批准执行白名单修复，再以实际 HTTP 请求验证服务恢复。

重点是工具失败、写入结果未知、审批失效和会话恢复时的行为。首版支持 Windows 原生多进程，不要求 Docker。公开 RCAEval 数据参与独立诊断评测。

> 这是可复现的实验系统。离线脚本模型用于验证工程行为，不能当作 LLM 准确率；本地服务实验也不代表真实生产部署。

## 能展示的工程能力

- SQLite WAL 检查点、运行租约、跨 CLI 进程恢复；持久化工具和模型 trace。
- 结构化工具错误、有限只读重试；写请求失去响应后按幂等键查询并对账。
- 审批绑定运行 ID、事件代次、目标服务、动作和诊断摘要；拒绝过期或被修改的请求。
- 独立诊断/复核上下文、一次有界返工；schema 校验反馈进入 Agent Loop，异常输出可自行纠正。
- 真实 MCP stdio 服务及客户端；只暴露有参数校验的调查工具，Agent 无 shell、文件读取或审批工具。
- 实际进程崩溃、HTTP 依赖超时、SQLite 锁竞争；修复后连续 3 次请求返回 200 才判定成功。
- RCAEval 固定版本、文件哈希、按服务与故障组合隔离的开发/测试集；匿名证据视图与答案分离。
- 单 Agent 无自动重试、单 Agent 有重试、双 Agent 有重试三组消融；费用账本跨运行累计。

## 快速运行（Windows PowerShell）

需要 Python 3.12 或 3.13。所有 API 设置均在本机，无密钥也能运行离线闭环。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,datasets]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }

.\.venv\Scripts\ops.exe lab up
.\.venv\Scripts\ops.exe lab inject database_lock
.\.venv\Scripts\ops.exe diagnose --mode offline --mcp
```

最后一条命令会输出 `run_id`、诊断、完整 `proposal_digest` 和 HTML 报告地址。审阅后执行：

```powershell
.\.venv\Scripts\ops.exe approve <run_id> --digest <完整proposal_digest>
.\.venv\Scripts\ops.exe resume <run_id>
.\.venv\Scripts\ops.exe lab probe
.\.venv\Scripts\ops.exe lab down
```

`approve` 只写入审批；`resume` 才执行修复。CLI 退出后实验服务可继续运行，使用 `lab down` 停止项目启动的服务。审批有效期 10 分钟；租约用于阻止并发恢复同一个运行，异常进程退出后需等待租约到期。

Linux/macOS 将 `.venv\Scripts\ops.exe` 换为 `.venv/bin/ops`。锁定依赖复现可先安装 `requirements.lock.txt`，再 `pip install -e . --no-deps`。

## 本机接入模型

`.env` 已被 Git 忽略。仓库只发布 `.env.example`，任何密钥都不得写入源码、提交、issue 或评测输入。

```dotenv
OPS_MODEL_MODE=online
OPS_API_BASE_URL=https://api.deepseek.com
OPS_MODEL_ID=deepseek-flash
OPS_API_KEY=在本机填写
OPS_API_STYLE=deepseek
OPS_INPUT_PRICE=2
OPS_OUTPUT_PRICE=8
OPS_BUDGET_CNY=100
```

这里的 DeepSeek 配置是一个本机示例。其他支持工具调用的 OpenAI Chat Completions 兼容服务，设置相应 URL、模型、密钥和单价，并用 `OPS_API_STYLE=openai`。标准接口不兼容的服务需另写模型适配器。

DeepSeek 单价为 2026-11-18 官方高峰价快照：输入/输出每百万 token 2/8 元，运行前自行核查[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。费用记录按配置的保守价格估算，不是服务商账单；缓存折扣未扣减。请求发出前预留费用，无响应时保留预留，避免把未知消耗算成零。

真实模型会收到工具 schema、提示词、所查询遥测和中间诊断。当前实验数据为项目模拟数据或公开数据。先运行 `ops privacy-preview` 查看发送内容示例，再运行 `ops diagnose --mode online`。模型 HTTP 请求没有 SDK 隐式重试。

## 公开数据与评测

数据来源：[RCAEval](https://github.com/phamquiluan/RCAEval)、[公开数据仓库](https://huggingface.co/datasets/phamquiluan/RCAEval)。首版使用 RE2-OB 的指标与日志，保留 3 个开发组、12 个测试组，故障覆盖 CPU、内存、磁盘、延迟、丢包及 socket。每个组只取一个重复样本，同组其他重复样本不会出现在另一数据划分。

```powershell
.\.venv\Scripts\ops.exe data-import --dev 3 --test 12
# 不调用付费 API：90 次工程运行（3 类本地 + 12 个公开案例）× 3 变体 × 2 次
.\.venv\Scripts\ops.exe evaluate --mode offline --suite all --repeats 2 --name offline-engineering
# 先用开发集调试协议，再固定提示词运行测试集
.\.venv\Scripts\ops.exe evaluate --mode online --suite public --split dev --repeats 1 --variants dual --name online-dev
.\.venv\Scripts\ops.exe evaluate --mode online --suite all --split test --repeats 2 --name online-heldout
.\.venv\Scripts\ops.exe budget
```

复现本仓库归档所用的数据版本时，在**首次导入之前**固定公开仓库 revision（已有数据清单不会被静默替换）：

```powershell
New-Item -ItemType Directory -Path data/raw -Force | Out-Null
Set-Content -Encoding ascii data/raw/revision.txt afeacb11bcc94dadfd1c8f483ee4377b2b8b614e
.\.venv\Scripts\ops.exe data-import --dev 3 --test 12 --seed 20261118
```

同时使用 requirements.lock.txt 固定依赖；外部模型别名仍可能随服务商更新，历史逐运行结果和响应 trace 用于核查当时结果，不承诺将来调用逐位一致。

- `data/raw`：公开原始数据；`data/answers`：独立评分答案；`data/blind`：匿名任务和证据。
- Agent 只能调用读取 `data/blind` 的工具，不能访问答案、原始案例目录名或其他项目文件。
- 诊断主指标为根因服务 AC@1，另报接受且正确的比例、复核拒绝率、耗时、调用数与错误。
- 本地修复另报探针验证通过率、未知结果恢复数和重复修复事件数。测试审批明确标记 `automated-test-policy`，不是人工审批效果数据。
- 诊断汇总权重：本地 60%、公开 40%，同时保留两条轨道的分项指标。静态公开数据不参与修复评分。
- 每份实验目录包含 `protocol.json`、逐运行 `results.jsonl`、`summary.json`、HTML 总结与各会话 trace。实际结果见 [评测记录](docs/EVALUATION.md)。

已知限制：样本较小；重复调用相关，不能把全部运行当作独立故障；模型别名可能变化；本版未导入原始分布式 traces；不能排除公开数据的预训练污染。本地故障环境主要验证执行机制，不能据此推断广泛运维推理能力。

### 2026-11-18 实测

DeepSeek Flash，核心源码摘要 `5a0185bf94ec`（完整摘要见评测协议），12 个公开测试组各重复两次；本地 3 类故障各重复两次，共三组 90 次真实模型运行。

| 变体 | 公开有效诊断 AC@1 | 公开平均模型调用 | 本地实际修复 | 60/40 加权诊断 |
|---|---:|---:|---:|---:|
| 单 Agent、工具单次尝试 | 75.0% (18/24) | 7.17 | 6/6 | 90.0% |
| 单 Agent、只读有限重试 | 91.7% (22/24) | 5.33 | 6/6 | 96.7% |
| 双 Agent、只读有限重试 | 87.5% (21/24) | 9.00 | 6/6 | 95.0% |

“有效诊断”要求系统完成合法结构化输出。部分失败运行在自由文本中提到了正确服务，但耗尽步骤后没有合法结构化结果，仍计失败；这些提升主要反映执行与协议完成率，不能声称模型推理能力提高。双 Agent 没有在本样本上超过可靠执行单 Agent，而且调用更多。

全部 18 次修复都模拟了成功后响应丢失，均通过对账完成恢复，重复修复事件为 0。正式评测按配置保守单价估算 6.216738 元，非服务商账单。

完整[HTML 报告](artifacts/evaluation/2026-11-18/report.html)、[协议与汇总](artifacts/evaluation/2026-11-18/summary.json)、[逐运行结果](artifacts/evaluation/2026-11-18/results.jsonl)、[失败分析](artifacts/evaluation/2026-11-18/failure-analysis.json)均保留。三个变体并发运行、共享主机，因此耗时只作描述；样本不支持广泛泛化结论。要使用实测较经济的单 Agent 方案，可在 diagnose 时传入 `--single`，默认仍保留双 Agent 演示。

## 代码结构

```text
src/ops_recovery/
  coordinator.py     双 Agent 调度、校验反馈、审批与恢复状态机
  execution.py       有限只读重试与写入未知结果语义
  storage.py         SQLite 检查点、运行租约、顺序 trace、预算账本
  models.py          离线工程基线、带预算的通用 HTTP 模型适配器
  tools.py           smolagents 工具与参数/证据校验
  mcp_server.py      只读 MCP 工具服务
  mcp_transport.py   实际 stdio MCP 客户端和生命周期
  lab.py            监督进程、业务服务、故障注入、幂等修复
  backends.py       本地服务/公开匿名数据/工具故障适配器
  datasets.py       数据版本固定、盲测导出和分组划分
  evaluation.py     评测、消融、评分及结果持久化
  report.py         对不可信内容转义的独立 HTML 报告
  cli.py            命令行入口
tests/              单元测试、真实多进程与 MCP 集成测试
```

技术文档：[架构](docs/ARCHITECTURE.md)、[设计决策](docs/DECISIONS.md)、[评测协议](docs/EVALUATION.md)、[运行排查](docs/RUNBOOK.md)、[贡献指南](CONTRIBUTING.md)。

## 测试与 CI

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
```

测试不使用付费模型。若系统临时目录权限异常，可指定一个全新的工作区临时目录：`pytest --basetemp .state/tests-<唯一名称>`。该目录专用于测试，pytest 会管理其中内容。

GitHub Actions 配置覆盖 Windows/Linux、Python 3.12/3.13。配置了 CI 不代表远程 CI 已实际运行；请以上传后的 Actions 结果为准。

## 开源与归属

本项目采用 Apache-2.0。smolagents 提供 Agent Loop 与工具调用基础；本项目实现其上的可靠执行、审批与恢复、双角色编排、本地实验和评测闭环。没有将框架能力归为自研。依赖和数据归属见 [THIRD_PARTY.md](THIRD_PARTY.md)。
