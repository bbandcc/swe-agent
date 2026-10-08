# SWE Agent 架构优化调研计划

## 状态文件治理（长期有效）

- 本文件是项目当前进度的唯一实时状态源；`MASTER_PLAN.md` 只维护总目标、阶段设计和 D 项，不记录日常进度。
- 每当阶段开始、完成/审核通过、出现 blocker/遗漏/风险、计划或验收变化、下一阶段变化时，必须同步更新本文件。
- 每次实质更新后，先执行 `git diff -- task_plan.md` 和 `git status`，再将本文件与相关代码/测试放入同一完整 checkpoint 提交；配置远程且具备权限时继续 push，并如实记录失败。
- 只有源码、测试和审核证据确认后，阶段才能标记为完成；不得按计划预先勾选。
- 新阶段开始前必须核对工作区文件、HEAD 中版本和远程跟踪分支；发现未提交或未推送内容时，先处理同步再开始。

### 当前状态文件基线

- S5a Final Seal 已通过此前 ChatGPT Final Gate，并已同步 upstream。
- D9/S4b 本次全项目审计排定的三个 blocker 均已封口、通过 ChatGPT Final Gate 并 push：production tree completeness evidence 的最终审核提交为 `25e0222d57e606a48de5ae16aaaa527c3348a61f`；tree filesystem enumeration hard bound 的最终审核提交为 `436e03e926ee8a7505066312bd74b6fdb63defc3`；search filesystem enumeration hard bound 的最终审核 HEAD 为 `39c8e3fccb9fc8ffa66c57968f2a573a5a1e2415`。search 中间提交 `aeb2ad0448f5a4b56cdcb1708641e1c71cb22662` 曾因 exact-cap false truncation 未通过 Final Gate，已由 `39c8e3f...` 修复并通过 Gate、push。tree enumeration 使用独立 4096 项预算，过滤前计入目录及 ignored、protected、unsafe/link/hardlink entries；预算用尽后进行常数级 lookahead，发现额外项或仍有未检查目录而无法证明完整时报告 truncated / warning，未扫描后缀无 continuation。search 使用独立 4096 项预算并在过滤前计入目录、ignored、protected、unsupported、link/unsafe entries；只有确认存在未扫描后缀才报告 truncated，且该截断边界不提供 continuation，避免无状态 cursor 重扫造成 livelock。现有 query/evidence-bound cursor、raw/tree 读取上限及语义保持。RunConfig/checkpoint 未因这些 slice 改变，semantic config schema 保持 v7。
- D7/S3.5b workspace revision bounded Git evidence 的 Final Seal `eba39a66ba16c68cb5fba8f5dcf064646bd69918` 已通过 ChatGPT Final Gate 并 push。workspace revision Git stdout 上限为 1 MiB、stderr 上限为 64 KiB；status、HEAD、diff 增量读取，overflow/timeout/query failure 返回 `UNKNOWN / QUERY_FAILED`，不以 partial output 生成可信 digest；RunRecord v2 与 semantic config schema v7 未改变。
- 本轮文档治理的生产代码基线为 `eba39a66ba16c68cb5fba8f5dcf064646bd69918`。本轮开工时 branch/upstream 为 `review/step3-runtime-recovery` / `origin/review/step3-runtime-recovery`，HEAD 与 remote-tracking HEAD 一致，工作树 clean、ahead/behind `0/0`。这些 Git 数字记录本轮文档修改前的开工状态。
- 此前排定的四项技术 blocker（D9/S4b tree evidence、tree enumeration、search enumeration + exact-cap repair，以及 D7/S3.5b bounded Git evidence）均已通过 ChatGPT Final Gate 并同步；S5a Final Seal 也已通过并同步。
- project-state Final Seal 已通过 ChatGPT Final Gate。状态文档提交 `3f30b756367d91c44893ade404adfcb0850f3d32` 与 `8ec2a1d6934e55b588d278cdb6750e9340b4c6c7` 已正常 push；该 Final Gate 同步基线 HEAD 为 `8ec2a1d6934e55b588d278cdb6750e9340b4c6c7`，branch/upstream 为 `review/step3-runtime-recovery` / `origin/review/step3-runtime-recovery`，工作树 clean、ahead/behind `0/0`。
- 基于固定审核 HEAD `ea81eb561061076296f04d7cab66b3e1b8c20d91` 的 full-project re-audit blocker repair 提交 `4469758f80e0e84b9b5bd1f17bf6d8ca50565860` 已通过 ChatGPT Final Gate 并同步 upstream；这不等同于 GitHub CI 或独立外部测试证据。
- 本轮仅修复锁定测试依赖、贡献文档、WorkspaceEditor 契约措辞及 S5a 离线测试 helper；生产 validator/runtime 行为未改。pytest 仅属于 `[dependency-groups].dev`；`uv.lock` 只新增 pytest 与 iniconfig、pluggy、Pygments，不升级现有 runtime 包。
- 四个历史 seed 的 target/oracle revisions、allowed scopes、oracle hashes 与 historical `lock_sha256` 均未修改。测试 helper 对 source-backed offline validation 使用清单自身的 historical environment；另有回归证明显式传入当前环境仍返回 `ENVIRONMENT_MISMATCH`，且 target revision 的 lock blob 不匹配仍 fail closed。
- 本轮使用 workspace 外 uv 环境/cache，`uv sync --locked` 成功，Python 3.12.3、pytest 8.4.2。首次 re-audit 暴露的 pytest 缺失所致 8 failures + 1 error 已消失；修正历史测试语义后，unittest 438 tests OK / 10 skipped，pytest 432 passed / 10 skipped / 25 warnings，S5a 专项 unittest 19 tests OK，S1–S5a 关键 seam unittest 442 tests OK / 10 skipped。
- compileall、11 prompt render、root/start/resume CLI help、`git diff --check` 均通过。pytest 的 25 条 FutureWarning 来自锁定 Tree-sitter 旧 API，10 项 skip 为既有 Windows link capability 限制。S5d.2 repair commit `9938d633...` 对应的 GitHub Actions run `36391267054` 已在 Ubuntu/Windows 成功；这不构成真实模型或 benchmark 证据，本地/托管测试通过也不等于 full-project audit PASS。
- S5d.1 实现提交 `05082eb2e33d4880a38918fcc3462226a2ea3798` 与 project-state seal `dcc6c8571a29e51bca26362d9fa892fb81b6fb3e` 均已通过 ChatGPT Final Gate 并同步 upstream；S5d.2 也已完成并同步。S5b.1 技术提交 `6de5d32122093a78508defae282ceec5f7c52ae9`（`feat(S5): 建立隔离任务执行边界`）已通过 ChatGPT Technical Final Gate 与 SYNC_AUDIT，并由用户 push；其 docs seal `08d902906b38993f0d6a2cda4df399d3918bc193` 已通过 `PROJECT_STATE_FINAL_GATE` 并由用户 push。S5b.2 preflight 开工时 HEAD/upstream 同步、ahead/behind `0/0`、工作树 clean；S5b.1 正式完成。S5b.2 binary-only frozen dependency preparation boundary 已由 `c471b3b0145d9ce30fec3f6675cb0f4d38b8d9aa` 完成、通过 ChatGPT Technical Final Gate 并同步 upstream；但 historical lock 的 `fuzzysearch==0.7.3` 无 binary wheel，S5b.2 仍受 ENVIRONMENT/TOOL_DEPENDENCY blocker 阻塞。当前 isolated source-build feasibility preflight 已取得并验证 pinned sdist，静态 build 配方已核实；动态构建依赖/CPython 3.12 兼容性与 AppContainer 隔离构建可行性仍待实验，不能宣称 FEASIBLE；未执行 sdist/build backend。S4d 不提前于 S5b.2，repo map、embedding 与 S4e 不作为默认必做项。
- S3.2 production 技术冻结点：`5850b8370c49f868e90aeffe9e6042f85eaa522c`；S3.2、D1-D6、D7/S3.5a、D7/S3.5b、S2d/D3、S4a/D8 及 S4b/S4c 既有实现保持其各自已验收契约。S4c.1 Python、S4c.2 JavaScript/JSX、S4c.3 TypeScript、S4c.4 TSX 均已通过 ChatGPT 技术 Final Gate；S4c.4 冻结 HEAD `3ebefa53da38152381a11bfe8d526cbb3bbe60c8` 已正常 push。TSX、repo map 与 D11 context assembly 之外的能力边界继续按各阶段记录；符号 adapter 只承诺已测试的语言和声明/组件范围。
- D2/S2b Final Seal 已收窄 library/CLI 的异常边界；`RunSummary.warnings` 只输出 preflight warning code，unexpected `RuntimeError`、`KeyboardInterrupt` 和 `SystemExit` 不被入口吞掉。
- 最近一次代码 slice 的 Codex 本地验证（D7/S3.5b workspace revision evidence）：unittest 436 tests OK / 10 skipped；pytest 430 passed / 10 skipped / 218 subtests passed；`python -m compileall -q agent tests` 与 `git diff --check` 通过。revision 专项 unittest 9 tests OK；workspace revision、Agent revision、RunRecord/trajectory、access policy/admission、verification runner/workflow/recovery 相关 unittest 132 tests OK / 6 skipped。pytest 的 25 条 FutureWarning 来自锁定 Tree-sitter 旧 API；10 项 skip 未改变既有平台/能力边界。这些是该 slice 的本地证据，不是本轮 full-project re-audit 结果。
- 已有 S5d.2 GitHub Actions hosted-runner 证据；没有独立外部测试、真实模型或 benchmark 证据，不作相应声明。
- 剩余能力：完整 secret-safe state persistence（当前只覆盖 RunConfig 中已知凭据）、verification rerun/replay、超出当前明确列出的语法子集的完整语言语义、repo map 与 D11 context assembly 仍未实现；当前符号 adapter 只承诺 Python、JavaScript/JSX、TypeScript 和 TSX 的已测试声明/组件范围。D7 policy 只约束使用本 seam 的合作进程和模型可调用工具，不阻止第三方直接写 workspace，也不是 OS sandbox。本轮 EventSink/RunRecord 与 verification artifact 只提供单进程本地 JSONL、结构化记录和有界/脱敏日志，不是完整恢复系统。provider 正向 retry/独立 attempt 语义未实现，当前 durable 只允许单次外部尝试；当前模型 `request_digest` 只代表有界语义输入，身份范围明确为 `PARTIAL`，不能据此安全重放最终 rendered request；exactly-once 不承诺。JUnit XML 是当前唯一可信报告格式，未配置或报告缺失/畸形时保守返回证据不足；多框架 parser registry、扩展 repair 尚未实现。runner 只改写精确匹配的 `--junitxml/--junit-xml` destination 到 owned temporary output，workspace report_path 保持原 bytes/mtime；owned temp cleanup 失败返回结构化 `EXECUTION_ERROR`。S3.4b 对命令已启动但结果未持久化的恢复默认保守返回 `OUTCOME_UNKNOWN`；S5b.1 的隔离执行 seam 不接入该恢复链，也不支持自动重跑；pytest 对其它 workspace 文件的副作用仍未提供 recovery。S4b raw-read cursor 用文件 stat 元数据摘要识别常规变化，并非全文件快照/强内容锁；tree 和 search enumeration 各自最多计入 4096 个目录项，search 允许最多 cap+1 个消费条目确认后缀，仅在确认存在未扫描后缀时返回 truncated/warning 且不给 continuation；tree 未扫描后缀也没有 continuation。
- 后续技术切片继续按 MASTER_PLAN §5.2 执行；保留现有 S1/S2/S3 历史编号，不重新开启或重命名 S1。
- MASTER_PLAN 对应：当前实现事实覆盖 S3.2、D1/S1a、D2/S2b、D3/S2c、D4/S3.2a、D5/S3.4a、D5/S3.4b、D6/S3.3a、S3.3b、S3.3c、D7/S3.5a、S3.5b Final Seal、S2d/D3 受控多文件 repair、D8/S4a 工具消息完整传递、D9/S4b 有界读取及 D10/S4c.1 Python、S4c.2 JavaScript/JSX、S4c.3 TypeScript、S4c.4 TSX 符号提取；D11 context assembly 尚未实现。S5d.2 已完成 Technical、hosted-runner 与 project-state Final Gate 并同步；S5b.1 也已通过 Technical Final Gate 与 SYNC_AUDIT 并同步。当前 S5b.2 首个 task-pinned Python 环境 slice 受 historical lock 缺 binary wheel 阻塞；S4d 不得提前。

## D12/S5d.1：可复现安装（已通过 Final Gate）

- 状态：S5d.1 实现提交 `05082eb2e33d4880a38918fcc3462226a2ea3798`（`fix(S5): 封闭可复现安装契约`）已通过 ChatGPT Technical Final Gate；project-state seal `dcc6c8571a29e51bca26362d9fa892fb81b6fb3e` 已通过 ChatGPT Final Gate 并 push。S5d.1 已完成并同步。
- 基线：S5d.2 开工 HEAD `dcc6c8571a29e51bca26362d9fa892fb81b6fb3e` 已同步 upstream；branch/upstream 为 `review/step3-runtime-recovery` / `origin/review/step3-runtime-recovery`，开工时工作树 clean、ahead/behind `0/0`。
- 范围：prompt 显式 UTF-8、wheel package-data、非仓库 cwd、外部 clean wheel install 与安装入口一致性。
- [x] 阅读 MASTER_PLAN D12/S5d.1、AGENTS、task_plan、pyproject/uv.lock、prompt loader、11 个 prompt、相关测试与安装文档。
- [x] 通过公开 `markdown_to_prompt_template` 增加 11 prompt UTF-8/render 与非仓库 cwd 测试；先运行确认旧 loader 未传 encoding 时失败，再修复。
- [x] Clean source copy 经 `uv build --wheel --no-build-isolation` 生成 wheel；wheel 检查确认 production package 与 11 个 prompt 文件齐全。外部 Python 3.12 环境由 `uv sync --locked --no-install-project --no-dev` 准备，wheel 安装后从非 repo cwd、无 `PYTHONPATH` 导入并 render 全部 prompts；模块路径和 distribution metadata 证明不是 editable install。
- [x] 最小 production 修复：prompt 两处 Markdown text open 均显式 `encoding="utf-8"`；现有 setuptools package-data 已由实际 wheel 检查证明有效，因此没有改 packaging 配置或依赖。
- [x] README 安装/运行/测试命令统一使用 `--locked`；CONTRIBUTING 原有入口已核对一致。`pyproject.toml` 和 `uv.lock` 未改变，没有依赖或 packaging 配置变化。
- [x] 使用外部 cache/project environment 执行 `uv sync --locked` 成功；uv `0.12.1`，Python `3.12.3`。
- [x] 全量 unittest：440 tests，0 failures/errors，10 skipped；全量 pytest：434 passed、10 skipped、25 warnings。S5d.1 专项 2 tests 通过；S1–S5a 关键 seam 模块显式回归 420 tests，0 failures/errors，10 skipped。
- [x] `python -m compileall -q agent tests`、11 prompt render、root/start/resume CLI help、`git diff --check` 均通过。README 与测试文档中的 canonical `uv` 入口已复核。
- [x] 验证通过后已独立提交 `05082eb2e33d4880a38918fcc3462226a2ea3798`：`fix(S5): 封闭可复现安装契约`；未 amend、未 push。
- 已知限制：unittest / pytest / seam / wheel 和其它运行数字均为 Codex 本地执行证据；pytest 的 25 条 warning 是锁定 Tree-sitter 旧 API 的 FutureWarning；10 项 skip 是 Windows 当前 symlink/junction 能力边界。临时 wheel 环境使用 `--link-mode copy`，避免与执行测试的 Windows Python 共享已加载 `.pyd` hardlink。没有真实模型或 benchmark 证据。

## D12/S5d.2 自动化 gate（已完成并同步）

