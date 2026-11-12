# 支持范围与验证状态

## 已实现

- Architect→Developer、确定性WorkspaceEditor文件事务、基线/修改后测试和最多两次有限repair。
- 外部注入模型、工作区和配置；SQLite状态/文件对账、预算账本、运行轨迹与结构化终态。
- 有界文本工具、多语言符号子集、目标文件working视图和版本化cursor。
- 每次宿主验证的可信输入准入；已撤回Windows profile的六入口提前拒绝。
- 固定镜像、无网络候选容器与独立observer的受限JSON任务试点；模型请求前人民币预留。

## 已有证据

2026-11-12首个真实DeepSeek Flash任务通过：7请求，输入7607/输出2046 token，baseline失败→post通过，audit/acceptance/cleanup完整。按官方人民币峰值价保守核算0.031582元；不是供应商扣款账单。

随后独立预登记的三固定任务×三次观察已完成，7/9解决、2/9未解决；sum与sum-squares各2/3、weighted-sum为3/3。共71个实际API请求，峰值价保守核算0.333800元，9行全部保留、候选清理全部通过，无自动重试。见[整理后的结果](evaluation.json)。旧首题不混入新cohort。

两次未解决在8请求额度耗尽后停止：实际已发请求都有usage，guard费用已知；原逻辑账本仍将被拒绝调用记为transport error/unknown，不重写为零费成功。这是后续诊断语义改进点，不能隐藏或删去失败。每题3组stdin属于1个pytest testcase，F2P=1、P2P=N/A；不是holdout、SWE-bench或显著提升证明。

策略最多8请求、每次最大输出2048 token、总预留上界0.917504元。输入上界依固定公开模板/tokenizer推导，软件预算以供应商遵循公开协议和核价为前提。只给已允许工具和G工作区，不给候选容器oracle或凭据。

推导与固定上游源码见[预算依据](../research/remaining-20261111/BUDGET_RESEARCH.md)。只支持受审的纯文本/本地函数协议，新字段和收费工具不能自动沿用此上界。

受影响156项回归、25项试点边界和4项Docker控制已通过；这些数字属于相应固定版本，不能当新CI成绩。新增评测与最终双平台CI以当前交付报告和实际GitHub run为准。

## 开发验收

```text
uv sync --locked
uv run --locked python -m tests.test_s5d2_ci_contract --suite unittest
uv run --locked python -m tests.test_s5d2_ci_contract --suite pytest
uv run --locked python -m compileall -q agent tests
```

支持选择保留历史测试源码，但不执行已撤回的Windows任务通道。平台或能力skip必须单列。没有检查的编辑为UNVERIFIED，可信fixture执行不是沙箱。源码pilot CLI需要源码中的锁文件、任务和身份，不是wheel中的console入口。

## 未承诺范围

旧Windows AppContainer安全恢复、88包环境、旧C3、20题/holdout/SWE-bench、trial恢复、repo map/缓存/摘要/角色实验继续延期。单题或小任务集不能证明一般收益；本地测试不能当托管CI或发行证据。

公开CI快照只包含运行代码、测试、必要静态审计工具及整理后的说明。原dirty候选、原始oracle诊断、凭据、私有运行目录与历史投入材料保留本机，禁止随Git分支历史意外发布。
