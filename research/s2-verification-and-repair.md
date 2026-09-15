# S2：确定性测试验收与有限修复

## 范围与基线

- S1 固定基线：`022371ee769269f38d33cfe2f799884822444505`。
- 本轮只增加配置驱动的 verification、baseline/post 判定以及最多两次的 Developer repair。
- WorkspaceEditor、WorkspaceTransaction、模型 provider 和依赖主版本保持不变；不加入新 Agent、checkpoint、预算、RunRecorder、repo map、Docker sandbox 或 SWE-bench 正式评测。

## 固定源码依据

### mini-swe-agent

固定版本：[`04d809ceab9df28f9adaed044884180159172930`](https://github.com/SWE-agent/mini-swe-agent/tree/04d809ceab9df28f9adaed044884180159172930)。

- [`LocalEnvironment`](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/local.py#L14-L45) 把 cwd、timeout、return code 和异常信息作为明确的执行结果。
- [`_run`](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/local.py#L64-L92) 使用 timeout，并在超时时终止 POSIX 进程组；Windows 路径只能尽力终止进程树。
- [`observation_template`](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/config/mini.yaml) 对长输出保留头尾并报告省略量。

本项目借鉴 timeout、结构化执行结果和头尾诊断。mini-swe-agent 允许模型产生 shell 字符串并使用 `shell=True`，不符合本项目的命令来源约束，因此本项目只接受外部传入的 argv，并固定 `shell=False`。

### SWE-bench

固定版本：[`02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)。

- [`get_logs_eval`](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py#L91-L145) 将 apply/reset/test error/timeout、缺少测试边界、没有 suite 执行证据以及退出码冲突视为无效运行，不把空结果当成功。
- [`get_eval_tests_report`](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py#L148-L244) 按测试身份区分 fail-to-pass 与 pass-to-pass，而不是让模型解释日志后自行宣布成功。

本项目借鉴“执行证据必须存在”和“基线身份由确定性代码比较”。S2 不引入 pytest 等框架专用 parser；失败身份由 check 配置、状态、退出码及完整 stdout/stderr 摘要共同计算。输出中间部分即使被截断，完整流摘要仍参与身份比较。

### Aider

固定版本：[`5dc9490bb35f9729ef2c95d00a19ccd30c26339c`](https://github.com/Aider-AI/aider/tree/5dc9490bb35f9729ef2c95d00a19ccd30c26339c)。

- [`cmd_test`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/commands.py#L996-L1048) 优先使用配置的 `test_cmd`，并在非零退出时把具体输出加入对话。
- [`auto_test`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L1598-L1620) 只在发生编辑后运行测试，把失败文本作为 reflection 反馈给同一个 coder。
- [`max_reflections`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L96-L108) 给反思回路设置明确上限。

本项目借鉴“同一个 Developer 接收失败反馈”和 bounded retry。S2 使用结构化结果而非自由文本，repair 固定最多两次，并且只有确定性分类为 `REGRESSION` 才能进入；TIMEOUT、EXECUTION_ERROR 和未配置检查都不会触发 repair。

## 本项目契约

- `VerificationSpec`：名称、argv、workspace 内 cwd、timeout 和单流输出上限。命令只能由调用方或仓库配置传入。
- `VerificationResult`：`PASS / FAIL / TIMEOUT / EXECUTION_ERROR`、退出码、耗时、限量 stdout/stderr、截断标志、消息和确定性 failure id。
- `VerificationRunner.run(spec)`：统一经过 WorkspacePathResolver，使用 `shell=False`；超时终止进程组/进程树。
- 父图在 Developer 前运行 baseline，在 Developer 后运行相同 specs；没有 specs 时明确返回 `UNVERIFIED`。
- repair 复用同一个 Developer 子图。每轮重新 `begin` 当前文件，将结构化 baseline/post 失败证据传入编辑 prompt，并继续经过 S1 的 stage/commit。

## 判定表

| Baseline | Post | 结果 | Repair |
|---|---|---|---|
| 全部 PASS | 全部 PASS | `VERIFIED` | 否 |
| 全部 PASS | 出现 FAIL | `REGRESSION` | 最多两次 |
| 存在 FAIL | 同一 failure id | `PRE_EXISTING_FAILURE` | 否 |
| 存在 FAIL | Post 全部 PASS | `IMPROVED` | 否 |
| 某些既有 FAIL 变 PASS，其余失败身份不变 | 部分 PASS | `IMPROVED` | 否 |
| 任一 PASS 变 FAIL，或失败身份改变 | 出现新失败 | `REGRESSION` | 最多两次 |
| 任一阶段 TIMEOUT / EXECUTION_ERROR | 任意 | `VERIFICATION_ERROR` | 否 |
| 没有 specs | 任意 | `UNVERIFIED` | 否 |
| 两次 repair 后仍是 REGRESSION | FAIL | `REPAIR_EXHAUSTED` | 终止 |

## 已知限制

- S2 是本机进程执行器，不是安全沙箱；配置命令本身仍应由可信用户或仓库提供。
- 通用 runner 不理解 pytest/Jest 的测试用例语义。failure id 对完整输出做严格摘要比较；含时间戳、随机顺序或临时路径的失败可能被保守地判为变化。
- Windows timeout 先用有限等待的 `taskkill /PID /T /F` 清理当前 PID tree，再用 Job Object 兜底；POSIX 使用独立 process group。本轮不引入容器级隔离。
- `VERIFIED` 只表示配置的 checks 通过，不代表未配置的业务行为正确。

## 本轮实际验证

- 全量 `unittest`：67 项，其中 66 项通过；1 项普通 symlink 用例因 Windows 缺少创建权限跳过，junction 用例通过。
- S2 runner、四类 baseline/post 判定、`UNVERIFIED`、一次 repair 成功、两次耗尽、次数上限和 S1 transaction 拒绝路径均通过测试。
- Python compile、两个修改后 Developer prompt 的渲染以及 `git diff --check` 通过。
- 本轮没有运行真实 Agent 任务或 SWE-bench，因此不声明任务成功率、成本或质量提升。

## S2 Final Gate

- 生产 `swe_agent` 通过 `SWE_AGENT_VERIFICATION_CHECKS` 读取 JSON argv
  checks；未配置仍为 `UNVERIFIED`，字符串 shell command 会在配置解析时被拒绝。
- 顶层 `WorkflowOutcome` 独立表达整体结果。post check 即使通过，也不能把
  rejected、NOOP 或其他 Developer failure 覆盖为成功。
- repair 使用独立 `repair_plan` 收窄到本轮最后提交的文件，并保留 Architect
  原始 `implementation_plan`。两文件验收证明 A 只编辑一次且最终字节保持不变，
  B 单独 repair 后通过。
- verification stdout/stderr 被标记为不可信诊断数据，反馈同时携带两条流的
  truncation 标志。
- Windows 使用有界 `taskkill` 清理当前 PID tree，并以
  [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
  作为 backstop；POSIX 继续使用新 session/process group。所有 wait 都带有限
  timeout，parent-child 延迟写文件测试证明当前环境下 timeout 返回后子进程未继续执行。

Final Gate 全量 `unittest` 共 77 项，其中 76 项通过；1 项普通 symlink 用例因
Windows 当前账户缺少创建权限跳过，junction 用例通过。compile、两个 Developer
prompt render 和 `git diff --check` 通过。

当前 repair 的确定性目标是本轮最后成功提交的文件。若更早编辑的文件通过跨文件
副作用引入回归，S2 没有足够证据自动定位它；本轮不会解析不可信测试文本来猜测路径。

## S2 Acceptance Fix

- repair path 直接复用 S1 的 canonical helper：先转为 workspace 相对路径，再按
  `os.path.normcase` 使用当前平台的大小写语义。
- outcome 先处理 verification error 与不可修复 regression，再处理 Developer
  状态；NO_CHANGES 不能遮住验证错误，PENDING/RUNNING 不能成为 COMPLETED。
- `VerificationSpec` 与环境配置入口都拒绝 NaN、正负 Infinity、零和负 timeout；
  runner 保留同一防御检查。
- `.env.example` 和 README 使用单引号包住 JSON，复制到 `.env` 后由 dotenv
  保留内部 JSON 双引号。

Acceptance Fix 全量 `unittest` 共 83 项，其中 82 项通过；1 项普通 symlink
用例因 Windows 当前账户缺少创建权限跳过，junction 用例通过。compile、Developer
prompt render 和 `git diff --check` 通过。

## S2 Seal Fix

- 父图所有结束分支先进入 `finalize_outcome`；残留的 `PENDING` 会确定性封口为
  `FAILED`，原 `developer_status` 保留用于诊断。repair 路由中的 PENDING 仍只是中间态。
- Windows timeout 固定先执行 0.5 秒上限的 PID tree `taskkill`，随后终止 Job
  Object；进程本身的两次 wait 各有 1 秒上限。parent-child 黑盒测试同时检查子进程
  不产生延迟文件且 cleanup 总耗时有界。

Windows 清理仍是 best-effort：不使用 `CREATE_SUSPENDED` 意味着无法从进程创建瞬间
消除所有竞态；外部重新托管、主动脱离进程树或系统拒绝两个清理机制的子进程不在 S2
保证范围内。

Seal Fix 全量 `unittest` 共 84 项，其中 83 项通过；1 项普通 symlink 用例因
Windows 当前账户缺少创建权限跳过，junction 用例通过。compile、prompt render 和
`git diff --check` 通过。