- 状态：repair commit `9938d633083ebf0229408e31d07b6b8172eabaad`（`fix(S5): 修复托管 CI 跨平台回归`）及 state seal `aeacb08c12cb1a018e33ba07d952568cdf7c9212` 均已通过相应 ChatGPT Final Gate 并同步 upstream；S5d.2 已完成。其 hosted-runner 事实继续以本节 run 记录为准。
- 开工状态：本地 HEAD 与 GitHub branch `review/step3-runtime-recovery` 均为 `9938d633083ebf0229408e31d07b6b8172eabaad`，工作树 clean、ahead/behind `0/0`；提交在本轮核验前已同步，因此未重复 push。
- Hosted run #1：GitHub Actions run `36376012714`，commit `733d607...`。Ubuntu 与 Windows 的 `uv sync --locked` 和 S5d.1 clean-wheel/install 均实际通过。Ubuntu 全量 unittest 为 442 tests、14 failures、7 skipped：4 项 S5a 因 shallow checkout 缺少 pinned historical revisions/blobs，另 10 项 verification workflow 因 pytest 使用 base interpreter 而缺少 locked venv 工具。Windows 为 442 tests、16 failures、15 errors：5 项 S5a shallow-history、10 项 verification workflow、1 项 bounded path failure；另有 1 项 admission canonical-path error 和 14 项 JS/TS/TSX byte-exact golden errors。两个 hosted job 均失败。
- 本轮修复：CI checkout 显式 `fetch-depth: 0` 并保留 `persist-credentials: false`；contract test 锁住这两个 checkout 参数。Manifest environment 拒绝用例从 historical environment 起步，只篡改被测字段；生产 validator、manifest 与历史 revision/hash/lock SHA 未改。真实 pytest VerificationSpec 使用 `sys.executable`，测试 helper 的进程 timeout 留出 10 秒冷启动窗口，并显式验证 baseline/post/repair 的 JUnit case evidence。bounded-read 与 admission tests 统一 canonical path 比较；admission lock 获取后用 `try/finally` 释放。新增 `.gitattributes` 仅为 JS/JSX/TS/TSX golden fixtures 固定 `text eol=lf`，没有全仓库 renormalize。
- EOL 证据：四个 fixture 的 `git check-attr` 均为 `text: set, eol: lf`；当前 index/worktree 均为 `i/lf w/lf`。
- 验证环境：全新外部 `UV_PROJECT_ENVIRONMENT=%TEMP%\swe-agent-s5d2-repair-733d607-py312`，外部 uv cache `%TEMP%\swe-agent-audit-cache-1eb6563d47564d44902b2f4eed6d1db7`；uv 0.12.1，`uv sync --locked` 通过，Python 3.12.3。首次无网络权限同步因缺少缓存中的 `iniconfig` 失败；授权网络重试完成安装，之后 wheel 专项在同一 external cache 下通过。
- 专项本地结果：S5a manifest 19 tests、S5d.2 contract 2 tests；verification workflow 27 tests；bounded-read 26 tests；admission 18 tests（3 skipped）；JS/JSX/TS/TSX golden 18 tests；S5d.1 + S5d.2 专项 4 tests，均通过。
- 全量 Codex 本地结果：unittest 442 tests OK、10 skipped；pytest 436 passed、10 skipped、25 warnings；compileall、11 prompt render、root/start/resume CLI help、workflow YAML parse、`git diff --check` 均通过。10 个 skip 均为 Windows 当前 symlink 创建权限不足并有明确原因；25 条 pytest warning 来自锁定 Tree-sitter 旧 API FutureWarning。
- Hosted rerun：GitHub Actions run `36391267054`（[run](https://github.com/bbandcc/swe-agent/actions/runs/36391267054)），push event，head SHA `9938d633083ebf0229408e31d07b6b8172eabaad`，terminal `success`。Ubuntu/Python 3.12 job `108827408429` 与 Windows/Python 3.12 job `108827408515` 均 terminal `success`；两边 checkout、locked uv 0.12.1/Python 3.12、`uv sync --locked`、S5d.1 clean-wheel/install、canonical full unittest、full pytest、platform path-safety seams、compileall 均执行成功。checkout 日志确认 `fetch-depth: 0` 与 `persist-credentials: false`。
- Hosted 结果：Ubuntu 使用 Python 3.12.3，unittest 442 tests OK / 7 skipped，pytest 439 passed / 7 skipped / 25 warnings，platform seams 121 tests OK / 7 skipped；Windows 使用 Python 3.12.10，unittest 442 tests OK / 0 skipped，pytest 446 passed / 0 skipped / 25 warnings，platform seams 121 tests OK / 0 skipped。Ubuntu 的 7 项 skip 均为 Windows junction/path-case 专属用例；Windows 没有 skip。两平台 25 条 pytest warning 均为锁定 Tree-sitter 旧 API 的 `Language(path, name)` FutureWarning。
- 四类旧失败均由对应 hosted evidence 覆盖：S5a pinned historical revision/environment tests 通过且 checkout 获取 full history；verification baseline/post/repair JUnit evidence tests 通过；Windows bounded-read/admission canonical-path 专项通过；Windows JS/JSX/TS/TSX byte-exact golden source/hash/range tests 全部通过。
- S5d.2 的 hosted evidence 不外推为真实模型或 benchmark 结果，也不代表 full-project re-audit PASS。该阶段已完成并同步后，下一阶段按 MASTER_PLAN 顺序进入 S5b.1。

## D7/S5b.1 隔离任务执行边界（已完成并同步）

- 状态：技术提交 `6de5d32122093a78508defae282ceec5f7c52ae9`（`feat(S5): 建立隔离任务执行边界`）已通过 ChatGPT S5b.1 Technical Final Gate 与 SYNC_AUDIT 并由用户 push；docs seal `08d902906b38993f0d6a2cda4df399d3918bc193` 已通过 `PROJECT_STATE_FINAL_GATE` 并由用户 push。开工时 HEAD/upstream 已同步、工作树 clean、ahead/behind `0/0`；S5b.1 正式完成。S5b.2 零模型费用 environment/harness preflight 已开始；S5b.1 不表示支持任意 frozen task/environment。
- 两项保证分开实现：版本隔离由独立 pinned revision task workspace 提供；命令隔离由唯一 Windows backend——AppContainer lowbox token + Job Object 提供。task tree 不含 `.git` indirection、hidden oracle 文件和受保护凭据路径；只从 task workspace 收集 patch，并返回 before/after、patch SHA-256 与 size。AppContainer 没有添加网络 capability，使用显式环境白名单，不复制父进程环境；Job Object 施加 CPU/内存限制，另有 wall-time timeout 和进程树终止。
- 目标平台边界：当前只实现 Windows AppContainer；非 Windows 返回 `UNSUPPORTED_PLATFORM`，不回退普通 subprocess。没有改 profile-store ACL，也没有新增依赖。
- 用户提供的预检事实：正常 Windows 用户 `y` 可创建 AppContainer，真实进程 `TokenIsAppContainer=1` 且退出码为 0；`CodexSandboxOnline` 创建返回 `0x80070002`。这只证明可创建 profile/lowbox，不能替代本项目的 host/oracle/secret/network/resource/process-cleanup canary。
- S5b.1 首轮 Codex 本地非集成验证：全新 workspace 外 uv cache/project environment 上 `uv sync --locked` 成功，Python 3.12.3；task workspace / sandbox / task execution 专项 unittest 为 28 tests、0 failures、13 skipped；全量 unittest 为 470 tests、0 failures/errors、23 skipped；全量 pytest 为 451 passed、23 skipped、25 warnings。首次全量 unittest 中 S5d.1 wheel 子进程因未继承可写 cache 失败 1 项；设置为已填充的外部 cache 后，该 wheel 专项单测通过，随后全量 unittest 通过。pytest 的 25 条 warning 是锁定 Tree-sitter API FutureWarning。
- `python -m compileall -q agent tests`、11 prompt 的 UTF-8 / 非仓库 cwd render、root/start/resume CLI help 与 S5d.1 clean-wheel 专项均通过。真实 AppContainer 集成测试需显式设置 `S5B_RUN_APPCONTAINER_TESTS=1`；所有真实测试在 profile/ACL/canary 前统一检查 Windows、opt-in、非提升且非 AppContainer token，`S5B_EXPECTED_TEST_USER` 仅是可选的本机账号 guard，产品契约不固定用户名。当前 Codex 工具进程账号实测为 `laptop-epb7acin\codexsandboxonline`，故没有以该身份创建 AppContainer，也没有把旧 elevated `y` 结果计作本轮证据。
- ChatGPT pre-commit audit 指出的代码/测试缺口已在最终实现中修正：可信 `WorkspaceAccessPolicy` 的 hidden/oracle 与 manifest oracle 合并，scope 冲突在复制/ACL 前拒绝，内容先从 task tree 移除；`prepare_task_workspace` 与 `execute_isolated_task` 都要求显式 policy，`None`/非 policy 在复制或 dispatch 前结构化拒绝。网络测试包含 sandbox curl 和 host listener control；CPU workload 必须启动并到 wall timeout，内存测试包含同 limit 小分配 control 与超限分配；单一可信 CPython 3.12 toolchain 位于 task workspace 外，低权限进程仅获 read/execute ACL，命令使用显式 `python` 选择符且不做 PATH lookup。受保护原始内容不进入 task copy；如果任务在可写目录中新建受保护名称，patch collection 会拒绝，但本项目不声称 OS 按路径名阻止该创建。
- 本次 policy 必填修复后的本地回归：全量 unittest `485 tests OK / 25 skipped`；全量 pytest `464 passed / 25 skipped / 25 warnings`；workspace / execution / sandbox 专项 unittest `43 tests OK / 15 skipped`；policy seam 四项新增回归通过；S5d.1 clean-wheel 专项在 workspace 外可写 `UV_CACHE_DIR` 下通过（291 秒）；`compileall -q agent tests` 通过。首次全量重跑因 uv 默认 cache 无权限失败的 wheel 用例，改用外部临时 cache 后单项和全量均通过。25 个 skip 含需普通用户显式 opt-in 的 AppContainer canary 与原有平台/能力 skip；这些本地结果不替代真实 AppContainer、GitHub hosted 或 benchmark 证据。
- canary #3 是 runtime-root safety 修复前的历史真实证据：30 tests、29 passed、1 expected skip、55.521s；覆盖 host/oracle、hidden path、secret env、controlled network、资源/期限、进程树、bounded output、Python RX、patch 与宿主 baseline。之后发现 runtime-root safety blocker，因此 #3 不代表最终代码；最终代码由 canary #4 覆盖。
- 用户在普通非提升 Windows token 下运行真实 AppContainer canary #1：29 tests、2 failures、1 skipped。两项失败分别为 CPU `.cmd` workload `PROCESS_FAILED`（拒绝访问）和 hidden-path command 条件解析导致未产生 patch；两者已修复为 inline CPU workload 与明确的嵌套 `if/else`，并通过普通 `cmd.exe` 控制验证。
- 用户运行真实 AppContainer canary #2：30 tests、1 failure、1 skipped。CPU/memory Job 限额 canary 已真实通过；hidden-path 的 AppContainer 隔离、授权文件修改、patch evidence 及其它断言均通过，唯一失败是宿主 baseline 断言硬编码 LF，而 Windows 宿主工作树实际 bytes 为 CRLF。生产 checkout 明确使用 LF，因此该断言混淆了 host 工作树与 pinned task snapshot 两种 EOL 契约。本轮仅将该断言改为 execution 前后逐字节对比，并同时核对 private secret bytes、repository HEAD 与 clean status；不修改 production sandbox / checkout / patch collector。
- baseline assertion 修复后重新运行：`tests.evaluation.test_task_execution` 为 15 tests OK / 4 skipped；全量 unittest 为 486 tests OK / 25 skipped；全量 pytest 为 465 passed / 25 skipped / 25 warnings；`compileall -q agent tests` 与 `git diff --check` 通过。真实 AppContainer 测试在本地仍因未启用普通用户 opt-in 而 skip，不替代 canary。
- ChatGPT `Codex with ChatGPT · 简历项目SWE` connector 的源码审计报告：canary #3 在本轮 runtime-root 修复前真实通过（30 tests，29 passed、1 expected skip，55.521 秒），覆盖普通非提升 token、AppContainer token、host/oracle denial、hidden-path isolation、secret env isolation、controlled network denial、CPU/memory/wall-time、descendant cleanup、bounded output、Python toolchain RX、pinned task patch，以及宿主 baseline bytes/HEAD/Git status 不变。该证据保留为历史结果，不替代对本轮修复后代码的复跑。
- 本轮修复 runtime-root safety blocker：`prepare_task_workspace` 直接校验用户提供的 runtime root，创建目录前后均调用现有 `canonicalize_root_path(..., must_exist=False)`；不先 resolve 掩盖 root symlink/junction。repository/runtime 使用与 RunConfig 共用的 `canonical_roots_overlap` 规则，equal、runtime 在 repository 内、repository 在 runtime 内均在 task checkout/sandbox 前结构化拒绝。root symlink/junction 拒绝，ancestor link/junction 按既有 canonicalization 规则允许。新增测试覆盖这些路径和拒绝前无 task workspace/dispatch。
- `execute_isolated_task` 顶层 failure-path 公开 seam 覆盖 sandbox cleanup 不完整、task workspace cleanup 失败、以及 out-of-scope patch：均返回 FAILED 与对应 `CLEANUP_FAILED` / `PATCH_COLLECTION_FAILED`，断言 cleanup 状态，并确认失败时不发布成功 patch。相关用例位于 task workspace 测试；真实 canary 仍为 30 个 opt-in 测试。
- 本轮最终本地验证使用外部 uv cache `%TEMP%\swe-s5b-uv-cache-20260929` 与外部 Python 3.12 环境；S5d.1 clean-wheel 专项 `tests.test_s5d1_installation` 2 tests OK。workspace/task execution/Windows sandbox/RunConfig/access policy/admission/workspace path 专项 104 tests OK / 23 skipped；Windows junction root rejection 与 ancestor junction canonicalization 实际通过。3 项普通目录 symlink 用例因 WinError 1314 权限能力不足 skip，junction 用例覆盖对应 root/ancestor link 语义。
- 本轮全量 unittest：495 tests OK / 27 skipped；全量 pytest：472 passed / 27 skipped / 25 warnings。25 条 warnings 为锁定 Tree-sitter API 的 FutureWarning。`python -m compileall -q agent tests`、11 prompt UTF-8/non-repository-cwd render、root/start/resume CLI help、S5d.1 clean-wheel、`git diff --check` 均通过。结果为 Codex 本地证据，不代表 GitHub Actions、真实模型或 benchmark 验收。
- 最终真实 AppContainer canary #4 由普通、非提升 Windows 用户 `y` 在 PowerShell 7 对 runtime-root safety 修复后的最终代码运行：`Ran 30 tests in 65.033s`，29 passed、1 expected skip、0 failures、0 errors。唯一 skip 为 Windows 上预期跳过的 `UnsupportedSandboxPlatformTests.test_non_windows_backend_fails_closed`。该真实证据覆盖 non-elevated token preflight、`TokenIsAppContainer`、task workspace 读写、host/oracle 读写拒绝、trusted hidden path、secret 环境剥离及 allowlist 非继承、受控 loopback 禁网、CPU hard cap、memory cap、wall timeout、后代进程清理、有界 stdout/stderr、profile/private scratch cleanup、cleanup failure 不报成功、sibling baseline 不可访问/修改、可信 CPython 3.12 执行及 RX 边界、pinned task patch、host baseline bytes/HEAD/Git status 不变。该 canary 不证明任意 frozen task/environment 均可执行。
- 最终 runtime-root contract：使用原始 runtime root 在 mkdir 前后调用 `canonicalize_root_path(..., must_exist=False)`；root symlink/junction fail closed，ancestor link/junction 保持现有 canonicalization 语义；repository/runtime 相等或任一方向包含均拒绝，且拒绝发生于 checkout/sandbox dispatch 前。failure-path contract：sandbox cleanup incomplete 与 task workspace cleanup failure 均为 `FAILED / CLEANUP_FAILED`；invalid/out-of-scope patch 为 `FAILED / PATCH_COLLECTION_FAILED`；不把 exit 0、NOOP 或 cleanup failure 冒充成功。
- S5b.1 implementation、确定性回归和最终 canary #4 evidence 已通过 Technical Final Gate 并同步。Windows AppContainer 是唯一 backend；非 Windows 为 `UNSUPPORTED_PLATFORM`，不回退普通 subprocess。没有真实模型或 benchmark evidence。S5b.2 零模型费用 preflight 已确认首个 ENVIRONMENT blocker；首个技术 slice 的 binary-only preparation boundary 已完成、通过 Technical Final Gate 并同步，真实 historical environment 仍被 binary-wheel 缺失阻塞；S4d 不提前于 S5b.2。

## D12/S5b.2：零模型费用 environment/harness preflight（已完成，首个 blocker 已确认）

- 固定基线：`08d902906b38993f0d6a2cda4df399d3918bc193`；开工已核验 branch `review/step3-runtime-recovery`、upstream `origin/review/step3-runtime-recovery`、HEAD/upstream 一致、工作树 clean、ahead/behind `0/0`。
- S5b.1 Technical Final Gate、PROJECT_STATE_FINAL_GATE 与 sync audit 均已通过，技术实现和文档 seal 已同步。
- 本 preflight 只检查 S5a frozen task 的环境与 harness 可执行性，不调用模型；真实模型调用、费用和 trial 均为 0。当时未修改 production code，也未进入 S4d。
- 重新读取的 manifest canonical SHA-256 为 `7274b2769b39a242c0b8a4005e86bf92b842532c2f80117eb3ffe57df67b9253`。四个任务共同固定 environment：Windows / AMD64 / Python 3.12 / `uv.lock` SHA-256 `c270b21033292b4c578ca30cbe06a79718a33f272d2a05570aaaa31f57dca04f`；budget 均为 max_steps=100、max_cost_usd=2.0、deadline=1800 秒、`provisional_unmeasured`。所有 check 均为 `shell=false`、`cwd="."`、timeout=180 秒；下列 argv 为 manifest 中的逐项命令。oracle 文件路径取 pinned manifest，hash 仍由 validator 按对应 oracle revision 校验。
- 冻结任务矩阵：
  - `symbols-python-exact-source-v1`（train / `language:python`）
    - target `7ed6bc9ee41b0f5586c0da4c980d790afb59f945`；scope：`agent/tools/codemap.py` modify、`agent/tools/python_symbols.py` create。
    - oracle revision `b8fcb68e123fd19d4542fbf48155240926405d33`；files：`tests/tools/test_python_symbols.py`、`tests/tools/test_workspace_boundary.py`。
    - baseline `python-workspace-baseline` / tests revision `7ed6bc9ee41b0f5586c0da4c980d790afb59f945` / argv `python -m unittest tests.tools.test_workspace_boundary`；target `python-target-oracle` / tests revision `b8fcb68e123fd19d4542fbf48155240926405d33` / argv `python -m unittest tests.tools.test_python_symbols`；regression `python-language-regressions` / tests revision `b8fcb68e123fd19d4542fbf48155240926405d33` / argv `python -m unittest tests.tools.test_python_symbols tests.tools.test_workspace_boundary`。
  - `symbols-javascript-exact-source-v1`（train / `language:javascript`）
    - target `b8fcb68e123fd19d4542fbf48155240926405d33`；scope：`agent/tools/codemap.py` modify、`agent/tools/python_symbols.py` modify、`agent/tools/javascript_symbols.py` create、`agent/tools/symbol_contract.py` create。
    - oracle revision `c25094f5b383c625994c59ca49fd77018115eb07`；files：`tests/tools/test_javascript_symbols.py`、`tests/tools/test_python_symbols.py`、`tests/tools/fixtures/javascript_symbols.js`、`tests/tools/fixtures/javascript_symbols.jsx`。
    - baseline `javascript-workspace-baseline` / tests revision `b8fcb68e123fd19d4542fbf48155240926405d33` / argv `python -m unittest tests.tools.test_workspace_boundary`；target `javascript-target-oracle` / tests revision `c25094f5b383c625994c59ca49fd77018115eb07` / argv `python -m unittest tests.tools.test_javascript_symbols`；regression `javascript-language-regressions` / tests revision `c25094f5b383c625994c59ca49fd77018115eb07` / argv `python -m unittest tests.tools.test_python_symbols`。
  - `symbols-typescript-exact-source-v1`（dev / `language:typescript`）
    - target `c25094f5b383c625994c59ca49fd77018115eb07`；scope：`agent/tools/codemap.py` modify、`agent/tools/typescript_symbols.py` create。
    - oracle revision `d86d1c8ecb08ba2e146518f1d0de784a7f01b095`；files：`tests/tools/test_typescript_symbols.py`、`tests/tools/test_javascript_symbols.py`、`tests/tools/test_python_symbols.py`、`tests/tools/fixtures/typescript_symbols.ts`、`tests/tools/fixtures/javascript_symbols.js`、`tests/tools/fixtures/javascript_symbols.jsx`。
    - baseline `typescript-workspace-baseline` / tests revision `c25094f5b383c625994c59ca49fd77018115eb07` / argv `python -m unittest tests.tools.test_workspace_boundary`；target `typescript-target-oracle` / tests revision `d86d1c8ecb08ba2e146518f1d0de784a7f01b095` / argv `python -m unittest tests.tools.test_typescript_symbols`；regression `typescript-language-regressions` / tests revision `d86d1c8ecb08ba2e146518f1d0de784a7f01b095` / argv `python -m unittest tests.tools.test_python_symbols tests.tools.test_javascript_symbols`。
  - `symbols-tsx-exact-source-v1`（dev / `language:tsx`）
    - target `c8b0840812a7b2f1d7d56450bdea908d623a7606`；scope：`agent/tools/codemap.py` modify、`agent/tools/tsx_symbols.py` create、`agent/tools/typescript_symbols.py` modify。
    - oracle revision `3ebefa53da38152381a11bfe8d526cbb3bbe60c8`；files：`tests/tools/test_tsx_symbols.py`、`tests/tools/test_typescript_symbols.py`、`tests/tools/test_javascript_symbols.py`、`tests/tools/test_python_symbols.py`、`tests/tools/fixtures/tsx_symbols.tsx`、`tests/tools/fixtures/typescript_symbols.ts`、`tests/tools/fixtures/javascript_symbols.js`、`tests/tools/fixtures/javascript_symbols.jsx`。
    - baseline `tsx-workspace-baseline` / tests revision `c8b0840812a7b2f1d7d56450bdea908d623a7606` / argv `python -m unittest tests.tools.test_workspace_boundary`；target `tsx-target-oracle` / tests revision `3ebefa53da38152381a11bfe8d526cbb3bbe60c8` / argv `python -m unittest tests.tools.test_tsx_symbols`；regression `tsx-language-regressions` / tests revision `3ebefa53da38152381a11bfe8d526cbb3bbe60c8` / argv `python -m unittest tests.tools.test_python_symbols tests.tools.test_javascript_symbols tests.tools.test_typescript_symbols`。
- Manifest / pinned identity 的公开 validator 结果：以 manifest 固定的 historical environment 为 `expected_environment`，四项 source-backed task 全部 `valid=true`、无 issue；逐一读取四个 target revision 的 `uv.lock` Git blob，均为 269,403 bytes 且 SHA-256 与 manifest 的 `c270b210...` 相同。当前 checkout environment 则不匹配该 identity：Python 为 3.12.3，但当前 `uv.lock` SHA-256 为 `740a35a78d5cc4d1f09dfa3378bff572c199197b93d0653de5d8307a15fe0035`；将当前 environment 显式交给同一 validator 时四项均以 `ENVIRONMENT_MISMATCH` 拒绝。历史锁与当前锁不同本身不是 manifest 错误；它证明不能直接复用当前 checkout 环境。实际 blocker 是当前 execution seam 没有 task-pinned frozen environment，且现有 interpreter staging 排除 `site-packages`，无法提供该锁要求的依赖；首个阻塞分类为 **ENVIRONMENT**（依赖缺失细分为 **TOOL_DEPENDENCY**），不是 manifest provenance 错误。
- 公开 workspace seam 实际准备 `symbols-tsx-exact-source-v1` 成功：target pin 正确、`.git` 不在 task tree、allowed scope 契约满足（modify paths 在 pinned baseline 中存在，create paths 不存在）、oracle 中列出的测试与 fixture 均不在 task workspace；空变更 `collect_patch` 有效且 cleanup 成功。这只证明 pinned task copy / oracle 排除路径可准备，没有 dispatch 命令或启动 AppContainer。
- 首个 blocker 出现后没有继续命令执行。源码核对的后续执行约束：目标和 symbol tests 需要 `langchain-core`、`tree-sitter==0.21.3`、`tree-sitter-languages==1.10.2`；当前 S5b.1 staged CPython 明确不复制 `Lib/site-packages`，sandbox 用 `-B -S`，故 frozen dependencies 尚未随 task 环境提供（**TOOL_DEPENDENCY**）。baseline `test_workspace_boundary` 还会从 Python 子进程调用 `pwsh.exe` 创建 link/junction；当前 sandbox 的显式 PATH 只含 System32，executable 校验只接受 task workspace、System32 或受控 Python toolchain，因此该子进程需求与当前允许范围不相容（**SANDBOX_PLATFORM**，源码确认，未在 sandbox 实跑）。symbol tests 使用 `tempfile.TemporaryDirectory()`；sandbox 将 TEMP/TMP 指向 AppContainer 私有 `AC/Temp`，但本轮未实际验证这些测试在该 temp 行为下是否通过。没有假定过去的 tempfile 探索结果。
- oracle 内容在 model task tree 之外受到保护；但当前 `execute_isolated_task` seam 不接 evaluator file material，也没有独立 trusted evaluator materialization/execution seam。Manifest 的 `evaluator_only_declared` 是配置声明，不是可运行证据；因此 hidden tests 不能由当前 S5b.1 seam 安全供 evaluator 使用（**ORACLE_MATERIALIZATION / HARNESS**，源码确认）。没有改动、不复制或运行 oracle 内容。
- 分类结论：首个 trial blocker 为 **ENVIRONMENT**；继续执行还需解决 **TOOL_DEPENDENCY、SANDBOX_PLATFORM、ORACLE_MATERIALIZATION/HARNESS**。**MANIFEST** 未发现 blocker：四项 pinned lock、target/oracle revisions、scope 与 check argv 均通过 source-backed validator；四个历史任务保持 2 train / 2 dev，未伪造新任务或更改 provenance。
- 本轮没有启动 manifest checks、真实 AppContainer command、模型或 benchmark；真实模型调用=0、费用=0、trial=0。静态 tempfile 依赖已确认，AppContainer 下的实际 tempfile 可用性仍待执行验证。此前回滚的 historical-lock/evaluator/tempfile 探索未用作本轮实现事实；未运行 `test_sandbox_tempfile_acl_diagnostic`。
- 当时建议的下一技术 slice 限于 task-pinned Python 环境：建立 CPython 3.12 / dependency provision + staging/execution seam，使 manifest historical lock 对应依赖可在现有 AppContainer 边界内只读使用；不解决 `pwsh.exe`、evaluator/oracle materialization 或 tempfile compatibility。此建议已由下方当前 slice 执行。
- preflight 结论：首个 `ENVIRONMENT` blocker 已确认，继续执行还需处理 `TOOL_DEPENDENCY`、`SANDBOX_PLATFORM`、`ORACLE_MATERIALIZATION/HARNESS`。本节为 preflight 历史结果；环境 slice 仍未因此整体完成。

## D12/S5b.2 首个技术 slice：task-pinned Python 环境（binary-only 边界已完成并同步，historical environment BLOCKED）

- 基线：固定 HEAD `f040955479e0ead0d0ab0e8e8fb58984a4bd00d2`；开工时 branch/upstream 为 `review/step3-runtime-recovery` / `origin/review/step3-runtime-recovery`，local/upstream HEAD 相同、工作树 clean、ahead/behind `0/0`。本 slice 已提交为 `c471b3b0145d9ce30fec3f6675cb0f4d38b8d9aa`（`feat(S5): 封闭冻结依赖准备边界`）、通过 ChatGPT Technical Final Gate，并已同步 upstream。
- 范围仅为首个 `ENVIRONMENT` blocker：使用现有 S5a pinned task workspace、CPython 3.12 与唯一 Windows AppContainer backend，让一个真实 task 的 historical lock 依赖经过准备、受限 staging 后可由现有隔离执行 seam 使用。没有运行 frozen oracle/check、模型或 benchmark；真实模型调用、费用、trial 均为 0。没有进入 S4d。
- 根因与 identity：四个 S5a task 的 historical `uv.lock` SHA-256 为 `c270b21033292b4c578ca30cbe06a79718a33f272d2a05570aaaa31f57dca04f`，当前 checkout lock 不同（preflight 时为 `740a35a78d5cc4d1f09dfa3378bff572c199197b93d0653de5d8307a15fe0035`）。现有 Python staging 原先排除 `Lib/site-packages`，无法提供 manifest 锁定依赖。
- 最小 seam：新增 `stage_frozen_python_312`，仅从已准备的 task target snapshot 读取 canonical lock path，验证 target revision、Windows/AMD64/Python 3.12 identity 及 manifest SHA-256；无 current checkout lock fallback。运行时只接受经 `--version` 确认的 uv `0.12.1`；uv 0.12.1 help 实际确认支持 `--no-build`，因此 binary-only 安装使用 `uv sync --locked --no-build --no-install-project --no-dev --link-mode copy`，禁用配置/keyring 与 Python 下载。uv 临时 project/cache/profile 清理后，才将 bounded dependency tree 复制进 staged CPython `Lib/site-packages`。历史锁/hash、当前锁不回退、20,000 files / 256 MiB / 20,000 entries staging cap、symlink/junction/hardlink 拒绝、显式 PYTHONPATH 和既有 AppContainer RX ACL 保持。
- TDD：首轮 red 8 tests 中 7 项因缺少 frozen dependency staging/execution 能力失败，1 项 symlink 因 WinError 1314 skip。后续回归新增 wrong uv version、wrong executable architecture、binary-only no-fallback 与 frozen temp cleanup failure；cleanup 失败在顶层映射为 `FAILED / CLEANUP_FAILED`、`cleanup_complete=False`，不 dispatch、不发布 patch。解释器 probe 从传入 executable 本身确认 CPython、3.12、64-bit、AMD64（含 `win-amd64` build target），并和 manifest architecture 对比。
- 当前 targeted evaluation regression：仓库 Python 3.12.3 `.venv` 执行 `python -m unittest discover -s tests/evaluation -v`，90 tests、0 failures/errors、19 skipped。skip 为需普通非提升用户 opt-in 的真实 AppContainer canary、普通 symlink 权限能力（WinError 1314），以及 Windows 下不适用的 non-Windows backend contract。
- 本次最终回归同样使用仓库 Python 3.12.3 `.venv`；`UV_CACHE_DIR` 指向 workspace 外可写临时 cache。全量 unittest：513 tests，0 failures/errors，29 skipped，564.492s；全量 pytest：488 passed、29 skipped、25 warnings，514.09s。warning 全为 Tree-sitter `Language(path, name)` FutureWarning。`python -m compileall -q agent tests` 通过；`git diff --check` 在本轮 task_plan 更新后复核。
- 当前 no-build 真实准备证据：Python 3.12.3、uv 0.12.1；公开 task workspace 取得 `symbols-python-exact-source-v1` target `7ed6bc9ee41b0f5586c0da4c980d790afb59f945` 和 manifest pinned lock `c270b210...`。`uv sync --locked --no-build` 返回 exit 2：`fuzzysearch==0.7.3` 被标记为 no-build 且没有 binary distribution。错误在 sandbox dispatch 前结构化为 `FROZEN_ENVIRONMENT_INVALID`，task workspace cleanup 成功；未运行 AppContainer command、import canary 或 oracle。此前 host source-build 允许时曾临时 stage 出 6,966 个 site-packages 条目，但该试验执行了被禁止的 host source build，现明确视为无效/已废弃证据，不证明当前 slice 可准备 frozen environment。
- 当前不提供/不运行真实 AppContainer canary：pinned lock 在 binary-only gate 已无法完成 staging；旧 canary 不可作为通过证据。source dependency 不可读、staged dependency RX 与隔离 import 仍未验证。确定性 blocker 是 `ENVIRONMENT / TOOL_DEPENDENCY`：该历史 lock 的 `fuzzysearch==0.7.3` 无可用 binary distribution，而 host build backend 被 D7 隔离边界禁止。本轮不实现 build sandbox，也不 fallback。
- `pwsh.exe`、evaluator/oracle materialization、tempfile compatibility 仍未解决，本 slice 不覆盖。没有改 manifest、oracle/revisions、`pyproject.toml`、当前 `uv.lock`、S3.5b policy、模型 workflow 或 `MASTER_PLAN.md`；没有模型调用、费用或 trial。
- 当前状态：binary-only preparation boundary 已实现；historical lock 因 `fuzzysearch==0.7.3` 无 binary wheel 而无法准备，S5b.2 未完成。隔离 source-build feasibility preflight 的结论和证据见下节；未实现 build sandbox、未运行真实 AppContainer canary、未执行模型或 trial。非 Windows backend 仍 unsupported；不承诺 frozen oracle 可运行、真实模型效果或 benchmark 结果。

## D12/S5b.2 隔离 source-build feasibility preflight（静态证据已核实，隔离构建尚未确认）

- 基线：`c471b3b0145d9ce30fec3f6675cb0f4d38b8d9aa` 已提交、通过 ChatGPT Technical Final Gate 并同步 upstream；binary-only frozen dependency preparation boundary 已完成。该次 preflight 的文档修改随后纳入证据提交 `89a4e9038a352ebe930d4e3b8412e7cb0bad88fb`；本轮开工 local/remote-tracking HEAD 均为该证据提交、ahead/behind `0/0`、工作树 clean。S5b.2 真实 historical environment 仍被 `fuzzysearch==0.7.3` 无适用 binary wheel 阻塞；本轮不是 production repair。
- pinned identity：target `7ed6bc9ee41b0f5586c0da4c980d790afb59f945` 的历史 `uv.lock` 为 269,403 bytes，SHA-256 `c270b21033292b4c578ca30cbe06a79718a33f272d2a05570aaaa31f57dca04f`。`fuzzysearch==0.7.3` 的 source URL 为 [PyPI 官方固定归档](https://files.pythonhosted.org/packages/f7/28/3e9e4e55fd35356f331a22976694e151eb0214b68d3cd471936f9c09deba/fuzzysearch-0.7.3.tar.gz)；实际取得 112,677 bytes，SHA-256 `d5a1b114ceee50a5e181b2fe1ac1b4371ac8db92142770a48fed49ecbc37ca4c`，均与 historical lock 完全一致。归档保留于 workspace 外 `%TEMP%\s5b2-sdist-evidence-vlta5560\fuzzysearch-0.7.3.tar.gz`，不是仓库交付文件。
- 获取历史与边界：先有界扫描既有 uv/pip/相关临时 cache，100,000 entries 预算内没有准确 size/hash 的原始归档；扫描触顶，不宣称所有 cache 均已穷尽。已有解包/build cache 不替代 pinned archive 校验。先前 Windows Schannel 下载失败 `SEC_E_NO_CREDENTIALS (0x8009030E)` 属于 `PINNED_SDIST_FETCH_FAILED`；本轮使用标准库 HTTPS/default TLS 验证从同一官方 URL 有界读取，成功解除此 fetch blocker，未关闭 TLS、未用镜像或执行下载内容。
- 只读静态 recipe：hash/size 校验后才用标准库 tar reader 检查归档；共 39 entries，metadata 29,938 bytes，检查设定上限为 512 entries、单文件 64 KiB、metadata 合计 256 KiB，不解包或执行。无 `pyproject.toml`；`setup.cfg` 仅 egg-info 配置；`setup.py` 导入 `setuptools.setup/Extension` 和 `distutils.build_ext/errors`，读取 README/HISTORY，声明四个 C extensions。默认尝试 C 编译，捕获指定 `BuildFailed` 后调用纯 Python `run_setup(False)`；显式 `--noexts` 则不尝试 extensions。未发现 `setup_requires` 或 Cython import/构建要求；`install_requires=['attrs>=19.3']` 是 runtime requirement，不能当成 build requirement。项目描述中的 Cython 优化不等于此归档需要 Cython 构建。
- requirements 的确定与未知：静态可确认 setuptools 和其 distutils 接口需求；wheel packaging 需要可信 wheel-generation tooling，但该 legacy sdist 没有固定 build-tool 版本或声明完整 `[build-system]`。`--noexts` 仅为源码确认的候选纯 Python 路径，不是实际构建成功证据。未调用 backend hooks/`setup.py`，动态依赖返回值、工具版本兼容性、CPython 3.12 下行为及最终 wheel 内容仍未知。setuptools 上传客户端版本 47.3.1 不作为 build dependency pin。官方 [distutils 文档](https://setuptools.pypa.io/en/stable/deprecated/distutils-legacy.html) 说明 setuptools 60 起自带 distutils；该说明不是本项目实际 build 验收。
- prebuilt dependency evidence：仅从官方 PyPI 查询并有界下载候选 `setuptools==75.8.0` 与 `wheel==0.45.1` 的 `py3-none-any` wheel，未安装/执行。setuptools wheel 1,228,782 bytes、SHA-256 `e3982f444617239225d675215d51f6ba05f845d4eec313da4418fdbb56fb27e3`；wheel wheel 72,494 bytes、SHA-256 `708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248`，均与官方 metadata 一致。ZIP 只读文件名检查确认 setuptools 包含 `build_meta.py`、vendored distutils `build_ext/errors` 和 `bdist_wheel.py`。官方 Requires-Python 分别为 `>=3.9`、`>=3.8`，无不带 extra 的 Requires-Dist；这些仅是候选 bootstrap wheel evidence，不改 historical lock、不宣称已选定或验证最终 build recipe。历史 runtime dependency `attrs==25.1.0` 在 pinned lock 中有 `py3-none-any` wheel，SHA-256 `c75a69e28a550a7e93789579c22aa26b0f5b83b75dc4e08fe092980051e1090a`、63,152 bytes，与官方 metadata 一致；本轮没有安装/import。
- blocker 分类：`PINNED_SDIST_FETCH_FAILED` 已解除；`SOURCE_BUILD_RECIPE_UNVERIFIED` 的静态归档内容缺口已解除，但动态 hook/build compatibility 仍未验证。当前首个未解决门槛是隔离构建可行性尚未确认：现有唯一 AppContainer backend 有可信 argv、禁网、Job Object CPU/memory/time、有界输出、进程树清理与 RX toolchain 能力；当前 production seam 尚无受校验 source/build-tool 输入 staging、wheel hash/size 结果契约，也没有真实隔离 build evidence。不能将静态候选路径写成整体 FEASIBLE，historical environment 继续 BLOCKED。
- 下一最小动作仅供审核：先评估复用现有 AppContainer、在 disposable workspace 中使用固定且 hash-verified 的 source/bootstrap wheels，禁网并保持 host/oracle/secret 不可访问，验证纯 Python wheel 候选路径、输出 hash/size 与 cleanup fail closed。任何 source/backend/hook 执行只能在隔离边界内；host `--no-build` 不变。本轮没有实现 build sandbox，未处理 pwsh、evaluator/oracle materialization、tempfile 或 S4d。
- 证据可读性修复（2026-10-08）：连接器仅允许 workspace 文本读取，不能直接读取 `%TEMP%` 二进制 sdist；不扩大 host 读取权限。新增 `research/audit_fuzzysearch_sdist.py`，hash/size 验证先于 tar 解析，并生成 `research/s5b2-fuzzysearch-sdist-evidence.md`。报告提供 39 项 inventory、setup.py/setup.cfg、PKG-INFO、README/HISTORY、runtime requirements 与 LICENSE 原文及逐项 hash/size，供 `read_file` 分页复核；本次选择的 metadata 集合与前次 29,938 bytes 统计集合不同，不替代归档整体 hash。
- 实际验证：新工具失败用例先红（模块尚不存在），最小实现后 unittest 2 tests / 3 invalid-byte subcases 全绿；覆盖错误大小、同大小错误 hash、超限输入和缺失归档，输入 bytes 保持不变。pinned archive 静态核验通过；compileall 与 `git diff --check` 通过。通过 `c2c record` 发布本轮真实静态核验和 unittest 输出，task=`c2c_sdist_evidence`、iteration=1；连接器 `execution_output` 已 list/read 验证 readable、exitCode=0、未截断。不是历史 build 日志，未重建或伪造旧命令记录。报告 `read_file` 也已实测可读，超过默认行数时必须继续分页。
- 最终证据记录为 `execution_output` id=`4`（task=`c2c_sdist_evidence` / iteration=1），取代先前报告版本记录；exitCode=0、readable、未截断，已实际 list/read。报告 SHA-256 为 `238c6c05463aa5d56a326599df73a280252472511a67c6b959af561d5ee4261f`；展示文本仅规范化行尾空白/换行，metadata hash/size 仍绑定归档原始 bytes，不可对展示副本计算 hash 来替代原始 bytes 验证。staged diff 检查暴露的 upstream metadata 行尾空白已通过显式展示规范化处理，最终检查通过。
- 证据交付提交为 `89a4e9038a352ebe930d4e3b8412e7cb0bad88fb`（`docs(S5): 发布可读取的固定归档审核证据`），已同步 `origin/review/step3-runtime-recovery`。该提交限于证据工具、其测试、生成报告和本状态文件；保留已有相关 preflight 状态修改，形成 Git checkpoint。没有 production/dependency/manifest/oracle 或 MASTER_PLAN 改动；source/build execution=0、AppContainer commands=0、model calls=0、cost=0、trials=0。S5b.2 historical environment 仍 BLOCKED；下一步仍等待 ChatGPT 审核隔离 source-build feasibility，不自动推进阶段。只读 metadata/download 成功不外推为 task/environment 可运行。

## D12/S5b.2 最小隔离 source-build 执行契约预检（READY_TO_IMPLEMENT，仅构建切片）

- 审核状态：ChatGPT `S5b.2 ISOLATED_BUILD_CONTRACT_AUDIT: PASS`。本 checkpoint 仅固定已审核文档契约，不开始 production 实现；`READY_TO_IMPLEMENT` 仅适用于最小构建切片，historical environment 和 S5b.2 仍 BLOCKED。
- Git 开工快照：workspace `D:\文档\ChatGPT\简历项目`；branch `review/step3-runtime-recovery`；完整 HEAD / remote-tracking HEAD 均为 `89a4e9038a352ebe930d4e3b8412e7cb0bad88fb`，upstream `origin/review/step3-runtime-recovery`，ahead/behind `0/0`、工作树 clean。本轮仅修改状态文件，修改后不再声称工作树 clean；不 commit/push。
- 证据复核：已阅读 AGENTS、MASTER_PLAN D7/D12、§5.3–5.4/§6、当前状态及四个 production 模块。`research/s5b2-fuzzysearch-sdist-evidence.md` 实际 SHA-256 为 `238c6c05463aa5d56a326599df73a280252472511a67c6b959af561d5ee4261f`；只读工具先验证 pinned archive hash/size，再检查 metadata，不解包或执行。该 committed report 检查 39 entries、27,409 metadata bytes（与此前采用另一组 metadata 的 29,938 bytes 记录区分）。实际读取 `execution_output id=4`：exitCode=0、truncated=false，记录 pinned archive 校验和 2 个 static-evidence tests 成功；不是 build 日志，也不证明候选工具兼容。
- 固定输入来源：sdist `fuzzysearch==0.7.3` 使用上节官方 files.pythonhosted.org URL、112,677 bytes、SHA-256 `d5a1b114ceee50a5e181b2fe1ac1b4371ac8db92142770a48fed49ecbc37ca4c`。候选 bootstrap 固定为 `setuptools==75.8.0`（1,228,782 bytes、SHA-256 `e3982f444617239225d675215d51f6ba05f845d4eec313da4418fdbb56fb27e3`；[官方 wheel](https://files.pythonhosted.org/packages/69/8a/b9dc7678803429e4a3bc9ba462fa3dd9066824d3c607490235c6a796be5a/setuptools-75.8.0-py3-none-any.whl)）和 `wheel==0.45.1`（72,494 bytes、SHA-256 `708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248`；[官方 wheel](https://files.pythonhosted.org/packages/0b/2c/87f3254fd8ffd29e4c02732eee68a83a1d3c346ae39bc6822dcbcb697f2b/wheel-0.45.1-py3-none-any.whl)）。这些版本是拟验收的可信构建输入，不是 historical lock 中已验证的 build 环境；可下载/hash verified 不等于可构建。
- 输入 staging 契约：host 只做有界下载/hash、归档结构检查和逐文件数据复制，不 import/install/执行 sdist、setup.py、backend、hooks 或 wheel 内代码。对输入 root/文件及所有子项拒绝 symlink/junction/hardlink、非 regular 文件；tar/ZIP 拒绝 absolute/drive/`..`/escape、重复或 Windows case-collision 名称、链接/设备条目、超限 entry/单文件/累计解压 bytes、压缩炸弹。不能使用无检查 extractall。同一固定上限约束实际遍历及复制，不能先无限 materialize 后再判断。输入校验和完整 staging 均在 profile/ACL/dispatch 前完成；staging 后按新副本 bytes 再核对。工具 ZIP 的 `.pth` 不在 host 被处理，toolchain 不得包含来自父进程的 package/config。
- 唯一执行路径：新建 owned disposable build root（不是 host repo、oracle、用户工作区），与 `toolchain-*` sibling 共同位于已 canonical/root-safe 验证的 owned runtime area；复用 `stage_python_312` 和 `run_in_windows_appcontainer(SandboxCommand)`，不新增 backend。实际传入 Python executable 必须自证 CPython 3.12 / 64-bit AMD64；bootstrap package bytes 进入 sibling `Lib/site-packages`，按现有 AppContainer RX ACL 只读执行，原 CPython/下载/cache source 不授权。工作目录为 build root 内 `source/fuzzysearch-0.7.3`，仅该 disposable root 可写。绝不使用 host cwd/PATH/PYTHONPATH 作为隐式运行依赖。
- 候选固定命令：staged Python 绝对路径 + `-B -S setup.py --noexts bdist_wheel --dist-dir <owned build-root/out>`；argv/cwd 由可信 recipe 生成，不由 task/model 更改。`-S` 不处理 site/.pth，现有 backend 的显式 `PYTHONHOME/PYTHONPATH` 指向只读 sibling；setup.py 先 import setuptools，再 import distutils 的静态顺序已确认。此命令本轮没有执行，bootstrap compatibility/纯 Python wheel 生成仍 experiment-pending；若需要未声明 dependency、compiler 或网络则失败，不 fallback host 或自动扩 recipe。
- 启动/隔离契约：真实验收在 Windows 普通非提升、非 AppContainer token 下显式 opt-in；username 只可作为可选 test guard。preflight 失败必须在 profile/ACL 前拒绝。沿唯一 backend 保留 capability=0、explicit env、不继承 secrets/proxy/PYTHONPATH、Job Object CPU rate/time 与 memory、wall timeout、有界 stdout/stderr、先 suspended create/assign job/token 验证再 resume，以及后代进程终止。build inputs 不含 repo/oracle；不修改 profile-store ACL，不处理现有 tempfile observations。
- 输出门槛：Job 已终止/后代已清理后才检查 owned `out`；exit 0 本身不是 build success。仅接收恰好一个唯一 regular/no-link wheel，真实路径留在 owned out，明确固定 file/entry/uncompressed-byte cap 并流式 hash；超额/缺失/多个 wheel、unsafe ZIP/重复/case-collision/escape、未知脚本或 `.pth`、非预期 package entries 均 fail closed。检查 filename、METADATA Name=`fuzzysearch`/Version=`0.7.3`/runtime requirements、WHEEL purelib 与兼容 tag、RECORD 成员/hash/size 一致；不以 output hash 证明功能正确。wheel 功能 import smoke 只能再用同一 AppContainer backend；host 只读 metadata 不 import 生成 wheel。
- provenance 绑定：小型 versioned derived-wheel evidence 必须包含 task id、target revision、original lock SHA/path、package/version、original sdist URL/SHA/size、固定 recipe digest（含 `--noexts`）、Python identity、bootstrap wheel SHA/size、sandbox result 和 output wheel SHA/size。输出标记 `DERIVED_FROM_PINNED_SDIST`，不声称 PyPI 官方 wheel 或 historical lock 已包含此 wheel hash；不得改 manifest/lock、伪造 uv cache/index entry、关闭 hash 校验或全局移除 `--no-build`。package 名称/版本相同也不足以接纳输出。
- 与 frozen preparation 的关系：当前 `execute_isolated_task` 会先调用 `stage_frozen_python_312`，其原始 `uv sync --locked --no-build` 因 fuzzysearch 无 wheel 而拒绝；因此不能通过该顶层 seam 启动构建。最小新公开 seam 拟为 `build_pinned_fuzzysearch_wheel(...) -> IsolatedBuildResult`，内部复用同一底层 backend，仅发布经校验的 derived evidence/output。下一 slice 先验收构建结果，不自动接入 task dispatch。未来 frozen environment 若消费它，必须新增显式可信 derived-artifact admission：按上述 source/lock/recipe receipt 验证当前 output bytes、完整核对 installed distributions 与 historical lock，所有其它 host dependency preparation 继续 binary-only。仅放 wheel 到 find-links 或预装同版本不能证明原 `uv sync` 接受它；这一路径仍需单独 red→green 验证，未建立之前继续 `FROZEN_ENVIRONMENT_INVALID`，不宣告 historical environment ready。
- 失败/cleanup：输入不符、unsupported/token preflight、spawn/timeout/resource、process failed、output invalid、cleanup failed 均为结构化失败，零 task/model dispatch，不发布成功 wheel/patch。清理含 build root、bootstrap staging、toolchain、profile/private scratch 和子进程；任一 incomplete 必须 `cleanup_complete=False`、CLEANUP_FAILED。若输出需保留，先验证并有界复制到 hash-named controlled artifact location，再完整 cleanup 后才发布 reference；cleanup 失败的候选不得作为可消费 artifact。不复用模型编辑 patch collector 充当 build artifact 契约。
- 最小 production slice 允许范围（建议，未实施）：一个小型 `agent/evaluation/isolated_source_build.py` 封装固定输入/receipt/output validation；`python_toolchain.py` 仅在必要时复用 bounded data-only bootstrap staging；`frozen_environment.py` 仅在必要时抽出共享纯 pinned-lock identity validation，不改变 uv no-build 路径；对应 `tests/evaluation/test_isolated_source_build.py` 和 `task_plan.md`。原则上不改 `task_execution.py`、`windows_sandbox.py` 或 `_windows_appcontainer.py` 行为；若运行揭示需要改 backend/environment，先停止单独审核，不顺手改 ACL/TEMP。无第二 backend/registry/通用 build 平台，无 dependency/manifest/oracle 修改。
- red→green/public seam：依次锁住 pinned identity 正确/错误及 current-lock no-fallback、archive escape/link/collision/bomb/容量、trusted bootstrap hash、no-host-execution、wrong interpreter/token/platform dispatch 前拒绝；再覆盖 fake backend failure/timeout、wheel missing/multiple/metadata/RECORD/hash 不符、stale receipt、cleanup failure不发布、host baseline不变。真实普通用户 AppContainer canary 必须证明工具正向 import control、真实 `--noexts` wheel 生成和隔离 import、host/oracle/source/bootstrap write denial、secrets/parent PYTHONPATH 不继承、受控禁网、resource/descendant cleanup、output provenance/bytes/hash 和完整清理；skip 不能替代真实证据。现有 frozen/task workspace/sandbox/toolchain/policy/admission 回归保持。
- 判定：`READY_TO_IMPLEMENT` 仅表示该固定 recipe、单 backend 的最小构建切片具备可审查实现边界；没有发现阻止编写该切片的确定性 source blocker。当前真实 environment 的确定性 blocker 仍是 fuzzysearch 无适用官方 binary wheel，以及现有 no-build preparation 没有显式 derived-wheel admission；本轮不解除该 blocker。Python 3.12/tooling/permissions/实际 wheel 构建与 admission 都尚未执行验证，不是 FEASIBLE/环境就绪/阶段完成结论。本轮 source build=0、AppContainer commands=0、model calls=0、cost=0、trials=0；等待 ChatGPT 审核，不 commit/push。

## 当前文档任务：独立 MASTER PLAN（2026-09-16）

- 基线：`330f20f25426ce9b0ceb26ea926a11f6a8cb9d15`，开始时工作区干净。
- [x] 核对当前源码、测试、研究资料和外部固定版本机制。
- [x] 编写根目录 MASTER_PLAN.md，形成 D 项到实施阶段和评测的证据链。
- [x] 执行现有测试、文档审计，确认历史架构调研文件未改动。
- [x] 更新核验记录，中文 Conventional Commit 本地提交。

仅新增长期设计基线并追加过程记录，不修改生产代码、测试、依赖或历史架构调研。

验收：157 项测试中 151 通过、6 项普通 Windows symlink 权限跳过；compile、11 个 prompt render、CLI help 通过。MASTER_PLAN 的 12 个 D 项五字段结构、本地链接、围栏、五组可执行探针已校验；已细分锁/授权及隔离/真实评测，避免高风险改造捆绑。历史架构调研相对基线无差异。没有运行真实 provider 或 benchmark，不报告效果提升。

## 目标
基于 langtalks/swe-agent 的源码与优质 GitHub 项目已实现机制，形成可验证的渐进优化方案。当前只调研，不修改业务代码。

## 阶段
- [x] 建立计划并确认范围。
- [x] 核查目标仓库版本、架构、调用链和实现缺口。
- [x] 核查参考项目实现、适用条件及移植成本。
- [x] 汇总优先级、模块边界、验证标准及首个改造切片。
- [x] 检查证据、诊断结果与提交范围，随本轮中文提交保存调研文档。

## 新增任务：完整项目导读
- [x] 确认本地源码与 GitHub 上游项目。
- [x] 盘点目录、依赖、配置、入口与运行方式。
- [x] 沿 Architect → Developer 主链路逐文件核查核心实现。
- [x] 核查工具模块、数据结构、提示词、异常路径与限制。
- [x] 编写并校验完整中文导读，使用中文 Commit Message 提交。

## 原则
- 源码事实、设计推断、待实验收益明确分开。
- 不将新增框架或技术热度等同于改进。
- 固定源码版本，给出可定位的链接。

## 问题与错误
- 本地 Git 仓库没有历史提交；现有 .serena/ 不纳入本次提交。
- 一次工具结果序列化失败，已按独立结果重新获取；deepagents 分支经核实为 main。
- 本地没有 LangGraph/Tree-sitter 依赖；先做无模型、标准库隔离复现，完整运行留待实施阶段。
- 沙箱用户执行 Git 时触发 dubious ownership；后续命令使用单次 `-c safe.directory=...`，不修改用户全局 Git 配置。
- 初次 Markdown 围栏计数检查发现正文示例中含裸三反引号；已改写该句并重新检查。
- 上游研究初稿一度误判两个 dotfile 不存在；已用 GitHub `fetch_file` 核实固定提交中的 `.env.example` 与 `.python-version` 并修正。
- 首次暂存后的 `git diff --cached --check` 发现 5 处 Markdown 强制换行尾空格；提交被中止，清理后重新检查。

## 状态
新增任务已完成。主导读为 research/swe-agent-complete-guide.md，上游与官方资料核查为 research/swe-agent-upstream-notes.md。Markdown 围栏/链接/UTF-8、Git diff whitespace、Python 语法及四项基线行为复现均已检查；未修改上游业务源码，也未执行需要有效 Anthropic 模型的端到端运行。

## 新增任务：规范与第一步可靠编辑

- [x] 将认可的优化方案固化为根目录 AGENTS.md。
- [x] 导入固定提交的上游业务源码并核对来源。
- [x] 以 WorkspaceEditor 公开 interface 完成可靠编辑的 TDD 纵向切片。
- [x] 运行编辑模块测试、项目语法检查和原始缺陷回归诊断。
- [x] 更新证据和状态，随本轮中文 Commit Message 提交。

### 本步 seam 与范围

- 公开 seam：`WorkspaceEditor.apply(EditProposal) -> EditResult`。
- 本步包含：工作区相对路径约束、唯一精确匹配、基线哈希预检、单文件原子替换、结构化 applied/rejected/noop 结果。
- 本步不接 LangGraph Developer，不加入模型重试、测试执行器、checkpoint 或 repo map；这些分别作为后续可验证切片。

### 状态

AGENTS.md 已单独提交。上游源码和静态资源已导入并标记固定来源；WorkspaceEditor 通过 12 个测试中的 11 个，1 个符号链接用例因 Windows 权限跳过；Python 语法、原版缺陷复现与离线构建检查通过。Developer 接入留作下一步独立切片。

### 本步问题与处理

- `git clone` 在沙箱外访问 GitHub 时连接被重置；确认没有留下 `.research-cache/upstream`，改用已连接的 GitHub 插件按固定提交读取文本源码。
- 首次 `uv build --offline` 在沙箱内无法读取 uv 缓存；提升权限重跑后确认上游缺少包发现配置，setuptools 将 `agent/static/helpers/research` 误判为多个顶层包。已限制只发现 `agent*`、`helpers*`，并显式包含 prompt Markdown，等待重新构建验证。
- 修正包发现后 `uv build --offline` 成功生成 sdist 与 wheel；build/dist/egg-info 均由上游 `.gitignore` 排除。
- 当前环境没有 ruff 或 pyright，未安装新工具，也不将 Python 语法检查表述为 lint/type-check。

## 新增任务：将可靠编辑接入 Developer

- [x] 复核 Developer 的路径、模型输出解析、写入和路由边界。
- [x] 先写失败测试，固定工作区读取、单块协议、成功推进和失败终止行为。
- [x] 用最小实现替换 Developer 的直接文件读写与行号切片。
- [x] 运行受影响测试、图级集成测试、语法检查和离线构建。
- [x] 更新实现依据与已知限制，使用中文 Commit Message 提交。

### 本步 seam 与范围

- `WorkspaceEditor.snapshot(path) -> WorkspaceSnapshot`：以和写入相同的工作区边界读取 UTF-8 内容及 SHA-256。
- `DeveloperEditExecutor.prepare(plan_path) -> WorkspaceSnapshot`：兼容现有计划里的 `./workspace_repo/` 前缀，并将路径收敛为工作区相对路径。
- `DeveloperEditExecutor.apply(snapshot, model_output) -> EditResult`：新文件把模型输出作为完整内容；已有文件只接受一个严格 SEARCH/REPLACE 块，再交给 `WorkspaceEditor.apply`。
- Developer 仅在 `EditStatus.APPLIED` 时推进任务；rejected 与 noop 都结束当前运行并保留结构化结果。
- 本步顺带处理空任务计划，避免在第一个任务索引处崩溃；不加入模型重试、测试执行器、checkpoint、预算或 repo map。

### 参考实现边界

- 采用 Deep Agents 固定提交中的唯一匹配与结构化结果思想；基线哈希和原子写入仍是本项目自己的保证。
- 采用 Aider 固定提交中可读 SEARCH/REPLACE 协议的思想，但不采用首次匹配、跨文件 fallback 或部分成功语义。

### 当前状态

本切片已完成。26 个测试中 25 个通过，1 个符号链接用例因 Windows 权限跳过；锁文件检查、Python 语法、Prompt 渲染、图导入、敏感值扫描、Git diff whitespace、离线 sdist/wheel 构建均通过。未运行真实 Anthropic 模型或 benchmark，不声明 Agent 效果提升。

### 本步问题与处理

- `uv sync --locked --offline` 因本机缓存缺少 `tree-sitter==0.21.3` 失败；随后按现有锁定版本联网同步依赖，没有升级业务依赖。
- 当前 uv 版本要求将旧版 `uv.lock` 从 revision 1 更新到 revision 3；解析结果仍为 66 个锁定包，等待构建复核。
- 当前开发依赖不含 ruff，`python -m ruff` 无法运行；本步只报告实际完成的测试、语法、构建和 diff 检查。

## 新增任务：S1 完整性审计与修正

- [x] 逐项复现并判定用户提出的 13 项问题，记录源码证据与范围判断。
- [x] 核查成熟项目的路径限制、同文件事务和模型配置实现，固定参考版本。
- [x] 通过公开 seam 写红测并完成 S1 内必要修正。
- [x] 在本机存在密钥时执行最小真实 API smoke test；否则留下可复现命令和明确阻塞原因。
- [x] 完成测试、语法、构建、静态检查、文档一致性和差异审计。
- [x] 更新 S1 审计报告并使用中文 Conventional Commit 提交。

### 已确认测试 seam

- Architect 编译图：从 invalid research 状态观察后续节点，不断言私有调用次数。
- 工作区文件接口：通过 `WorkspaceEditor`/同文件计划接口观察结构化结果与最终文件内容。
- 检索工具公开调用接口：用越界路径调用并观察结构化拒绝。
- Developer 编译图：观察 no-change、失败和同文件多 atomic task 的终态与文件内容。

### 设计结论

- 同文件事务由 editing 模块定义不可变 transaction，Developer 状态只保存并推进它；atomic proposal 顺序 stage，最后 commit 一次。
- `ImplementationPlan.status=no_changes` 且有 reason 才是无需修改；默认 ready 的空计划仍为 invalid。
- `EditProposal.task_id` 为必填，`EditResult.task_ids` 用于定位已 stage 与失败步骤，不引入完整 RunRecorder。

### 当前状态

13 项已完成源码审计，详细判断见 `research/s1-completeness-audit.md`。文件事务、统一路径 resolver、Architect 路由、no-change 语义、模型配置和错误分类已经落地。切换 DeepSeek 配置后共有 41 项测试：40 项通过，Windows junction 用例实际通过，1 项普通 symlink 用例因 WinError 1314 跳过。Python 语法、Prompt 渲染、图导入、diff whitespace 和离线 sdist/wheel 构建通过。真实 DeepSeek smoke 已验证鉴权、普通响应、工具调用和结构化输出。

## 新增任务：S1.1 DeepSeek 真实模型适配

- [x] 以 DeepSeek 官方文档核对当前模型 ID、专用 provider、API 端点和工具调用能力。
- [x] 通过公开模型配置 seam 写红测，将生产默认切换到 `deepseek-v4-flash`。
- [x] 统一 Architect/Developer 的模型构造入口，同时保留显式 Anthropic 配置。
- [x] 将真实 smoke 扩展为文本、强制工具调用和结构化输出三项检查。
- [x] 使用被 Git 忽略的本机密钥完成真实 API smoke。
- [x] 完成全量回归、构建检查并提交独立中文 Conventional Commit。

### 当前边界

smoke 通过只能证明密钥、端点、模型、工具调用和结构化输出与当前客户端兼容；它不等于完整 Architect → Developer 任务质量评测。完整效果仍需后续固定任务集、多次运行和执行器验收。

## 新增任务：S1 Final Gate

- [x] 统一默认读取工具与 Developer 编辑器的 workspace 配置来源。
- [x] 将 search 文件读取失败暴露为 warnings 与 skipped_files。
- [x] 在任何写盘前拒绝指向同一 canonical 文件的重复任务。
- [x] 增加父图 fake-model 状态传播和真实 ToolNode 循环回归。
- [x] 完成全量 S1 tests、compile、diff check 与 DeepSeek smoke。
- [x] 独立提交 S1 Final Gate。

### 范围边界

本步只关闭 S1 最终验收缺口；保留现有单文件事务、角色划分、模型 provider 和依赖主版本，不引入 S2 能力。

### 验收结果

统一工作区根、search 跳过文件诊断、重复 canonical 文件任务拒绝、父图状态传播与真实 ToolNode 循环均已覆盖。全量 48 项测试中 47 项通过，1 项因 Windows 缺少普通符号链接权限跳过；junction 用例通过。Python compile、Git diff whitespace 与真实 DeepSeek 三项 smoke 均通过。

## 新增任务：S2 测试验收和有限修复

### 固定范围

- 基线：`022371ee769269f38d33cfe2f799884822444505`。
- 只实现确定性 Verification Runner、baseline/post 判定、最多两次 repair 和顶层状态传播。
- 保留全部 S1 契约；不实现 checkpoint、预算、RunRecorder、repo map、正式 benchmark、新 Agent、Docker sandbox 或 provider/依赖升级。

### 阶段

- [x] 阅读 AGENTS.md、架构评审和当前 Developer/父图实现。
- [x] 固定 mini-swe-agent、SWE-bench、Aider 源码证据和适用边界。
- [x] 以公开 Verification 接口完成 runner 的 red → green 测试。
- [x] 以确定性判定接口覆盖 baseline/post 组合。
- [x] 接入父图并完成最多两次的 repair 闭环测试。
- [x] 运行全量测试、compile、diff check 并审计 S1 契约。
- [x] 更新研究文档。
- [x] 独立提交。

### 已确认 seam

- `VerificationRunner.run(VerificationSpec) -> VerificationResult`。
- baseline/post 的纯确定性判定函数。
- 注入 fake runtime、真实 Developer 编译子图和临时 workspace 的顶层图。

### 当前状态

实现、研究记录和最终验证已完成。全量 67 项测试中 66 项通过，1 项普通 symlink 用例因 Windows 权限跳过；compile、prompt 渲染和 diff check 通过，并以独立提交交付。

### 问题与处理

- GitHub 一次 TestSpec 猜测路径读取返回 404；改用固定提交的 SWE-bench grading 源码，不依赖错误路径。
- 后台参考研究因工具额度中断；主任务已直接核对三个项目的固定提交源码并写入 S2 研究文档。
- Windows venv 启动器的 timeout 测试一度留下短暂 cwd 句柄；runner 改为显式进程组/进程树终止，timeout 测试使用真实解释器进程验证。
- 抽取 verification controller 后，LangGraph 对 bound method 的 state 注解推断与父图 Pydantic state 不一致；父图增加带 `AgentState` 注解的薄 wrapper，保持 controller 与图 schema 解耦。

## 新增任务：S2 Final Gate

- [x] 让生产图从可信外部 JSON 配置加载 argv verification checks。
- [x] 增加整体 outcome，确保 Developer 失败或 NOOP 不被测试通过覆盖。
- [x] 将多文件 repair 收窄到最后提交并触发回归的文件。
- [x] 有限等待进程树清理，并验证 parent-child timeout。
- [x] 标记 verification 输出为不可信诊断数据并传递截断标志。
- [x] 运行全量测试、compile、prompt render 和 diff check。
- [x] 独立提交 S2 Final Gate。

验收：全量 77 项测试中 76 项通过，1 项普通 symlink 用例因 Windows 权限跳过；
compile、prompt render 与 diff check 通过。生产配置、整体终态、多文件 repair 和
parent-child timeout 均由顶层或公开接口测试覆盖。

并行执行 compile 与 prompt render 时曾因瞬时内存不足导致 Pydantic 初始化失败；
改为顺序执行后两项均通过，最终全量测试也在顺序执行下通过。

## 新增任务：S2 Acceptance Fix

- [x] repair canonicalization 与 S1 `os.path.normcase` 完全一致。
- [x] 修正 NO_CHANGES、PENDING、RUNNING 与 verification 的 outcome 优先级。
- [x] 在公开 spec 和环境配置入口拒绝非有限或非正 timeout。
- [x] 修正 `.env.example` JSON，并更新 README 主流程与 S2 research。
- [x] 完成全量验收并独立提交。

验收：83 项测试中 82 项通过，1 项普通 symlink 用例因 Windows 权限跳过；
compile、prompt render 与 diff check 通过。

## 新增任务：S2 Seal Fix

- [x] 父图所有 END 路径将残留 PENDING outcome 封口为 FAILED。
- [x] Windows timeout 先执行有界 PID tree taskkill，Job Object 作为 backstop。
- [x] 加强终态矩阵和 parent-child timeout 黑盒测试。
- [x] 完成全量验收并独立提交。

验收：84 项测试中 83 项通过，1 项普通 symlink 用例因 Windows 权限跳过；
compile、prompt render 与 diff check 通过。

## 新增任务：S3 Design Gate

### 固定范围

- 基线：`8d89670e97442f652d592a3e0f6f63c373fe3717`。
- 只设计最小 RunConfig、预算与 usage、运行轨迹、SQLite checkpoint 与恢复对账。
- 本轮不修改生产代码或依赖，不进入 S4/SWE-bench，不增加 Agent、UI、sandbox 或 provider。

### 阶段

- [x] 审计现有 S1/S2 的配置、状态、模型/工具边界、编辑 hash 与图装配 seam。
- [x] 核对 LangGraph 1.x 官方 SQLite saver 接口、thread_id 与子图传播行为。
- [x] 核对 mini-swe-agent 等固定版本源码中的预算、usage、trajectory 与恢复机制。
- [x] 设计公开 seam、状态字段、图节点、恢复状态机、预算规则和测试矩阵。
- [x] 明确拟修改文件、必要依赖变化、迁移顺序与已知限制。
- [x] 仅完成研究文档与本计划的检查并独立提交。

### 当前状态

Design Gate 已完成。仅修改 `research/s3-runtime-recovery.md` 和本计划；84 项 S1/S2 测试中 83 项通过、1 项因 Windows 普通 symlink 权限跳过，compile、11 个 prompt render、设计结构检查和 `git diff --check` 均通过。未修改生产代码或依赖。

## 新增任务：S3 Design Gate Seal

### 固定范围

- 基线：`06d0db01dd0e9a483386e3241afe9e75f5b74565`。
- 只修订 S3 设计文档与本计划；不修改生产代码、测试或依赖。
- 保留 S1/S2 契约与 Architect → Developer → bounded repair 主链；不进入 S4/SWE-bench，不增加 Agent、UI、sandbox 或 provider。

### 审查与设计封口

- [x] 逐项审核五项调整，确认均属于 S3 且优于原设计，并记录必要的保守边界。
- [x] 将预算改为 checkpoint 先行的 `reserve → dispatch → settle` graph steps；恢复遇到结果不确定的 `IN_FLIGHT` 调用停止，step 不回退。
- [x] 将 usage boundary 放在 raw model response 与 parser 之间；设计 secret-safe checkpoint/event 与 Runner pipe-drain 完整日志 artifact。
- [x] 定义 durable step/call/write identity、事件幂等键、严格分离的 start/resume，以及 pending write/repair 生命周期。
- [x] 补充 runtime root、model output-token limit、Agent/workspace revision 和 path-only WorkspaceIdentity 限制。
- [x] 保留公开 ToolNode，只增加前后预算节点；不包装私有 checkpoint 表或建立 telemetry 平台。
- [x] 补齐 crash 预算不回退、raw usage、secret canary、event replay、start/resume、完整日志 artifact，以及跨 Architect → Developer → repair → restart 的预算测试矩阵。
- [x] 完成 Markdown 结构/固定链接/禁止范围、全量 S1/S2、compile、prompt render 与 `git diff --check` 验证。
- [x] 仅提交 `research/s3-runtime-recovery.md` 与 `task_plan.md` 的独立中文 Conventional Commit。

### 当前状态

设计修订与验证已完成。全量 84 项 S1/S2 测试通过，41 项 unittest subtests 通过；Python compile、11 个 prompt render、Markdown 围栏、27 个固定 GitHub 源码链接和 `git diff --check` 均通过。S3 生产实现尚未开始，项目依赖文件未变化。

## 新增任务：S3 Design Seal 最终集成边界

### 固定范围

- 基线：`8808be1da787d79a2d5c84aaa5379d4ae941340b`。
- 只修订 `research/s3-runtime-recovery.md` 与本计划；不修改生产代码、测试或依赖。
- 保持 S3.1 为 Config + Identity，S3.2 才启用 SQLite + durable Budget；不进入 S4 或其他禁止能力。

### 审查与设计封口

- [x] 审核四项调整，确认都能加强 S3 恢复一致性且不扩大阶段范围。
- [x] 定义版本化 semantic RunConfig digest、纳入/排除字段，以及允许 API key 轮换的 resume binding。
- [x] 将本地 durable CLI/library seam 定为 S3 正式入口，明确 `langgraph.json:swe_agent` 仅为非 durable Studio/dev 兼容入口。
- [x] checkpoint 绑定 clean Agent commit；known mismatch 拒绝、unknown 告警，不设计 migration framework。
- [x] 分离业务 `max_steps` 与 LangGraph `recursion_limit`，固定当前拓扑公式、cycle 约束和最坏路径测试。
- [x] 完成指定文件范围、Markdown、全量 S1/S2、compile、prompt render 与 `git diff --check` 验证。
- [x] 使用独立中文 Conventional Commit 提交。

### 当前状态

设计修订与验收已完成。全量 84 项 S1/S2 测试与 41 项 unittest subtests 通过；Python compile、11 个 prompt render、Markdown 围栏、27 个固定 GitHub 源码链接和 `git diff --check` 通过。仅两份指定文档有差异；S3 生产实现尚未开始，项目依赖文件未变化。

## 新增任务：S3.1 Config + Identity

### 固定范围

- 基线：`1503e62efa32f95a45983961fc07b3f8ad6d6112`。
- 只实现 RunConfig、semantic digest、显式双 provider 装配、Identity 和纯 preflight。
- 不安装 SQLite，不增加 durable CLI，不把 BudgetController 或 reserve/dispatch/settle 接入生产图。

### TDD 纵向切片

- [x] 红测固定 RunConfig 数值、base URL、workspace/runtime 隔离和 symlink/junction 拒绝行为。
- [x] 实现显式 mapping loader、TokenPricing 和版本化 secret-free semantic digest。
- [x] 红测并实现 DeepSeek/Anthropic 显式 ModelSettings + output-token limit 装配，保留旧环境入口。
- [x] 红测并实现 path-only WorkspaceIdentity、clean/dirty/unknown AgentCodeRevision。
- [x] 红测并实现 Start/Resume request/result、injectable CheckpointLookup 与全部结构化拒绝路径。
- [x] 完成全量 S1/S2/S3.1、compile、prompt render、diff check、敏感值与禁止范围审计。
- [x] 更新最小文档并独立提交。

### 当前状态

S3.1 已完成。全量 124 项测试与 110 项 unittest subtests 通过；Python
compile、11 个 prompt render、diff whitespace、敏感值和禁止范围审计通过。
普通 symlink 与 Windows junction 用例均实际通过；仅有现有 tree-sitter
弃用告警。未修改依赖或生产图，max_steps/max_cost 只完成校验与语义绑定，
SQLite、durable CLI 和预算执行仍留在 S3.2。

## 新增任务：S3.1 Acceptance Fix

- [x] 显式 ModelSettings 装配拒绝所有未进入 semantic digest 的额外模型参数；旧 non-durable 环境入口保持兼容。
- [x] 将 WorkspaceIdentity 的持久化值校验与 `from_root()` 的实时文件系统校验分离。
- [x] 恢复 S1 对祖先 symlink/junction 的兼容，同时继续拒绝 workspace root 自身及内部目标链接。
- [x] 运行全量测试、compile、prompt render、diff check，并独立提交。

### 当前状态

三项边界修复及最终验收已完成。全量 127 项测试与 117 项 unittest
subtests 通过；Python compile、11 个 prompt render、diff whitespace 和
禁止范围审计通过。普通 symlink 与 Windows junction 用例均实际通过；仅有
现存 tree-sitter 弃用告警。未引入 S3.2 能力或依赖变化。

## 新增任务：S3.2 Checkpoint + Durable Budget

### 固定范围

- 基线：`5929328cbde1af10f27a5405ff80d08ebf0fba5f`。
- 只实现同步 SQLite durable runtime、严格 start/resume、可恢复预算和现有图调用边界。
- 不实现 trajectory、artifact/log spool、pending-write recovery、S4 或新 Agent/provider。

### TDD 纵向切片

- [x] 锁定 SQLite saver 3.1.1，验证父图 checkpointer 与默认子图传播、关闭重开恢复。
- [x] 实现不可变 Budget contracts，覆盖 step、usage、cost、deadline 与批量准入。
- [x] 实现 reserve → dispatch → settle durable boundary 和不确定调用恢复。
- [x] 接入 Architect、Developer 与 ToolNode，保持 S1/S2 主链和 non-durable 图行为。
- [x] 实现 library start/resume 与最小 CLI，覆盖所有 preflight 拒绝路径。
- [x] 完成全量 tests、compile、prompt render、diff check 和禁止范围审计。
- [x] 更新最小文档并独立提交。

### 当前状态

S3.2 实现与验收已完成。全量 157 项测试通过，6 项普通 Windows symlink 用例因当前账户缺少创建权限跳过；Windows junction、安全路径、真实 SQLite 关闭重开、父子图 checkpoint 传播、两类 crash window、预算跨 Architect → Developer → repair、deadline、secret canary 和全部 preflight 拒绝路径均实际通过。Python compile、11 个 prompt render、CLI help、SQLite saver 3.1.1 版本检查、非 durable 图导入、禁止范围/密钥扫描及 `git diff --check` 通过；仅有既存 tree-sitter 弃用告警。trajectory、artifact/log spool、pending-write/hash recovery 和 exactly-once 仍未实现。

## 新增任务：S3.2 Acceptance Fix

### 固定范围

- 基线：`330f20f25426ce9b0ceb26ea926a11f6a8cb9d15`；保留其后的计划文档提交。
- 只修正 cost gate、wall-clock deadline、真实生产链恢复集成和 usage 来源标签。
- 保持现有 ToolNode、S1/S2 主链与 non-durable 图；不进入 S3.3 或 S4。

### TDD 验收切片

- [x] 证明 max_cost/cost_unknown 只阻止下一次 MODEL，当前已请求的 TOOL 批次仍受 step/deadline 准入。
- [x] 在 model/tool 返回后检测 deadline overrun，完整 settle 结果并阻断 commit、verification 与后续副作用。
- [x] baseline/post verification 按 remaining time 缩短单项 timeout，deadline 耗尽时零 subprocess。
- [x] 用真实 SQLite 与实际 parent/Architect/Developer 图完成 post regression、repair、关闭重开 resume。
- [x] 将配置估算统一标记为 `configured_estimate`，保留 pricing source 在配置摘要中的来源语义。
- [x] 更新 README 的实际能力描述，运行全量 tests、compile、prompt render 与 diff check。
- [x] 使用独立中文 Conventional Commit 提交，不 amend 既有提交。

### 当前状态

四项 acceptance 边界已完成。全量 162 项测试中 156 项通过、6 项因当前
Windows 账户缺少普通 symlink 创建权限跳过；真实 junction、cost gate、
model/tool deadline overrun、verification 零进程、真实 SQLite 生产链关闭重开
repair 均通过。Python compile、11 个 prompt render、CLI help、SQLite saver
3.1.1、non-durable 图导入和 `git diff --check` 通过；仅有既存 tree-sitter
弃用告警。同步 provider/tool 调用仍只能在返回后检测越期，S3.3 能力未实现。

## 新增任务：S3.2 Final Seal

### 固定范围

- 基线：`48738bd5d1489e31f51f96be6cb2e80c5a4148bd`。
- 只封闭 reserve→dispatch deadline、commit overrun、recursion-limit 证明和恢复序列输入边界。
- 不修改阶段顺序、`MASTER_PLAN.md` 或 `notes.md`，不进入 S1a/S3.3/S4。

### TDD 验收切片

- [x] reserve 后、dispatch 前越期时零外部调用，step 保留且 reservation 确定性终结。
- [x] commit 成功返回后越期时保留真实文件与 EditResult，并阻止后续节点。
- [x] 用实际 compiled graph 最坏路径证明现有 recursion-limit 公式充足。
- [x] checkpoint 值对象只归一化合法序列，拒绝 string 和 scalar。
- [x] 运行全量 tests、compile、11 prompt render、CLI 与 diff check。
- [x] 使用独立中文 Conventional Commit 提交，不 amend。

### 当前状态

Final Seal 实现与验收已完成。全量 168 项测试中 162 项通过、6 项因当前
Windows 账户缺少普通 symlink 创建权限跳过；junction 用例通过。fake-clock
覆盖 MODEL/ToolNode reserve 后越期零调用、SQLite 中断恢复后确定性 settle，
以及 commit 成功返回后越期保留文件与 EditResult。实际 compiled graph 以
40 个业务 step 的单工具循环正常到达 `MAX_STEPS_EXCEEDED`，证明现有
`max(200, 8*max_steps+64)` 公式充足，无需调整。Python compile、11 个
prompt render、CLI help 和 `git diff --check` 通过。

## S3 治理/测试封口（当前状态同步）

S3.2 技术验收已完成并冻结，当前冻结 commit 为
`5850b8370c49f868e90aeffe9e6042f85eaa522c`。最新 Codex 本地验证为
169 total / 163 passed / 6 skipped，129 个 unittest subtests 通过；Python
compile、11 个 prompt render、CLI help 和 `git diff --check` 均通过。6 个
skip 仅来自当前 Windows 账户缺少创建普通 symlink 的权限；没有执行 GitHub
CI 或独立外部测试，未作相应声明。

当前仍未实现：model provider 显式 timeout/retry/attempt 语义、secret-safe
full-state persistence、EventSink / RunRecord、完整 verification artifact
spool、workspace/thread 独占、read/write authorization policy、pending-
write/hash recovery、verification execution recovery；exactly-once 不承诺。
当前不进入 S4；后续继续完成 `MASTER_PLAN` §5.2 中属于运行控制、轨迹和恢复
的剩余要求，并保留现有 S1/S2/S3.1/S3.2 阶段编号体系。

## D3/S2c Final Seal：稳定验收策略链路

### 固定范围

- 基线：`6a3383c0cd57f84c63519ff6b2ead108e08603f4`。
- 只把结构化 acceptance policy 接入 VerificationController、RunSummary 和 CLI，封闭按 check 的 allow-list、pytest JUnit 退出状态一致性与隔离的临时 evidence 生命周期。
- 不扩展 repair，不新增 parser registry、provider timeout、EventSink、recovery 或 S4 能力。

### TDD 验收切片

- [x] post verification 调用 `evaluate_acceptance`，可信 accepted 才能产生 CLI exit 0；PRE_EXISTING_FAILURE、IMPROVED 等诊断状态保持原值。
- [x] 按 `VerificationSpec.name` 独立解释 allowed failure，拒绝重复 check id，并拒绝 FAIL→ERROR/SKIPPED 的退化。
- [x] 校验 pytest 非正常退出与 JUnit 报告矛盾，旧 `failure_id` 只保留诊断用途。
- [x] 将 JUnit 报告限制为 pytest/`python -m pytest` 的显式 `--junitxml/--junit-xml` 输出；只改写精确匹配的 JUnit destination 到 owned temporary output，test path、`-k` 等其它参数保持不变，workspace report_path 不做 snapshot/覆盖/restore，清理失败结构化为 `EXECUTION_ERROR`，KeyboardInterrupt/SystemExit 仍传播且先清理。
- [x] 完成全量 unittest/pytest、compile、11 个 prompt render、CLI help 和 diff check。

### 当前状态

D3/S2c Seal blocker 修复已完成。最新本地验证为 unittest 218 tests OK、6 skipped；
pytest 212 passed、6 skipped、158 subtests passed。6 个 skip 仅来自当前 Windows
账户缺少普通 symlink 创建权限；没有 GitHub CI 或独立外部测试证据。当前仍只支持
`junit-xml-v1`，报告缺失、畸形、退出状态矛盾或命令异常时保守拒绝；报告在
owned temporary output 中读取，workspace report_path 的原有 bytes/mtime 不变，
pytest 对其它 workspace 文件的副作用不在本轮处理，也未实现 artifact spool/recovery。后续能力仍按
MASTER_PLAN §5.2 继续，本轮不进入 D4/S4。

## D4/S3.2a：实际调用时限

### 固定范围

- 基线：`88cbd5e0630cec4ca6c1bf247d5a1fc4fee83980`。
- 只增加 RunConfig 的模型请求 timeout/retry policy、semantic digest 绑定、
  durable dispatch 的剩余 deadline clamp，以及模型 timeout/transport failure
  的结构化结算；D3/S2c verification acceptance 保持冻结。本次 Final Seal
  只修正普通模型响应结算、明确 transport 异常边界，并为 durable reservation
  增加 checkpoint-safe 的请求身份范围。
- 不新增 provider、依赖、EventSink、secret filter、recovery 或 S4 能力。

### TDD 验收切片

- [x] 核对虚拟环境公开构造签名：`langchain-deepseek 1.1.0` 的
  `ChatDeepSeek(timeout, max_retries)` 与 `langchain-anthropic 1.7.1` 的
  `ChatAnthropic(timeout, max_retries)`；explicit durable path 传入 timeout 与
  `max_retries=0`，legacy `settings=None` 入口保留原 options 行为。
- [x] RunConfig 校验有限正的 `model_request_timeout_seconds`，单次尝试
  `ModelRetryPolicy(max_attempts=1)`，并把两者纳入 versioned semantic digest。
- [x] reservation 后通过公开 boundary 计算
  `min(model_request_timeout_seconds, deadline_remaining)`；剩余时间耗尽时零
  外部调用、已预留 step 不回退。
- [x] provider timeout/transport failure 结算 UNKNOWN usage/cost，错误分别为
  `MODEL_REQUEST_TIMEOUT` / `MODEL_TRANSPORT_ERROR`，不自动重发；返回后越过
  absolute deadline 仍由既有 `TIMEOUT_OVERRUN` 与副作用阻断规则优先处理。
- [x] 普通非 `ModelCallResult` 响应继续以 `failed=False` 结算，usage/cost 保持
  `UNKNOWN`/`None`；仅明确的 provider/httpx transport 异常进入
  `MODEL_TRANSPORT_ERROR`，本地意外 `OSError`/`RuntimeError` 继续传播。任一
  `ModelCallResult.error` 或 `error_code` 存在都会生成 runtime error 并 FAILED
  settle；裸 builtin `TimeoutError` 不自动伪装成 provider timeout。
- [x] `CallReservation.request_identity_scope` 进入 checkpoint-safe 序列化；模型
  reservation 明确为 `PARTIAL`，SQLite 关闭重开后的 resume 保留该范围。当前
  digest 不是最终 rendered request，不能据此安全重放或声称 complete identity。
- [x] D4 新增 timeout/retry 语义后将 canonical semantic digest schema 从 v1 升为
  v2；后续 canonical 字段或编码语义变化继续显式升版本，凭据轮换仍不参与摘要。
- [x] 真实本地慢 HTTP server 验证 DeepSeek 请求只发生一次，迟到响应不继续；
  fake-clock、模型/工具预算、repair、resume、N+1 和多 verification deadline
  回归保持通过。
- [x] 完成全量 unittest/pytest、compile、11 个 prompt render、两套 CLI help 和
  `git diff --check`。

### 当前状态

D4/S3.2a Final Seal 实现与本地验收已完成。当前验证为 unittest 233 tests OK、6 skipped；
pytest 227 passed、6 skipped、163 subtests passed。6 个 skip 仅来自当前 Windows
账户缺少普通 symlink 创建权限；没有 GitHub CI 或独立外部测试证据。模型 provider
版本保持不变；同步 SDK 无法被外层立即中断时，仍只能在返回后记录
`TIMEOUT_OVERRUN`，不承诺绝对瞬时终止或 exactly-once。模型请求身份仍为
`PARTIAL`，尚无 prompt/rendered request replay 或独立 attempt recovery。后续
secret-safe persistence、EventSink/RunRecord/artifact、workspace locking/policy、
pending-write/verification recovery 仍未实现，本轮不进入 S3.3/D6 或 S4。

## D6/S3.3a：持久化前秘密处理

### 固定范围

- 初始基线：`8985bda668622c7dcfc3d9ac66a692677d315eae`。
- Final Seal 基线：`78ba587e5b9394b6b3fc67b532175852e5e12697`。
- 只增加 process-local `KnownSecretFilter`，来源限定为 `RunConfig` 中的已知
  API key；使用固定 redaction marker，过滤器和原始凭据不进入 graph state 或
  SQLite checkpoint。
- 在 durable start 初始任务、Architect/Developer 节点输出、公开 ToolNode
  adapter 输出、verification result 以及 runtime message 写入持久化 state 前
  过滤。代码承载内容（源码、working copy、edit proposal/patch）发现已知凭据时
  返回 `SENSITIVE_DATA_DETECTED` 并拒绝写入；诊断文本只做确定性脱敏。
- 不实现 EventSink、RunRecord、artifact/spool、recovery、locking/policy、S4
  或依赖变更，不改 D1-D4 已冻结语义。

### TDD 验收切片

- [x] 通过公开 filter seam 覆盖普通消息、结构化模型结果、ToolNode 输出、
  verification stdout/stderr/message 和 runtime exception message 的固定 marker
  脱敏。
- [x] 通过 WorkspaceEditor/WorkspaceTransaction seam 覆盖已有源码、working
  copy 与 edit proposal 中的 canary 拒绝，确认 bytes/mtime 不变且返回结构化
  `SENSITIVE_DATA_DETECTED`。
- [x] 在 node wrapper 内处理含已知凭据的异常，保留无凭据 RuntimeError 原样传播，
  并保持 KeyboardInterrupt/SystemExit 传播；完整 parent/Architect/ToolNode/Developer
  canary 链路单独验证最终 state、SQLite 数据库与 CLI JSON 不出现已知 canary。
- [x] 独立验证 node-raises canary：受控开启 WAL 并保持 SQLite 连接打开，分别扫描
  database、WAL/SHM 与最终 state，确认异常秘密不进入持久化文件。
- [x] 在 start/resume 打开 SQLite 或运行 preflight 前检查 run/thread/task/workspace
  identity；敏感 identity 结构化拒绝，不使用脱敏 identity 继续执行。
- [x] 将敏感编辑错误映射为 durable `SENSITIVE_DATA_DETECTED`，阻止 child/parent
  后续 reserve、dispatch、post verification 与 repair，确认文件 bytes/mtime 不变。
- [x] 通过仅注入 `secret_filter` 的 Architect 公共 seam 验证首个模型异常后立即 END，
  后续 model/tool 调用为零并保留结构化 `SENSITIVE_DATA_DETECTED`。
- [x] 通过 Architect 公共 seam 验证正常进入 ToolNode、工具异常包含已知 canary 且
  预置合法非空 `durable_call_result` 时立即 END；当前 HEAD 在异常发生后后续 model/tool 调用为零，
  结果保留结构化 `SENSITIVE_DATA_DETECTED` 且不含 canary。相同回归在父提交
  `2b1e413509ba261859bad8b09868dfa0ebd5534d` 上因再次调用 `conduct_research` 失败。
- [x] 完成全量 unittest/pytest、compile、11 个 prompt render、两套 CLI help 和
  `git diff --check`。

### 当时状态（D6/S3.3a Final Seal）

D6/S3.3a Final Seal 当时已完成本地实现与验收，等待外部审核，未宣称技术冻结。
当时验证为 unittest 246 tests OK、6 skipped；pytest 240 passed、6 skipped、169
subtests passed。6 个 skip 仅来自当前 Windows 账户缺少普通 symlink 创建权限；
Python compile、11 个 prompt render、start/resume 两套 CLI help 和 `git diff --check`
均通过。没有 GitHub CI 或独立外部测试证据。覆盖范围只承诺 `RunConfig` 中已知凭据，
不能识别未配置的隐私或任意秘密；完整 secret-safe state persistence、EventSink/RunRecord、
artifact/spool、recovery、workspace locking/policy 和 exactly-once 当时仍未实现。本轮随后进入
S3.3b，不改变 D6a 的已验收边界。

## D6/S3.3b Final Seal：幂等运行审计记录

### 范围与公开契约

- `agent.runtime.trajectory` 保留稳定 facade；内部拆为 contracts、sink、record、recorder
  四个小模块，继续导出版本化 `RunEvent`、`AppendResult`、`EventSink` Protocol、
  `JsonlEventSink`、`RunRecord` 和记录写入结果。
- 幂等键只由 `schema_version + run_id + subject_id + event_type + phase` 的
  canonical JSON 摘要确定；sink `sequence` 只表示该文件的写入顺序。重放返回
  `DUPLICATE`，同键不同内容返回 `CONFLICT`；启动时发现尾部残缺、非法 JSON、序列损坏、
  既有记录损坏或已知凭据时结构化失败，不静默跳过。
- 本地 JSONL 和记录目录位于 `RunConfig.runtime_root`，记录路径只使用稳定哈希，
  不把用户的 run/thread/task id 拼进文件名。事件仅保存有界的模型/tool/edit/verification
  摘要、usage、错误码和稳定身份，不进入 graph state/checkpoint，也不参与恢复决策。
- durable runtime 默认注入该 sink；图结束后写入 versioned `RunRecord`，成功后通过
  `RunSummary.record_ref` 暴露引用。新增 `audit_incomplete` 后，公开 `RunSummary` schema
  已升级为 v2；既有 lifecycle 与 CLI 0/1/2/3 语义保持不变。事件或记录写入失败/损坏将
  `audit_incomplete=true`，CLI 默认不会返回 0；checkpoint 仍是恢复唯一依据。

### TDD 与验证事实

- [x] 覆盖关闭重开后的事件重放幂等、同键冲突、尾部截断/非法 JSON、事件和记录写失败，
  以及既有损坏记录的保守拒绝；两个同进程 sink 交错 append 会先刷新磁盘索引并产生
  sequence 1、2，明确不提供多进程锁。
- [x] 通过公开 Architect graph seam 验证 model 事件；通过 durable start/resume seams 验证
  lifecycle 与 checkpoint/event 独立性；另以有界 recorder contract test 锁定 tool/edit/
  verification 摘要形状。model/tool 记录 request/response digest 与 usage，edit 只记录
  status/error/hash/diff digest，verification 只记录 check identity/status/failure_id/attempt。
  审计文件不保存 raw prompt、response、源码、tool payload 或 stdout/stderr。
- [x] 通过真实 SQLite durable seams 验证“事件领先但 checkpoint 仍 IN_FLIGHT”恢复为
  `OUTCOME_UNKNOWN` 且不 redispatch，以及“checkpoint 已有 durable result、事件缺失”只 settle
  且不 redispatch；事件不参与恢复决策。
- [x] durable runtime 的审计时间统一取注入 clock；内部实现拆分后，旧
  `agent.runtime.trajectory` 公共 import 保持兼容。
- [x] 覆盖 KnownSecretFilter 对事件/记录的确定性脱敏、哈希记录路径和 runtime 文件扫描；
  当前仍只承诺配置中已知 secret，不是通用隐私检测。
- [x] 最新本地验证：unittest 264 tests OK / 6 skipped；pytest 258 passed / 6 skipped /
  169 subtests passed；Python compile、11 个 prompt render、两套 CLI help 和
  `git diff --check` 通过。没有 GitHub CI 或独立外部测试证据，不将本地结果外推为外部验收。

### 边界与未实现项

- 当前 sink 只保证单进程 append 协调，不承诺多进程 `append_once`；不引入事件数据库、
  telemetry 平台或万能 RunManager。
- 本轮不实现 verification 完整日志/spool、ArtifactStore、workspace/thread locking、
  pending-write/hash recovery、事件驱动重放或 exactly-once；这些限制不能由 RunRecord 掩盖。
- workspace revision 仍记录为 `UNKNOWN`，事件与 checkpoint 没有跨介质事务；事件落后或领先
  checkpoint 时，恢复仍只依据 checkpoint 状态。

## D6/S3.3b Final secret-safety Seal

### 固定范围

- 基线：`48e34233b13d44c2b6a6587c4aa08ad51a6a3890`。
- 只封闭 EventRecorder 和 durable `_run/_finish_audit/_audit_failure` 审计失败路径的已知 secret
  边界；不修改 RunSummary v2、事件身份/sequence、D1-D4 或 S3.3a 契约。
- 不进入 S3.3c，不实现 artifact/spool、recovery、locking/policy、S4 或新增依赖。

### 验收事实

- EventRecorder 在抛出 `AuditIncompleteError` 前对外部 `AppendResult.error_code/message` 做
  `KnownSecretFilter` 确定性脱敏；对直接抛出的结构化 `EventSinkError` 也只转换为安全异常，
  避免 secret-bearing sink error 作为 LangGraph raw pending write。普通 unexpected `RuntimeError`
  继续传播。
- durable audit failure 返回的 `message`、`error_code`、`audit_error_code` 以及 terminal
  record/event 中的错误字段均经过同一已知 secret 边界；`audit_incomplete=true`，CLI exit 保持 2。
- 真实 SQLite/checkpointer 回归覆盖 lifecycle start 成功后 graph node sink error、terminal
  `_finish_audit` sink error；扫描实际生成的数据库和 runtime 文件（存在时包含 WAL/SHM），确认
  最终 result/summary 无 canary，且 node sink failure 后不继续执行后续副作用。
- 本轮本地验收完成：unittest 268 tests OK / 6 skipped；pytest 262 passed / 6 skipped /
  169 subtests passed；Python compile、11 个 prompt render、两套 CLI help 与
  `git diff --check` 均通过。无 GitHub CI 或独立外部测试证据，不将本地结果外推为外部验收。

### 已知边界

- 过滤范围仍只覆盖 `RunConfig` 中已知凭据；无法识别未配置的秘密或任意隐私。
- EventSink/RunRecord 继续是审计旁路，checkpoint 仍是恢复依据；不承诺跨介质 exactly-once、
  多进程 append、完整日志 artifact 或 D5/S3.3c 能力。

## D6/S3.3c：验证日志 artifact 与安全摘要

### 固定范围

- 基线：`c2b57ddacd7bb805ce288d691fdb86c62a2ebaff`。
- 只实现 verification stdout/stderr 的流式脱敏、完整 artifact、`ArtifactRef` 和 checkpoint-safe
  `VerificationSummary`；保持 D3 acceptance、baseline/post、repair 次数和现有 EventSink 契约。
- 不实现模型/工具 raw response artifact、patch artifact、write/verification recovery、locking、
  telemetry、S4 或新依赖。

### 实施阶段

- [x] 建立 schema v1 `ArtifactRef`/`ArtifactStore` 和 `KnownSecretFilter` streaming seam；artifact
  只写 `runtime_root/artifacts`，使用 hash-only 文件名、临时文件、flush/fsync 和 atomic replace。
- [x] 将 runner 的 stdout/stderr pipe drain 接入脱敏 spool、原始完整 digest、bounded preview 和
  结构化 artifact failure；preview 截断不影响 artifact，timeout 后仍完成可获得输出的 drain。
- [x] 让图状态使用 checkpoint-safe `VerificationSummary`，并将 artifact refs、digest 和 truncation
  摘要接入 verification `RunEvent`；repair feedback 只保留 bounded preview，不暴露 runtime 路径。
- [x] 补齐长 stdout/stderr、跨 chunk secret、timeout、quota/publish/cleanup fault、临时文件清理、
  真实 SQLite checkpoint 无 raw log/canary，以及 D3 baseline/post/evidence 回归。
- [x] 完成全量 unittest/pytest、compile、11 个 prompt render、两套 CLI help 和 diff check。

### 当前状态

S3.3c 基础实现已完成；本轮封口补充了 runtime artifact 目录的 canonical containment、POSIX
symlink/Windows junction 拒绝，以及 semantic config schema v3 对 `VerificationSummary` +
artifact evidence checkpoint 语义的恢复门槛。真实 SQLite 回归证明旧 v2 暂停 checkpoint 在当前版本
resume 时 fail closed，且不会进入 verification 节点；non-durable runner/public seam 保持兼容。
本轮本地验证为 unittest 280 tests OK / 7 skipped、pytest 273 passed / 7 skipped / 169 subtests
passed；compile、11 个 prompt render、root/start/resume CLI help、`git diff --check` 均通过。没有
GitHub CI 或独立外部测试证据，不将本地结果外推为外部验收。

### 已知边界

- secret filter 只覆盖 `RunConfig` 中已知凭据；artifact 是脱敏后的完整 verification 输出，不是通用
  隐私检测，也不接 model/tool raw response、源码 patch 或 recovery。
- quota 按单个输出 artifact 执行；磁盘、写入、发布、清理失败会标记 evidence incomplete，不能被
  D3 acceptance 当作可信成功。artifact 与 checkpoint/event 没有跨介质事务，不承诺 exactly-once、
  workspace locking、verification replay 或 S4 能力。
- stdout/stderr digest 是脱敏前采集到的原始完整输出 digest；`ArtifactRef.sha256` 是脱敏后完整
  artifact 内容的 digest。artifact 路径只允许 canonical `runtime_root` 下的 `artifacts` 目录，
  预存 symlink/junction 会结构化拒绝；本轮不扩展 recovery/replay 或多进程锁。

## D7/S3.5a：合作进程独占准入

### 固定范围

- 基线：`7adde51e2b549e198f826ad1010dc11b280db64d`。
- 只增加 `WorkspaceAdmissionLock` 公开 seam 和 durable start/resume 准入集成；锁使用
  POSIX `flock` 或 Windows `msvcrt.locking`，不把 SQLite 写锁或进程内线程锁当作进程互斥。
- workspace lock 只由 canonical workspace 决定，位于进程共享的受控临时 admission area；
  runtime/thread lock 由 canonical `runtime_root` 与 `thread_id` 决定，位于 `runtime_root/locks`。
  两把锁按 workspace → runtime/thread 顺序非阻塞获取，文件名只使用安全 digest；稳定诊断 key
  同时绑定 canonical `WorkspaceIdentity` 与 thread identity。BUSY 在 SQLite preflight、graph
  factory 和编辑/verification side effect 前返回。

### TDD 验收切片

- [x] 补齐真实 durable subprocess：同 workspace 不同 runtime、不同 workspace 同 runtime/thread、
  不同 workspace/thread 并发、正常释放、强制终止后双锁重入、CLI BUSY JSON/exit 和 Windows
  canonical/link 边界回归。
- [x] 运行全量 unittest/pytest、compile、11 个 prompt render、root/start/resume CLI help 和
  `git diff --check`，确认现有 hash、checkpoint、event、artifact、budget 契约未改变。

### 已知边界

- 这是 cooperative-process serialization，只约束使用本 seam 的进程；不阻止第三方程序直接
  写 workspace，不实现 S3.5b read/write policy、`.git` 保护、sandbox、worktree、租约服务或
  分布式锁。workspace lock 位于进程共享的受控临时目录，runtime/thread lock 保留在
  `runtime_root/locks`；两处目录和最终 lock file 都做 canonical containment、symlink/junction
  和 fd 级 hard-link count 校验。hard link 拒绝依赖平台提供的 inode/link-count 语义；平台不
  支持创建 hard link 时只记录 skip，不扩大保证。workspace shared lock 只保证同一主机、同一
  admission namespace 内使用本 seam 的 cooperative processes，不是 OS sandbox。残留 lock
  文件可存在，但 OS 持有的 byte-range lock 会在进程退出时自动释放，不会因文件残留永久 BUSY。

### 当前状态

D7/S3.5a 本地实现与验收完成：unittest 298 tests OK / 10 skipped；pytest 288 passed /
10 skipped / 169 subtests passed。两层锁覆盖不同 runtime_root 的同 workspace、不同 workspace
同 runtime/thread、不同 workspace/thread 并行、正常释放和强制终止后重入；start/resume 在
SQLite preflight 前返回 BUSY，CLI 映射为 JSON exit 2。最终 lock file symlink/junction 及
preexisting hard link 在 open 后写入前结构化拒绝，外部 target bytes/mtime 保持不变，第二把
锁失败时第一把已取得的锁释放。compile、11 个 prompt render、root/start/resume CLI help 和
`git diff --check` 通过；hard-link/link 能力受平台限制时如实记录 skip。没有 GitHub CI 或独立
外部测试证据。

## D7/S3.5b：可信读写授权与工作区版本证据

### 固定范围

- 基线：`8c59cc1f500b6e3b8682df3284fc1daf4ce53e49`。
- `WorkspaceAccessPolicy` 是来自可信 `RunConfig` 的不可变值对象。它复用
  `WorkspacePathResolver` 的 canonical relative-path 边界，固定保护 `.git`、常见凭据路径，
  并保护配置的 hidden/oracle path；读取和写入分别返回结构化 `READ_DENIED` / `WRITE_DENIED`。
- policy 配置与模型路径统一归一 `oracle`、`./oracle`、`workspace_repo/oracle` 等等价拼写；
  model-visible regular file 若 `st_nlink != 1` 则保守拒绝，raw/codemap/search/tree、
  WorkspaceEditor 和 Developer 均不读取 hard-link alias 内容，也不泄漏受保护源路径。
- Architect/Developer 的 search、codemap、tree/read 工具在同一 workspace policy context 下执行；
  Developer 计划目标、`WorkspaceEditor.begin` 和 `commit` 也执行 write policy。受保护目录在 tree/search
  中静默跳过，直接访问返回不含受保护路径的 denial；verification runner 保持可信 checks 的独立执行边界，
  因而仍可读取 protected oracle。
- policy 值进入 semantic config digest，schema 从 v3 升至 v4；旧 checkpoint 或 resume 时更换 policy
  通过 digest mismatch fail closed。policy 不进入模型可修改的图状态。

### Workspace revision contract

- admission lock 获得后采集 Git HEAD、dirty 标记、status digest 和 `git diff HEAD` digest；命令使用 argv、
  `shell=False` 和有界 timeout，持久化只保留 digest，不保存 raw diff。非 Git、Git 不可用或查询失败统一返回
  结构化 `UNKNOWN` 原因。
- RunRecord schema 升至 v2，记录 admission 内的 start/end workspace revision；不执行 reset/checkout，
  不覆盖用户修改。v2 revision 必须是完整 known/structured-unknown shape，损坏记录拒绝覆盖；合法 v1
  legacy record 仍可读取。revision 是证据摘要，不是 recovery 或 workspace identity 的强唯一保证。
- resume 在 graph factory 前读取 SQLite root checkpoint；旧 semantic schema v3 paused checkpoint 在当前
  schema v4 下返回 `run_config_mismatch`，不触发 graph/model/verification side effect。

### TDD 验收切片

- [x] plan/read/commit 对 `.git`、credential、hidden/oracle path 一致拒绝；tree/search/codemap 不泄漏受保护
  名称或内容；Developer protected plan target 在模型工作前拒绝；trusted verification 可读取 oracle；
  hard-link alias 读取/编辑拒绝且源 bytes/mtime 不变。
- [x] policy env/config digest 与 resume mismatch seam；clean、dirty、non-Git、Git failure workspace revision；
  durable RunRecord start/end revision、严格 v2 shape 与 v1 legacy 读取；旧 v3 SQLite resume 零图副作用。
- [x] 全量 unittest/pytest、Python compile、11 个 prompt render、root/start/resume CLI help 和
  `git diff --check` 通过。

### 已知边界

- policy 是 cooperative model/tool boundary，不是 OS sandbox、通用 ACL/RBAC 或第三方进程写保护；
  verification checks 仍是可信外部配置，D3 oracle 不受 model read policy 限制。hard-link 拒绝依赖平台
  提供的 `st_nlink`/hard-link 语义；平台不能创建 hard link 时测试只记录 skip。
- revision digest 不保存 diff 内容；Git 查询失败或非 Git workspace 只记录 UNKNOWN；同路径整体替换、
  第三方并发修改和 S3.4 pending-write/hash recovery 仍不在本切片承诺范围。v1 仅保留历史读取语义，
  不把旧记录升级为新的 revision 证据。

### 当前状态

D7/S3.5b Final Seal 本轮本地实现与验收完成：unittest 319 tests OK / 10 skipped；pytest 309 passed / 10 skipped /
187 subtests passed。新增 protected path metadata non-disclosure、compiled ToolNode policy、严格 RunRecord
v1/v2 persisted shape、旧 semantic v3 resume 和 clean→dirty revision lifecycle 回归通过；旧
S1/S2/S3.1/S3.2/D3-D7a 测试保持通过。compile、11 个 prompt render、root/start/resume CLI help 和
`git diff --check` 通过；这些只是 Codex 本地证据，没有 GitHub CI 或独立外部测试证据。

## D5/S3.4a：文件写入恢复对账

### 固定范围与公开契约

- 基线：`ba9f8dd1fdb8d76afe924ee0e491d3b1f04f3fa6`；本轮只实现文件写入恢复，
  不进入 S3.4b verification attempt/replay，不引入跨文件事务、sandbox、worktree 或新依赖。
- `WriteIntent` 是 checkpoint-safe、versioned 的值对象，只保存稳定 `write_id`、run/task identity、
  task index、repair attempt、canonical workspace-relative path、operation、是否原先存在、before hash、
  expected-after hash 和 task ids，不保存文件内容。`write_id` 只由确定性身份字段计算，恢复不会随机重建。
- durable Developer 在任何实际 `WorkspaceEditor.commit` 前先 checkpoint `pending_write`；净零 existing
  transaction 在此之前直接返回 `NOOP`，保留 before/after hash、空 diff、全部 task ids，且不创建 intent，
  同时将 Developer 封为 `FAILED` 并保持 task index 不推进。
  非 durable 入口继续沿用原有直接 commit 行为。
- `RecoveryReconciler` 只读当前文件并返回 `SAFE_TO_APPLY`、`ALREADY_APPLIED` 或 `CONFLICT`：
  current==before/不存在时才允许沿现有 editor commit seam 写入；current==expected-after 时补
  `EditResult(APPLIED)` 且不再次写盘；第三值、缺失/意外存在、路径或 policy/link 边界变化均保留 intent 并 fail closed。
- 成功 commit 或 `ALREADY_APPLIED` 的结果先 checkpoint，再由独立 clear node 清除对应 intent，随后才推进
  task。repair attempt 使用新的确定性 identity；EventSink/RunRecord 只作 audit evidence，恢复只依据
  checkpoint 与当前 filesystem bytes。跨文件 atomicity、filesystem exactly-once 和第三方并发写保护不承诺。

### 版本与 TDD 验收

- semantic config digest 从 v4 升为 v5；真实 SQLite 中旧 v4 paused checkpoint 在当前版本 resume 前
  返回 `run_config_mismatch`，不触发 graph factory、model、write 或 verification side effect。
- [x] 公开 editing seam 覆盖 edit/create 的 SAFE→ALREADY、第三值 conflict、intent 无内容序列化、
  空文件 create、durable net-zero 无写盘/无 intent。
- [x] 真实 SQLite + subprocess/compiled child seam 覆盖 intent checkpoint 前后、edit/create 写前写后、
  commit result 已保存但 intent 尚未清除、repeated resume、多文件部分完成（只恢复 pending 文件）、
  第三方第三值和目录 link 冲突；恢复结果、最终 bytes、pending 清除与模型调用日志均通过可观察行为验收。
- [x] 全量 unittest 337 tests OK / 10 skipped；pytest 327 passed / 10 skipped / 189 subtests passed；
  Python compile、11 个 prompt render、root/start/resume CLI help、`git diff --check` 通过。10 个 skip
  仅为当前 Windows 普通 symlink 或目录 symlink 权限限制；以上均为 Codex 本地证据，不是 GitHub CI 或独立外部验收。

### 已知边界

- S3.4a 只对文件写入窗口做 conservative reconcile；verification 命令完成而 checkpoint 未保存的执行恢复、
  artifact replay、pending-write 之外的任意副作用恢复留给 S3.4b 后续切片。
- `ALREADY_APPLIED` 只证明当前 canonical path bytes 等于 expected-after hash，不证明写入者或历史过程；
  同路径整体替换仍受 path-only identity 限制。intent 冲突保留待处理状态，不静默清除或覆盖用户文件。

## D5/S3.4a Final Seal Fix

- 基线：`5102303f8102ea25ebf3b1ba544eaa8b20654b4b`；semantic schema 继续保持 v5，未引入新的持久化字段版本。
- durable net-zero 继续返回 `EditStatus.NOOP`、不创建 intent、不写盘、不推进 task，同时 Developer 和 parent
  终态封为 `FAILED`，不留下 `PENDING/RUNNING`。
- staged working content 无法 UTF-8 编码时返回 `REJECTED/ENCODING_ERROR`，不创建 intent、不写盘，原文件 bytes 保持不变。
- commit/reconcile 前校验 run/task identity、task index、repair attempt、canonical path、operation 和稳定 `write_id`；
  不匹配保留 pending intent，返回结构化 `RECOVERY_CONFLICT`，零写入、零推进。repair attempt 使用新的稳定 identity。
- 真实 SQLite/subprocess 与 compiled child seam 覆盖 intent checkpoint 前后、create/edit 写前写后、commit result checkpoint
  后清除前、重复 resume、多文件部分完成、第三值、目录 symlink/junction 和 identity 回归。以上只是 Codex 本地证据，
  10 个 Windows 链接能力用例跳过；没有 GitHub CI 或独立外部验收证据。

## D5/S3.4a Recovery Matrix Final Seal

- 基线：`94baffead0342f03873f8c2991700f8690ced56d`；本轮只补验收矩阵，semantic schema 保持 v5，未重构 recovery 生产架构。
- [x] 真实 SQLite + compiled Developer seam 构造 checkpointed pending intent，使用不同 task index 生成稳定但过期的 identity；resume 返回结构化 `RECOVERY_CONFLICT`，保留 intent，task index 不推进，model/tool 调用不重复，文件 bytes/mtime 不变。
- [x] pending intent 后将目标父目录替换为目录 symlink；当前 Windows junction 能力可用时实际覆盖。resume fail closed 为 `RECOVERY_CONFLICT`，pending 保留，外部目标 bytes/mtime 不变，未发生 workspace 写入。
- [x] 真实 parent → Developer → verification regression/repair 流程在 repair `prepare_write_intent` 后暂停；关闭并重开 SQLite 后读取子图 checkpoint，证明 repair attempt 为 1、`write_id` 与原始 edit 不同且稳定，随后仅完成预期 repair 写入并通过 post verification。
- [x] 最新 Codex 本地验证：unittest 339 tests OK / 10 skipped；pytest 329 passed / 10 skipped / 189 subtests passed；Python compile、11 个 prompt render、root/start/resume CLI help、`git diff --check` 均通过。10 个 skip 是既有 Windows 链接能力或平台边界限制；没有 GitHub CI 或独立外部验收证据。
- 本轮仍不承诺跨文件原子性、filesystem exactly-once、verification recovery/replay 或 S3.4b；checkpoint 与当前文件状态仍是恢复判定依据。

## D5/S3.4b：verification execution recovery

### 固定范围与版本

- 基线：`02ff14888ba5425ad34155c890c35dbc95900dc6`；本轮只增加 verification execution recovery，
  不进入 S2d、S4、sandbox/worktree、跨文件事务或 exactly-once。
- semantic config digest 从 v5 升为 v6，旧 v5 paused checkpoint 在 graph、command 或 verification
  前通过 durable preflight 返回 `run_config_mismatch`；non-durable `VerificationRunner` 与原有 public
  controller seam 保持兼容。
- `VerificationAttempt` 是 checkpoint-safe、versioned 的 per-check boundary，保存稳定 attempt identity
  （run/task、baseline/post、repair attempt、spec index/name、attempt ordinal）、trusted spec digest、
  输入 `WorkspaceRevision` 摘要、状态和 `VerificationSummary`（含已有 artifact refs、digest 和截断标志）。

### 恢复规则

- 每个 check 先由独立 STARTED graph node 持久化，再进入现有 `VerificationRunner`；结果由下一节点写成
  RESULT_RECORDED 后才进入下一个 check。结果已记录时 resume 只复用 checkpoint，不再次启动命令。
- 只有 STARTED 而没有结果时，新的 controller 没有 process-local dispatch arm，resume 保守返回结构化
  `OUTCOME_UNKNOWN` 并停止，不根据 command/name/exit code 猜测幂等性。trusted recovery policy 默认为
  `STOP_ON_UNKNOWN`；`RERUN_ISOLATED` 在当前没有可重建隔离执行环境时由 RunConfig fail closed。
- baseline、post 和 repair verification 复用同一 per-check seam；spec index 独立记录，repair attempt
  产生新的稳定 attempt identity。timeout、artifact、secret filter、deadline 和 D3 acceptance 语义保持原契约。

### TDD 验收与当前证据

- [x] 真实 SQLite + subprocess side-effect marker：命令副作用发生后进程退出且 result 未记录时，
  resume 不重跑、marker 保持 1、返回 `OUTCOME_UNKNOWN`；RESULT_RECORDED 后中断只复用结果；多个
  specs 按 index 独立持久化，已完成 check 不重复。
- [x] 真实 parent → Architect → Developer → baseline/post/repair 流程证明三类 verification 使用同一
  durable seam，repair attempt 的 identity 稳定且与初次 post 不同；旧 v5 checkpoint 迁移门槛和 policy
  rerun unsupported 均有回归。
- [x] 最新 Codex 本地验证：unittest 345 tests OK / 10 skipped；pytest 335 passed / 10 skipped /
  189 subtests passed；Python compile、11 个 prompt render、root/start/resume CLI help、`git diff --check`
  通过。10 个 skip 是既有 Windows 链接能力或平台边界限制；这些是 Codex 本地证据，不是 GitHub CI
  或独立外部验收。

### 已知边界

- 当前只保证“已持久化结果不重跑”和“未知结果保守停止”；没有 isolated execution seam，因此不支持
  自动 rerun，也不承诺 exactly-once、命令副作用回滚或 verification replay。
- `VerificationAttempt` 的 workspace revision 是输入证据摘要，不是执行环境快照；外部命令对 workspace
  的其它副作用仍不由本切片恢复。EventSink/RunRecord 继续是审计旁路，checkpoint 与当前运行状态才
  决定恢复，不以事件记录驱动重放。

## S2d/D3：受控多文件 repair

### 固定范围与公开契约

- 基线：`554411af3480d44652aa9da98fe12b753d76c6ec`；本轮只增加受控多文件 repair，保持
  Architect → Developer 主链、S1 workspace transaction、S3.4a WriteIntent 和 S3.4b
  VerificationAttempt，不进入 S4、sandbox/worktree 或跨文件事务。
- `CommittedEdit` 是 checkpoint-safe 的 versioned 值对象，只保存稳定 `write_id`、run/task identity、
  task index、repair attempt、canonical path、operation、before/after hash、task ids 和 patch digest；
  不保存源码或完整 patch。Applied commit 记录以 write_id 幂等追加，resume 不重复追加。
- `RepairScopePolicy.LAST_FILE` 是默认策略，保持原有只修最后成功提交文件的行为；可信 RunConfig 显式配置
  `COMMITTED_PLAN_FILES` 时，repair 只按原 Architect plan 顺序选择已经成功 committed 的文件。未提交、
  原计划外和模型/verification 输出指定的路径均不能扩大范围；每个文件继续独立 transaction，不承诺跨文件原子性。
- repair history 保存结构化 failure signature `(check_id, case_id, status)` 和按本次 scope 计算的 patch digest。
  相同可信 signature 与相同有效 patch digest 连续出现时提前返回 `REPAIR_EXHAUSTED`；缺失/畸形结构化证据
  不猜测归因，最多两轮 repair、全局 budget/deadline 不重置。
- semantic config digest 已从 v6 升至 v7；旧 v6 paused checkpoint 在 graph/model/write/verification
  前返回 `run_config_mismatch`。checkpoint 仍是恢复依据，EventSink/RunRecord 仅作审计旁路。

### TDD 验收与当前证据

- [x] CommittedEdit 序列化只含 hash/digest、稳定 identity 并支持重复去重；普通图和真实 SQLite
  parent → Developer → repair 均保留 committed edits，repair write_id 与原 edit 不同且稳定。
- [x] LAST_FILE 与 COMMITTED_PLAN_FILES 图级回归：首个文件保持 byte 内容，最后文件可修复；显式范围按原
  计划顺序修复已提交文件，未提交计划文件不会进入 repair；单文件失败不越界推进。
- [x] 结构化 failure signature 忽略 stdout/stderr 噪声；相同 signature + patch digest 提前停止，
  signature 改变仍允许下一轮；未配置/不可信 evidence 不做停滞猜测；两轮上限、budget/deadline 与
  S3.4a/S3.4b identity 回归通过。
- [x] 最新 Codex 本地验证：unittest 355 tests OK / 10 skipped；pytest 345 passed / 10 skipped /
  190 subtests passed；Python compile、11 个 prompt render、root/start/resume CLI help、
  `git diff --check` 均通过。10 个 skip 是既有 Windows 链接能力或平台边界限制；以上是 Codex 本地证据，
  没有 GitHub CI 或独立外部验收证据。

### 已知边界

- repair scope 是 trusted RunConfig 值，不是模型可修改的权限；policy 仍是 cooperative boundary，不是
  OS sandbox。CommittedEdit 只证明 checkpoint 中的 hash/digest 证据，不提供跨文件 atomicity、filesystem
  exactly-once、verification replay 或第三方并发写保护。stagnation 只在结构化报告可验证且 patch digest
  可取得时触发，不能从 failure_id 或截断日志推断归因。

## D8/S4a：工具消息完整传递

### 固定范围与公开契约

- 基线：`5ed85f9dc8b9c49ed197aff119e141c382a8bc76`；本轮只增加 Architect/Developer 共用的纯工具消息 renderer，
  不进入 S4b 搜索限额、S4c symbol/repo map、历史摘要或新消息总线。
- 公开 seam 为 `agent.common.tool_message_renderer.render_tool_messages(messages)`；Architect 与 Developer
  只通过该 renderer 生成送入模型的研究上下文，旧 `convert_tools_messages_to_ai_and_human` 名称仅保留兼容别名，
  不再各自维护实现。
- renderer 索引全部 AI tool calls，再按 `tool_call_id` 确定性配对 ToolMessage；保留 call id、工具名、arguments、
  结果内容及已有 path/hash/truncated/status 元数据。missing、duplicate、unknown id 和工具名不一致均输出明确
  `INCOMPLETE TOOL EXCHANGE` 标记，不静默配对；结果内容带 `UNTRUSTED EVIDENCE` 标记，失败工具结果同样保留。
- renderer 只影响模型上下文适配，不改变 ToolNode 原始消息、durable tool reservation/settlement、D6 audit 或
  checkpoint state；没有持久化 contract 变化，semantic schema 保持 v7。

### TDD 验收与当前证据

- [x] 纯 renderer 覆盖空/普通消息、0/1/多调用、乱序返回、missing/duplicate/unknown id、工具失败、伪指令内容，
  并保留结构化元数据。
- [x] compiled Architect 与 Developer graph 使用两个真实 ToolNode 工具调用回归：两次调用的 id、参数和结果都进入
  下一次模型上下文；现有 protected-path policy 测试改为通过不可信证据公开 seam 验收。
- [x] 最新 Codex 本地验证：unittest 357 tests OK / 10 skipped；pytest 351 passed / 10 skipped / 190 subtests
  passed；Python compile、11 个 prompt render、root/start/resume CLI help、`git diff --check` 均通过。10 个 skip
  是既有 Windows 链接能力或平台边界限制；这些是 Codex 本地证据，不是 GitHub CI 或独立外部验收。

### 已知边界

- renderer 只提供上下文完整传递和确定性错误标记，不提供新的 tool replay、消息持久化、搜索限额或 repo map 能力；
  ToolNode 原始状态仍由 LangGraph 管理，工具/仓库/测试结果仍是不可信证据。

## D9/S4b：有界文本检索与按需读取

### 固定范围与公开契约

- 基线：`568d4abb1fe41f9f1ff6dee1d1f279282fb96fed`。保留原工具名、workspace resolver、trusted read policy、
  symlink/junction/hardlink 边界和 S4a untrusted evidence renderer；不进入 S4c。
- `search_keyword_in_directory` 支持显式列出的 UTF-8 源码/配置后缀，包括 Python、JavaScript/TypeScript、JSON、
  YAML、TOML、INI、XML、HTML/CSS、SQL、shell 与 Markdown/text。固定上限为 128 个候选文件、100 条结果、
  1 MiB 累计扫描字节；单次返回的结果证据也限制在 1 MiB。短查询、二进制、非 UTF-8、访问拒绝、扫描错误以结构化
  错误或 warnings/skipped_files 返回。
- search 与 tree 共用 `.git`、依赖目录、缓存和构建产物的 ignore 名称集。protected 名称在 filesystem metadata 读取前排除；
  文件仍经过现有 resolver 与单硬链接检查。
- `search_keyword_in_directory` continuation 绑定 canonical directory、search term、context、确定性搜索版本、候选文件序号/行号及
  本页文件/目录 stat evidence；大文件按有界字节窗口继续读取，游标只保存 byte offset/line 状态，不保存源码片段。查询选项不匹配或
  游标携带的 evidence 变化时返回 `stale_cursor`。每页分别受 128 候选文件、100 结果、1 MiB 扫描字节与 1 MiB 返回 evidence 上限约束；
  正常续读不重复/遗漏下一段，耗尽后 `truncated=false` 且 continuation 为空。常规文件结果的 `content_hash_scope=file`；大文件分段结果
  使用 `content_hash_scope=scanned_segment`，只摘要匹配所在扫描片段。
- `get_raw_file_content` 用 UTF-8 byte range 分页，每页最多 32 KiB；返回 canonical path、range、page content hash、
  content-version token、truncated、warnings、continuation。cursor 绑定 path、请求 range 与文件版本摘要；
  query/version 不匹配返回 `stale_cursor`。
- `get_files_structure` 固定最大 depth 8、每页 128 项、最多扫描 4096 项；cursor 绑定 canonical directory、depth、
  page size 和 inventory digest。目录变化或参数变化时拒绝旧 cursor，返回结构化 stale；不会读取文件内容。
- 这些限制是代码常量，不是 RunConfig/resume 语义；semantic config schema 保持 v7，未修改依赖。

### TDD 验收与当前证据

- [x] 新增公开工具回归覆盖 JS/TS/TOML 与 Unicode、短查询/空结果、二进制/非 UTF-8、ignore 目录、文件/结果/字节上限、
  超长行/扫描字节预算续读、结果上限和文件上限续读无重复遗漏、每页硬上限、search cursor query/context/directory/evidence stale、
  扫描错误、raw byte range、多页 UTF-8 拼接、tree depth/entry 上限与 stale。
- [x] workspace boundary、protected-path/hardlink policy、S4a renderer 和 graph integration 回归通过。
- [x] Codex 本地全量验证：unittest 377 tests OK / 10 skipped；pytest 371 passed / 10 skipped；
  compile、11 prompt render、root/start/resume CLI help、`git diff --check` 通过。
  pytest 输出 1 条既有 tree-sitter FutureWarning；10 个 skip 是既有 Windows 链接能力或平台边界限制。
  没有 GitHub CI 或独立外部测试证据。

### 已知边界

- 支持文件类型由后缀白名单定义，不是任意文本探测。search continuation 是无状态位置游标，不缓存全量结果；每次调用会确定性重走目录项前缀以定位候选序号，实际读取/处理的候选文件、扫描字节、结果数和返回 evidence 仍受每页硬上限约束。游标校验其携带的本页文件/目录 stat evidence，不是整个 workspace 的强快照。跨页大文件的单条结果以 `content_hash_scope=scanned_segment` 标明摘要只覆盖匹配所在扫描片段；该路径无法提供跨页上下文行时会返回 `chunked_file_context_limited` warning。
- raw cursor 的文件版本使用可移植 stat 元数据摘要，不是全文件快照或强并发锁；每页 `content_hash` 只覆盖返回的 page bytes。
- tree 目录项扫描预算固定为 4096；达到预算后最多进行常数级额外探测，以判断当前目录或待处理目录是否仍可能有未扫描内容。探测发现额外目录项，或有限探测后仍有未检查的待处理目录、无法证明扫描已完整时，返回 `truncated` / scan-limit warning；未扫描后缀没有 continuation。protected path、path/link 边界及 OS 层隔离契约保持不变。

### D9/S4b Final Seal follow-up：production tree completeness evidence

- 基线：`1cee4b0603be850286d1769d14785dc6882ef49c`（S5a 已通过此前 ChatGPT Final Gate）；本 slice 开始前确认该 HEAD 已同步 upstream，branch/upstream 正确、工作树 clean、ahead/behind 0/0。全项目审计重新打开 S4b，本轮只修 production tree evidence 传递，不进入后续优化。
- Architect 与 Developer 共用 `agent/common/workspace_tree_evidence.py` 确定性 renderer；两个 production runtime loader 将 `get_files_structure()` 的结构化结果传入模型上下文。成功树明确显示 `COMPLETE` 或 `INCOMPLETE (TRUNCATED)`、truncated 标记、path/hash、现有 range/warnings/limits 与 `continuation_available`；opaque cursor 不进入上下文，也不自动读取下一页。失败树显示 `FAILED`、结构化 error code/message 和已有诊断字段，失败 path 不回显。
- 真实 production loader/compiled graph 回归使用 `default_architect_runtime()` 与 `default_developer_runtime()` 的 loader：130-file workspace 的首屏在两个 Agent 模型输入中均显式 incomplete；小型完整 workspace 保持 complete；扫描失败保留 scan_failed；hidden oracle 路径与 canary 不进入 tree 或其 metadata。未修改 tree enumeration、scan/sort 方式、hard caps、resolver、policy、link/hardlink 规则、依赖或 semantic schema。
- [x] Slice 专项 4 个 compiled graph unittest 通过；受影响 Architect/Developer、D9 bounded tree 与 access-policy 子集 pytest 56 passed / 5 subtests passed。
- [x] Codex 本地全量验证：unittest 425 tests OK / 10 skipped；pytest 419 passed / 10 skipped / 216 subtests passed；compileall、`git diff --check` 通过。pytest 有 25 条锁定 Tree-sitter API FutureWarning；10 个 skip 是既有 Windows 链接能力或平台限制。未运行 GitHub CI 或独立外部验收。
- 此 tree evidence slice 已纳入后续审核并通过 ChatGPT Final Gate；tree filesystem enumeration slice 也已通过审核并 push，详见下一节。

### D9/S4b Final Seal：tree filesystem enumeration hard bound

- 基线：`25e0222d57e606a48de5ae16aaaa527c3348a61f` 的 tree evidence slice 已通过 ChatGPT Final Gate 并 push；开工前确认 branch/upstream 为 `review/step3-runtime-recovery` / `origin/review/step3-runtime-recovery`，工作树 clean、ahead/behind 0/0。本 slice 只处理 tree enumeration，不修改 search enumeration。该 tree enumeration 提交 `436e03e926ee8a7505066312bd74b6fdb63defc3` 后续已通过 ChatGPT Final Gate 并正常 push。
- `_bounded_tree_entries()` 现在通过增量 `os.scandir` 读取，最多保留/计入 4096 个目录项；最多额外消费一个 `DirEntry` 作为超限确认，不再由 `os.walk()` 先物化完整 `directories/files` 或排序未处理后缀。ignored、protected、unsafe、link/junction、hardlink 等目录项都在过滤前计入预算。触限时不继续扫描以获得全局排序，返回 `truncated=true` 和 `tree_scan_entry_limit_reached`；未扫描后缀不能续读。未触限完整 tree 仍按原路径排序并沿用分页、range/hash/cursor/stale 行为。resolver、policy、tree evidence renderer、Architect/Developer 行为和 semantic schema v7 未改。
- [x] TDD 公开工具回归：受控超大单目录只消费 4096 项加一个确认项；大量 `.git`/oracle/link 条目仍耗尽预算并触发截断。真实 Architect production loader/graph 收到 incomplete 标记、warning、range、limits 与 `continuation_available=false`，不泄漏 protected 名称。
- [x] 专项回归：`tests/tools/test_bounded_reads.py`、`tests/runtime/test_access_policy.py`、`tests/test_graph_integration.py` 合计 40 passed。
- [x] Codex 本地全量验证：unittest 427 tests OK / 10 skipped；pytest 421 passed / 10 skipped；`python -m compileall -q agent tests` 与 `git diff --check` 通过。pytest 的 25 条 FutureWarning 来自锁定 Tree-sitter 旧 API；skip 是既有链接能力或平台边界限制。没有 GitHub CI 或独立外部测试证据。
- 本 tree enumeration slice 已通过 ChatGPT Final Gate 并 push；本节所列 Codex 运行结果是提交时的本地证据，不代表 GitHub CI 或独立外部验收。

### D9/S4b Final Seal：search filesystem enumeration hard bound

- 基线 `436e03e926ee8a7505066312bd74b6fdb63defc3` 的 tree enumeration slice 已通过 ChatGPT Final Gate 并 push；开工前确认 branch/upstream 为 `review/step3-runtime-recovery` / `origin/review/step3-runtime-recovery`，working tree clean、ahead/behind 0/0。本轮只处理 search enumeration，不修改 tree traversal、raw read、symbol adapters、workspace revision、依赖或 semantic schema。
- 新增独立固定 `MAX_SEARCH_SCAN_ENTRIES=4096`。search 以增量 `os.scandir` 读取，每次调用实际保留/计入不超过 4096 个目录项，最多再消费一个条目确认未扫描后缀；目录、ignored、protected、unsupported、link/unsafe 条目均在过滤前计入。上限通过公开 `limits.max_scanned_entries` 暴露；仅额外探测确认存在未扫描后缀时返回 `truncated=true` 与 `directory_scan_entry_limit_reached`。
- ChatGPT Final Gate 指出恰好读取 4096 项并由 lookahead 得到 EOF 时仍被标记 truncated；另一个缺口是跨目录累计到 cap 后没有检查 pending 目录是否为空。本轮以公开工具失败测试复现并修正这两个边界。
- 每页固定 scan-entry budget 为 4096，最多消费 cap+1 个条目；过滤前计入目录、ignored、protected、unsupported、link/unsafe。只有额外探测实际得到一项未扫描后缀时才设置 `directory_scan_entry_limit_reached` / `truncated=true`。恰好 cap 且 EOF 时为完整；跨目录恰好 cap 后，对已枚举 pending directories 逐个探测首项/EOF：全部为空则完整，有内容则 truncated。scan cap 已确认有后缀时不发 continuation：现有无状态 candidate-index cursor 会重扫 prefix，无法在该边界证明稳定向前；调用方需从头缩小搜索目录/查询范围。未触 cap 或恰好完整到 cap 时，原 file/result/byte continuation、query 和 file/directory evidence stale binding 不变；小型完整 workspace 排序保持 root files、再按目录字典序深度优先。
- [x] TDD 公开工具回归：恰好 cap 单目录+EOF 完整；恰好 cap 跨目录且 pending 目录为空仍完整；非空 pending 目录产生 truncated；cap+1 只消费 cap+1、产生 warning/truncated 且无 cursor；巨大 unsupported 及混合 protected/ignored/unsafe-link/unsupported 条目受同一 hard bound 约束、不探测 protected metadata、不泄漏 protected 名称；完整小 workspace 顺序稳定。
- [x] `tests/tools/test_bounded_reads.py`、`tests/runtime/test_access_policy.py`、`tests/tools/test_workspace_boundary.py`、`tests/test_graph_integration.py` 合计 53 unittest 通过、1 skip；新增 exact-cap 单目录 EOF、跨目录 pending-dir 边界及 cap+1 专项 3 个公开 seam 测试通过。
- [x] Codex 本地全量验证：unittest 432 tests OK / 10 skipped；pytest 426 passed / 10 skipped / 216 subtests passed；`python -m compileall -q agent tests` 通过。pytest 有 25 条锁定 Tree-sitter API FutureWarning；skip 为既有 Windows link 能力或平台边界限制。未运行 GitHub CI 或独立外部验收。
- 本 search enumeration slice `39c8e3fccb9fc8ffa66c57968f2a573a5a1e2415` 已通过 ChatGPT Final Gate 并 push；本节所列 Codex 验证是提交时本地证据，不代表 GitHub CI 或独立外部验收。

## D10/S4c.1：Python 符号正确性

### 固定范围与公开契约

- 基线：`7ed6bc9ee41b0f5586c0da4c980d790afb59f945`；S4b Final Gate 已通过。本轮只处理 Python，不实现 JS/JSX/TS/TSX query、repo map 或 D11 context assembly。
- 保留 `get_code_definitions`、`get_function_implementation`、`get_code_definitions_multi` 工具名及现有 resolver/access-policy/hard-link 边界。Tree-sitter 0.21.3 与 tree-sitter-languages 1.10.2 保持锁定，无依赖升级。
- `agent.tools.python_symbols.extract_python_symbols` 返回结构化符号：canonical path、qualified symbol、kind、半开区间 byte offset、1-based 行号、原始节点切片 SHA-256 和源码文本。源码只通过 `source_bytes[start_byte:end_byte]` 切片，不从 signature/body 重建；覆盖 decorator、async、多行签名、嵌套定义和 Unicode byte offsets。
- 同名函数/方法按 `qualified_symbol` 唯一匹配；不唯一时返回 `ambiguous_symbol` 与候选。JS/JSX/TS/TSX 明确返回 `unsupported_language` 并提示使用 bounded text search。语法错误返回 `parse_error`，Tree-sitter 明确的 parser API `TypeError`/`ValueError`/grammar `KeyError` 返回 `tree_sitter_error`；意外运行时异常继续传播。实现直接遍历 Python grammar 节点，不编译 Tree-sitter query。
- 单个 Python 输入最多 1 MiB；单次最多 128 个符号、16 个多文件输入、131072 字节序列化结果。超限以 `truncated` 或结构化错误呈现。未改变 RunConfig/checkpoint schema v7，也未改变依赖。

### TDD 验收与当前证据

- [x] 公开工具用例覆盖 decorator、async、多行签名、嵌套 class/function/method、同名候选 ambiguity、Unicode、逐字节 source/hash/offset 校验、Python syntax/parser failure、JS/JSX/TS/TSX unsupported 和单文件/多文件输出上限。
- [x] 真实 compiled ToolNode 返回结构化符号消息并通过原有 Architect/Developer 工具与 policy 回归；workspace/access-policy、S1–S3 和 S4a/S4b 旧回归保持通过。
- [x] Codex 本地验证：unittest 387 tests OK / 10 skipped；pytest 381 passed / 10 skipped / 190 subtests passed；compileall、11 prompt render、root/start/resume CLI help、`git diff --check` 通过。pytest 有 8 条既有 Tree-sitter FutureWarning；10 个 skip 是既有 Windows 链接能力或平台边界限制。
- S4c.1 已通过 ChatGPT Final Gate。以上是该阶段提交时的 Codex 本地验证快照，不代表 GitHub CI 或独立外部验收。本轮以已审核 HEAD
  `b8fcb68e123fd19d4542fbf48155240926405d33` 为基线；开始编码前已正常 push 到
  `origin/review/step3-runtime-recovery`，并复核工作树 clean、ahead/behind 0/0。

### 已知边界

- 符号 query 只支持 Python `.py`，不提供其他语言 AST。Python source 超过 1 MiB、符号索引或序列化输出达到固定上限时返回结构化拒绝或截断；被截断索引不能用于证明函数名唯一。Tree-sitter 仍产生锁定版本的 FutureWarning。

## D10/S4c.2：JavaScript/JSX 符号正确性

### 固定范围与公开契约

- 基线：已审核的 S4c.1 HEAD `b8fcb68e123fd19d4542fbf48155240926405d33`。本轮只实现 JavaScript/JSX；不实现 TypeScript/TSX、repo map 或 D11 context assembly。
- 编码前在项目锁定环境实测 `tree-sitter 0.21.3` 与 `tree-sitter-languages 1.10.2`。`javascript` grammar 对 function/class/method/arrow/export 产生对应节点且无 parse error；嵌套 JSX 元素、属性表达式、自闭合元素和 fragment 也以 JSX 节点可靠解析。本轮不升级依赖。
- `agent.tools.symbol_contract` 提供 Python/JavaScript 共用的 `SourceSymbol`、`SymbolIssue`、`SymbolExtraction` value contract；`codemap.py` 使用静态 suffix→adapter 映射，不建立通用 registry。`.py` 继续走 Python adapter；`.js`、`.jsx`、`.mjs`、`.cjs` 走 JavaScript adapter；`.ts`/`.tsx` 明确返回 `unsupported_language` 并提示 bounded text search。
- JavaScript adapter 覆盖 function declaration、class/method、async function、嵌套函数、export 和变量绑定的 arrow function。symbol 的源码只取原始 bytes 的 Tree-sitter node range，输出 canonical workspace-relative path、qualified symbol、kind、半开 byte offsets、1-based line range、source hash 与 source。相同名称不唯一时仍结构化返回 `ambiguous_symbol`。
- 既有 resolver、read policy、hard-link 检查，以及 1 MiB source、128 symbol、131072 字节 source/output、16 文件等 hard caps 保持生效。parse error 与已知 parser/grammar API error 结构化返回；意外运行时异常继续传播。没有改变 semantic schema v7。

### TDD 验收与当前证据

- [x] JavaScript/JSX golden fixtures 覆盖 Unicode byte offsets、export、async、多行 method、嵌套 class/function、arrow function、JSX 元素/属性/表达式/fragment；逐字节验证 source、hash、byte range 与行范围。
- [x] 公开工具覆盖同名 ambiguity、TS/TSX unsupported、语法与已知 parser 错误、意外异常传播、entry/source/output/file caps；真实 compiled ToolNode 返回 JSX symbol evidence。S4c.1 Python 回归保持通过。
- [x] Codex 本地全量验证：unittest 395 tests OK / 10 skipped；pytest 389 passed / 10 skipped / 198 subtests passed；Python compileall、11 prompt render、root/start/resume CLI help、`git diff --check` 均通过。pytest 显示 14 条锁定 Tree-sitter 旧 API 的 FutureWarning；10 个 skip 为既有 Windows 链接能力或平台边界限制。
- S4c.2 已通过 ChatGPT Final Gate。以上是该阶段提交时的 Codex 本地证据，不代表 GitHub CI 或独立外部验收。

### 已知边界

- JavaScript/JSX 以锁定 grammar 为边界；只识别本 adapter 列明的声明与变量绑定 arrow function，不承诺完整 ECMAScript 语义、任意表达式求值或 TypeScript 类型解析。TS/TSX 始终 fail closed。Tree-sitter 仍报告其旧 `Language(path, name)` API FutureWarning。

## D10/S4c.3：TypeScript `.ts` 符号正确性

### 固定范围与公开契约

- 基线：已审核的 S4c.2 HEAD `c25094f5b383c625994c59ca49fd77018115eb07`。编码前已确认工作区 clean、upstream 为 `origin/review/step3-runtime-recovery` 且 ahead/behind 为 0/0；正常 push 成功后复核相同 HEAD 与 clean、0/0 状态。本轮只实现 `.ts`，不实现 `.tsx`、repo map 或 D11 context assembly。
- 在锁定的 `tree-sitter 0.21.3` 与 `tree-sitter-languages 1.10.2` 下实际探测 `typescript` grammar；函数、class、method、arrow、export、generic/type annotation 以及 `interface_declaration`、`type_alias_declaration`、`enum_declaration` 均可解析且 probe fixture 无 parse error。本轮未升级依赖。
- 复用 `SourceSymbol` / `SymbolExtraction`，并在 `codemap.py` 静态 suffix→adapter 映射增加 `.ts`；`.tsx` 保持 `unsupported_language` 并提示 bounded text search。
- TypeScript adapter 输出 function、class、method、interface、type_alias、enum 六类 symbol；支持 async/nested/export、变量绑定 arrow、类型注解和 generic 参数。interface 成员签名及 type expression 不作为运行时方法索引。源码、hash 和半开 byte range 均来自原始 UTF-8 bytes 的 Tree-sitter node range；返回 canonical path 与 1-based 行范围。重复名称按既有 seam 返回 `ambiguous_symbol`。
- syntax error、明确的 grammar/parser API 错误结构化返回；unexpected exception 继续传播。source、entry、serialized output、multi-file caps、resolver/access-policy/hard-link 边界沿用既有实现；semantic config schema 保持 v7。

### TDD 验收与当前证据

- [x] TypeScript golden fixture 覆盖 Unicode byte offsets、export/async、nested function/class/method、变量绑定 generic arrow、类型注解、generic function/class/method，以及 interface/type alias/enum；逐字节核对 source、hash、offset 与行范围。
- [x] 公开工具测试覆盖 ambiguity、parse/grammar error、unexpected exception、source/entry/output/file caps，并通过真实 compiled ToolNode 验证 `.ts` symbol evidence；Python 与 JavaScript/JSX 回归保持通过，`.tsx` 继续 fail closed。
- [x] Codex 本地全量验证：unittest 400 tests OK / 10 skipped；pytest 394 passed / 10 skipped / 208 subtests passed；Python compileall、11 prompt render、root/start/resume CLI help、`git diff --check` 均通过。pytest 有 19 条锁定 Tree-sitter 旧 API FutureWarning；10 个 skip 是既有 Windows 链接能力或平台边界限制。
- S4c.3 技术 Final Gate 已通过。以上是 Codex 本地验证证据，不代表 GitHub CI 或独立外部验收。作为该阶段历史事实，技术基线 `d86d1c8ecb08ba2e146518f1d0de784a7f01b095` 已同步 upstream。

### 已知边界

- 这里只解析 adapter 明确列出的 TypeScript 声明，不承诺完整 TypeScript 类型系统或语义；interface/type alias 的内部成员不拆成独立 symbol。`.tsx` 未实现。锁定 Tree-sitter Python binding 仍发出旧 `Language(path, name)` API FutureWarning。

## D10/S4c.4：TSX 符号正确性

### 固定范围与公开契约

- 基线：已审核并同步的 S4c.3 HEAD `c8b0840812a7b2f1d7d56450bdea908d623a7606`；开始时工作树 clean、upstream 为 `origin/review/step3-runtime-recovery`、ahead/behind 0/0。本轮只实现 `.tsx`，不进入 repo map 或 D11 context assembly。
- 在锁定的 `tree-sitter 0.21.3` / `tree-sitter-languages 1.10.2` 下实际探测独立 `tsx` grammar。golden TSX fixture 在 `tsx` grammar 下无 parse error；同一 fixture 在 `typescript` grammar 下有 parse error。本轮未升级依赖，也不把 `.tsx` 映射到 `typescript` parser。
- `codemap.py` 的静态 suffix→adapter seam 将 `.tsx` 映射到独立 `tsx_symbols.extract_tsx_symbols` adapter；adapter 调用 grammar 名称 `tsx`，共享既有 TypeScript declaration walker 的确定性范围提取逻辑和 `SourceSymbol` / `SymbolExtraction` contract。
- TSX 结果保留 canonical path、qualified symbol、kind、半开 byte offsets、1-based line range、source hash 与 source。源码直接取原始 byte range。golden fixture 覆盖 typed async function、generic class/method、generic arrow、export、JSX element/attribute expression、自闭合组件及 fragment；重复函数名 fail closed。仅报告这些 grammar-backed symbols，不声称完整 TypeScript 或 React 语义。
- parse/明确 grammar API 错误结构化返回，unexpected exception 继续传播；source/entry/output/file hard caps、resolver/access-policy/hard-link 和 S1/S2/S3 原契约保持。

### TDD 验收与当前证据

- [x] 先增加公开工具 golden 回归，确认旧实现返回 `unsupported_language`；随后实现独立 TSX adapter，逐字节验证 Unicode/source/hash/byte range/line range。
- [x] 覆盖 TSX grammar 与 TypeScript grammar 差异、ambiguity、parse/grammar error、unexpected exception、source/entry/output/file caps 和真实 compiled ToolNode；Python、JavaScript/JSX、TypeScript 回归保持通过。
- [x] Codex 本地全量验证：unittest 404 tests OK / 10 skipped；pytest 398 passed / 10 skipped / 214 subtests passed；Python compileall、11 prompt render、root/start/resume CLI help、`git diff --check` 通过。pytest 有 25 条锁定 Tree-sitter 旧 API FutureWarning；10 个 skip 为既有 Windows 链接能力或平台边界限制。
- S4c.4 已通过 ChatGPT Final Gate；以上数字是该阶段提交时的 Codex 本地证据，不代表 GitHub CI 或独立外部验收。S4c/D10 当前全部通过。

### 已知边界

- 这里只索引既有声明 walker 能识别的 TypeScript function/class/method、变量绑定 arrow 与声明类 symbols；JSX 标签本身不建立 symbol index，组件语义、JSX namespace/type checking 和完整 React 语义不承诺。Tree-sitter binding 仍报告锁定版本旧 API FutureWarning。

## D12/S5a：固定任务 manifest 与离线校验

### 本轮范围

- 基线：已审核 S4c.4 HEAD `3ebefa53da38152381a11bfe8d526cbb3bbe60c8`；开工前已正常 push 并核对 upstream、clean 与 ahead/behind 0/0。
- 本片只提供 versioned JSON manifest 和离线 validator；不运行真实模型或 check 命令，不实现隔离执行器、CI、S4d 或评分服务。
- 外部机制只借鉴固定 revision、分别记录目标测试与回归检查的思路；SWE-bench 固定源码 [grading.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py) 明确按 FAIL_TO_PASS / PASS_TO_PASS 结果分类。本项目 manifest 不复制其 harness 或评分逻辑，不将自建任务称为 SWE-bench Verified，也不据此报告解决率。

### 公开 contract 与数据来源

- `agent.evaluation.load_task_manifest` / `validate_task_manifest` 校验 schema v1、canonical JSON manifest hash、每个 task text hash、重复 ID、完整 40 位已存在 Git commit、目标仓库身份、allowed edit scope、baseline/target/regression 的 argv checks、oracle 文件 hash、环境摘要、预算与 split/stratum。校验 Git 只使用固定 argv、`shell=False`、5 秒 timeout；validator 不执行 manifest 中的任何 check。
- `evals/s5a/tasks.v1.json` 收录 4 个历史符号实现 acceptance seed，来源分别锚定到项目 S4c.1–S4c.4 的目标基线、实现 commit、源码变更范围和 pinned 测试/fixture SHA-256。task text 明确标为基于 commit subject 与测试归纳，不伪称原始 issue 文本。
- S5a Final Seal 基于 HEAD `006aa9318575c2541e5b3c86fa0d4b9a8819a651`：ChatGPT Final Gate 指出 TSX target `c8b0840...` 到 oracle `3ebefa5...` 的 pinned diff 包含 `agent/tools/codemap.py`，但 seed scope 漏掉该公开 dispatch 文件。已给 `symbols-tsx-exact-source-v1` 增加 `agent/tools/codemap.py` / `modify`；四个历史任务均有 curated scope 回归，oracle/tests evaluator-only，未将测试或 `task_plan.md` 加入 editable scope。
- 修正后的 manifest canonical SHA-256：`7274b2769b39a242c0b8a4005e86bf92b842532c2f80117eb3ffe57df67b9253`。schema v1 的 checks 限定为 `python -m unittest` + 显式测试模块；这是离线校验 contract，不是执行器支持范围声明。
- 当前素材没有独立仓库/issue 任务集，也没有可证明独立性的 holdout；因此只冻结 4 个来源可追溯 seed（2 train、2 dev，按语言分层），不伪造 20 项或 holdout。oracle 仅声明 evaluator-only；本片没有隔离执行器，不能保证测试路径对未来 Agent 运行时不可见。
- 每项预算固定 max_steps=100、deadline=1800 秒、max_cost=2 美元；成本 cap 是未实测的试点候选，不是质量/成本校准结果或硬上限。当前没有运行真实模型、任务求解、CI 或独立外部验收。
- manifest SHA-256 用于发现未同步更新的内容，不是签名；真实性仍依赖受审查的 Git 配置。validator 是离线 library seam，不执行 argv checks，也没有 Agent 接入，因此 `evaluator_only_declared` 只是可信配置声明，不能证明运行时隔离。
- [x] 公开 seam 覆盖合法来源清单、缺字段、任务/manifest hash tamper、重复 task id、scope/oracle 冲突、浮动/未知 revision、非法路径与 argv、仓库/环境不匹配、oracle 文件 hash 不匹配、重复 JSON key、四个历史任务的 curated implementation scope，以及 pinned target→oracle diff 之外的路径拒绝（`scope_not_in_change`）。
- [x] Final Seal Codex 本地验证：manifest 专项 unittest 17 tests OK；pytest 17 passed / 2 subtests passed；全量 unittest 421 tests OK / 10 skipped；全量 pytest 415 passed / 10 skipped / 216 subtests passed；compileall、`git diff --check` 通过。pytest 的 25 条 FutureWarning 来自锁定 Tree-sitter 旧 API；10 个 skip 是既有 Windows 链接能力或平台限制。未运行 GitHub CI 或独立外部验收。
- S5a Final Seal 已通过此前 ChatGPT Final Gate 并已同步 upstream；D9/S4b tree evidence、tree enumeration 与 search enumeration 的最终审核及同步状态见本文件顶部实时基线和各自 slice 记录。

## D7/S3.5b Final Seal：workspace revision bounded Git evidence

- 基线：S4b search enumeration 最终 repair `39c8e3fccb9fc8ffa66c57968f2a573a5a1e2415` 已通过 ChatGPT Final Gate 并 push；本轮开工核验 branch/upstream 正确、HEAD 与 remote-tracking 一致、工作树 clean、ahead/behind 0/0。全项目审计重新打开 S3.5b 的原因是 `detect_workspace_revision()` 对 porcelain status 与 binary diff 仅设 timeout，仍通过 `PIPE` 完整捕获 stdout/stderr，内存没有 byte hard bound。
- 本轮只改 workspace revision Git 输出边界：status、HEAD 与 diff 均通过双 pipe 增量读取；每次命令 stdout 上限 1 MiB、stderr 上限 64 KiB，最多额外读取一个字节确认 overflow。超限立即终止并回收 Git 进程树；overflow、timeout 与其他 query failure 返回 `UNKNOWN / QUERY_FAILED`，缺失 Git 保持 `GIT_UNAVAILABLE`，非 Git 保持 `NOT_GIT`；所有 UNKNOWN 的 head/dirty/status_digest/diff_digest 均为 `None`。只有各命令完整输出均落在上限内时才计算 evidence；dirty 只由完整 status 判定。argv、`shell=False`、workspace `-C` 保持。
- 进程清理复用既有 `ProcessTree`（POSIX process group、Windows bounded taskkill 与 Job Object backstop），没有新增 process framework。`detect_agent_code_revision()` 仍使用原 `_run_git()` 路径；Agent revision regression 通过。workspace revision 的 RunRecord v2 / semantic config v7 shape 未改变，也不持久化 raw status/diff。
- [x] revision 专项 unittest：9 tests OK；真实 Git 超限 status 与 4 MiB 随机 binary diff 均返回无部分字段的 UNKNOWN；受控公开 seam 证明 stdout 与 stderr 各最多读取 cap+1 并终止/回收进程；timeout 与已有 missing-Git/non-Git 路径通过。
- [x] durable revision、Agent revision、RunRecord/trajectory、access-policy/admission、verification runner/workflow/recovery 相关 unittest：132 tests OK / 6 skipped。真实 durable start/end RunRecord 保持 KNOWN，clean→dirty 的 HEAD/摘要证据变化正常；记录字段保持完整既有 shape，文件路径与原始内容未进入记录。
- [x] Codex 本地全量验证：unittest 436 tests OK / 10 skipped；pytest 430 passed / 10 skipped / 218 subtests passed；`python -m compileall -q agent tests` 与 `git diff --check` 通过。pytest 的 25 条 FutureWarning 来自锁定 Tree-sitter 旧 API。未运行 GitHub CI 或独立外部验收。
- 本 slice `eba39a66ba16c68cb5fba8f5dcf064646bd69918` 已通过 ChatGPT Final Gate 并已 push；以上只是 Codex 本地测试证据，不代表 GitHub CI 或独立外部验收。
