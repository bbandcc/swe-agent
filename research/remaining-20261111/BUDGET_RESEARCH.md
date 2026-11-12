# DeepSeek 请求前人民币预算上界调研

结论：可以完成一个受限协议的真实试点，而不必等待供应商提供原子人民币预授权。采用官方人民币峰值价格、限制最终出站载荷、公开模板的保守字节上界、固定总请求数与最大输出，在供应商遵循公布计费/模型协议的前提下建立软件费用上界。不能把软件保证写成供应商账单绝不出错的保证。

本报告未读取密钥、未请求模型、未修改源码。外部源码和测试属于参考证据，尚不是本项目实跑结果。

## 官方证据与已确认限制

- [中文定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：Flash峰值缓存未命中输入2元/百万token、输出8元/百万token；缓存和闲时只会更低。直接使用人民币价格，无需美元汇率。模型别名路由是可变服务，必须记录核价时间、准确模型路由和公布版本，不能承诺不可变权重。
- [API max_tokens](https://api-docs.deepseek.com/api/create-chat-completion/)：最大生成token；输入与输出共同受上下文限制。不能用默认输出上限代替显式max_tokens。
- [Thinking](https://api-docs.deepseek.com/guides/thinking_mode/)：默认开启；必须在最终HTTP载荷确认thinking.type=disabled，避免预算和历史reasoning回传语义变化。
- [官方token说明](https://api-docs.deepseek.com/quick_start/token_usage/)：字符比率只是估计，实际usage返回为准；不能用“英文0.3token/字”做硬上界。
- [旧特殊token计数问题](https://github.com/deepseek-ai/deepseek-recipe/issues/5)：0.1.1把用户字面量当成AddedToken，会低估输入。维护者已说明0.2.0修复，不能把旧问题写成当前仍未解决的阻断。

## 固定GitHub参考及字节界依据

官方修复版本为 [93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170](https://github.com/deepseek-ai/deepseek-recipe/commit/93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170)：

- [V4.1 renderer](https://github.com/deepseek-ai/deepseek-recipe/blob/93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170/deepseek-recipe-encoding/src/v4/dsv41.rs)：工具schema和响应格式作为文本加入system；非thinking历史不额外加入reasoning；消息角色和结束由非空special拼写表示。
- [工具模板与公开测试](https://github.com/deepseek-ai/deepseek-recipe/blob/93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170/deepseek-recipe-encoding/src/v4/mod.rs)：工具固定指令、schema变换和special literal测试可查。带raw tool_calls的历史有DSML逐参数膨胀，因此当前最小协议应拒绝这种历史，复用项目已将工具消息转成普通消息的renderer。
- [PromptTokenizer](https://github.com/deepseek-ai/deepseek-recipe/blob/93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170/deepseek-recipe-encoding/src/tokenizer.rs)：plain与explicit special分开编码，不额外加BOS；每explicit special为一个token。
- [固定tokenizer](https://github.com/deepseek-ai/deepseek-recipe/blob/93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170/static/tokenizers/v41/tokenizer.json)：blob `3abdf13e3488acc65e11e6a57f073f6673175c3a`；[来源说明](https://github.com/deepseek-ai/deepseek-recipe/blob/93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170/static/tokenizers/v41/README.md)给出SHA256 `81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99`。本轮经GitHub blob读取确认：normalizer=空Sequence，pretokenizer为Split后ByteLevel且add_prefix_space=false；BPE无额外subword前后缀、无空词表项，padding=null。于是普通文本token数量不超过UTF8字节数量；每非空explicit special的1 token也不超过其拼写字节。这是从公开算法推导的上界，不是服务端usage实测。
- [官方JSON serializer](https://github.com/deepseek-ai/deepseek-recipe/blob/93b0fd229cc9ffa6b1a8c63d42b0bf741bbec170/deepseek-recipe-core/src/json.rs)：逗号和冒号后加空格。对受限整数/布尔/null/string JSON，spaced序列化长度小于等于compact的两倍；非ASCII不转义。浮点序列化实现存在差异，最小协议的schema值拒绝浮点，不用经验padding掩盖差异。

成熟Agent参考 [mini-swe-agent固定default.py](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py)：借鉴总调用数、deadline、finally保存轨迹；其cost_limit明确在花费后停止，不是请求前预留，不能照搬作硬预算证明。格式解析失败也可能已付费，应保留原usage再判断解析失败。

## 可以编码的最小协议

在真正HTTP出站前检查最终payload，而不是在模型调用前只检查原messages：

1. 固定https官方host、POST /chat/completions、deepseek-flash、stream=false、max_tokens=2048、thinking disabled、n为缺省或1；禁止跟随重定向、SDK/transport/application自动重试。只允许一条串行在途请求。
2. messages仅system/user/assistant且content为纯字符串；拒绝role=tool、历史tool_calls、reasoning_content、prefix、name和多模态块。项目的既有工具消息renderer应先完成配对转换，再由guard看实际载荷。
3. tools仅本地function JSON schema；禁止search/fetch/server tools/deferred tool loading。只启用必要的read_source；候选程序和工具都不得触发有偿网络API。
4. 只允许已审定top-level字段；响应格式只允许text/json_object或已计入payload的function-call结构化schema。schema递归只允许JSON整数/布尔/null/字符串/对象/数组，禁止浮点；temperature若需使用可单独限定0或0.0，因为它不进入prompt。
5. 令B为最终payload经compact JSON、ensure_ascii=False、allow_nan=False编码后的UTF8字节数，要求B≤16384；消息数M≤32。保守prompt字节界可取 `2*B + 256*(M+1) + 4096`，最大45312，低于预留的49152输入token。2B覆盖文本和schema的spaced序列化；256/消息覆盖全部角色/终结符与空行；4096覆盖固定工具指令、response-format提示头和required-tool前缀。该常量要随固定公开模板记录，支持新字段或服务版本时重新核对；不是通用OpenAI请求大小公式。

本公式依赖“没有raw历史tool_calls”。如果放开它，必须额外计算每个DSML invoke/parameter及其key/name/value的模板增长；16KB HTTP请求乘3并不是无条件证明。

## 人民币预留和故障规则

每次在网络发送前不可逆预留：`49152*2/1e6 + 2048*8/1e6 = 0.114688元`。总计最多8次，为`0.917504元`。同时使用现有预算账本记录settled usage；试点guard不因缓存、较短输出、失败或usage缺失而释放已用请求槽，不建立第二套可恢复财务数据库。

如请求发送后断连、超时、返回缺usage或计数超过公开上界，立刻停止后续付费，保留该次全额预留及UNKNOWN状态。非2xx也不自动重发。重启不恢复同一trial，原trial出现执行尝试后不得重新执行，避免再取得8次额度。脚本多进程重复启动必须通过同一trial的独占创建锁/执行标记拒绝。

软件能证明请求数、最终字段、最大生成数量和按公开协议计算的上界；无法证明供应商计费错误、临时价格变更、服务端隐式模板变化、账户其它并发应用支出或税费政策。应明确这些协议假设，不能声称余额查询或低额充值构成平台原子预授权。既有“缺平台硬cap就永远NOT_STARTED”方案过度阻断；受限协议加请求前预留是可落地的下一切片。

## 本项目验收必须覆盖

- 正常纯文本/中文/special字面量、边界字节数、最多请求数、最后一笔预留不越1元。
- 不支持字段、raw toolcalls、thinking开启、model/base/endpoint偏离、stream、多n、超大body/schema/多消息在网络前拒绝。
- 网络失败/usage缺失不退款不重试；解析失败保留原usage；并发和同trial再次启动拒绝。
- 模型bind_tools/with_structured_output后的真实最终HTTP请求必须也经过同一guard，防止只保护最外层调用。
- 凭据仅由外部注入；预算/轨迹不存Authorization、.env或原始响应秘密；真实trial保留Architect→Developer→S1→隔离verification→RunRecord证据，不能把一个HTTP成功当试点通过。
