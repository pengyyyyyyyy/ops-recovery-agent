# 运行与排查

## 最短演示路径

1. `ops lab up` 启动监督进程、gateway 和 inventory。
2. `ops lab inject api_crash` 真实终止项目拥有的 gateway 进程。
3. `ops diagnose --mode offline --mcp` 通过实际 MCP 调查，生成修复建议和 HTML trace。
4. 审阅服务、动作、事件代次和摘要，执行 `ops approve <id> --digest <完整摘要>`。
5. 退出并重新打开终端，`ops resume <id>` 从 SQLite 恢复执行，再 `ops lab probe`。
6. `ops lab down` 关闭实验环境。

dependency_timeout 将真实 inventory 请求延迟设为 1.2 秒，超过 gateway 的 0.35 秒上游超时；database_lock 用独立进程持有 SQLite 排他锁，gateway 写事务实际失败。

## 常见状态

- waiting_approval：尚未批准；不会执行修复。
- approved：审批已保存，等待恢复/执行。
- unknown：写请求结果不确定；再次 resume 会先查询同一操作 ID，不直接当作失败重放。
- rejected：复核拒绝或证据不足；没有修复授权。
- failed：schema、预算、步骤上限或实际探针检查失败；打开报告定位。
- resolved：当前实验中的三次实际请求均成功，不表示长期 SLO。

## 常见问题

- “Configure OPS_...” ：确认修改的是项目根目录 `.env`，已经保存，密钥字段非空。不要把密钥发到 issue 或聊天。
- “permission denied”：控制接口需要本机 manifest 中的令牌；使用提供的 CLI，不从外部公开控制端口。
- “scope changed”：审批后注入了新事件，或实验环境重启；重新诊断并获取新审批。
- “Approval expired”：批准有效期为 10 分钟；重新诊断，不通过修改数据库绕过检查。
- “Run is owned”：另一进程正在处理或之前非正常结束；等待运行租约到期。CLI 正常退出会释放租约。
- “Model context exceeds...” ：当前上下文超出项目输入上限，属于明确失败；缩小开发任务或工具输出范围后建立新评测迭代，不能悄悄改测试条件。
- 首次 query_logs 超时：正式评测故意注入一次工具传输错误。查看 trace 中尝试次数及模型是否自行纠正。
- 非 JSON 最终输出：Agent 可能耗尽步数后产生自由文本 fallback；外层 schema 拒绝并记录失败，不能执行修复。查看 model_response 与 output_rejected 的最后几步。

## 数据和输出位置

- `.state/runs.sqlite3`：检查点、trace 和累计预算；不要为了重跑而删除账本以绕过总预算。
- `.state/lab/manifest.json`：本机监督服务地址和控制令牌，不发布。
- `data/manifest.json`：公开数据版本、文件哈希、处理协议。
- `reports/<experiment>/`：每次实验的原始结果、HTML 和单运行 trace；自动忽略 Git。
- `artifacts/evaluation/`：审查后归档的可分享结果。

监督进程本身的故障接管不在首版范围；如果它非正常退出，先确认并关闭它启动的残留实验服务，再新建实验。不会根据不可信 PID 文件自动终止任意系统进程。

## Docker 后续入口

仓库提供 Dockerfile，容器主进程为监督服务，可用 docker exec 运行 CLI。Docker 镜像构建尚未验证。Windows/Linux 与 Python 3.12/3.13 的测试状态见仓库 GitHub Actions。
