# P7 Session、Identity 与长期数据边界

本文记录 P7 讨论中已经确认的产品和数据边界，以及仍需继续确认的设计问题。

当前状态为“设计讨论已部分确认，尚未实现”。P1-P6 的代码和行为保持不变。

## 已确认边界

### Device

P7 第一版使用 `device_id` 作为家庭数据隔离边界：

```text
一个 device_id = 一个家庭空间
```

- 不同 `device_id` 的 Session、Identity、Dialogue、VoicePrint、Memory 和 Profile 必须隔离。
- 第一版不引入 Household 实体，也不处理一个家庭绑定多个设备。
- Web 设备标识参考旧小智的 `device-id`、`client-id` 和认证 Token 机制。
- 旧小智 Web 使用保存在 localStorage 中的随机 MAC 样式值；Newtalk 的具体 ID 格式、签发和恢复方式仍待确认。

### Session 与 Connection

Session 表示用户从 Web 点击启动到主动断开的一次连续使用：

```text
点击启动
-> 创建 Session
-> 建立 WebSocket
-> 连续交互
-> 主动断开、刷新或异常断线
-> Session 结束
```

- P7 第一版不恢复刷新前或断线前的 Session。
- Session 是业务使用现场，WebSocket 是网络连接；第一版生命周期接近，但概念上保持区分。
- Session 结束后释放当前 Dialogue 和连接运行时状态。
- Identity、VoicePrint、Memory 和 Profile 不随 Session 结束而删除。

### Identity 与 Turn

Identity 表示当前说话的家庭成员。一个 Session 可以出现多个 Identity，不因说话人切换而创建新 Session。

每个用户 Turn 必须固定记录：

```text
device_id
session_id
turn_id
speaker_identity_id
```

- `speaker_identity_id` 在 Turn 创建后不可改变。
- Session 可以保留最近识别到的成员作为界面状态，但它不能代替 Turn 的正式身份字段。
- 文字输入需要由 Web 明确选择当前成员；语音输入由声纹结果映射成员。
- VoicePrint 是解析 Identity 的证据，不等同于 Identity。

### Dialogue

Dialogue 是当前 Session 的短期上下文，继续沿用 P6 的有限窗口原则。

- 已录入的正式家庭成员共享当前 Session 的家庭 Dialogue。
- Dialogue 中的用户消息必须携带说话人信息，使 LLM 能区分不同成员。
- 成员切换不清空家庭 Dialogue。
- Dialogue 不等同于长期 Memory，Session 结束后第一版不恢复 Dialogue。

### Guest

声纹未录入、未匹配或文字输入未选择成员时，当前 Turn 使用 Guest。

- Guest 可以正常聊天。
- Guest 使用当前 Session 内独立的临时 Dialogue，不继承家庭成员 Dialogue。
- Guest 不读取成员的 Memory 或 Profile。
- Guest 第一版不写长期 Memory、不建立长期 Profile，也不跨 Session 识别。

该边界只能阻止后续 LLM 自动继承成员上下文。已经显示在页面或通过扬声器播放的信息，不可能通过后端 Dialogue 隔离撤回。

### 长期数据概念归属

当前只确认归属关系，不确认具体实现：

```text
Device
|-- Identity
|   |-- VoicePrint
|   |-- Memory
|   `-- Profile
`-- Session
    |-- Family Dialogue
    |-- Guest Dialogue
    `-- Turn -> speaker_identity_id
```

- Memory 属于明确的 Identity，不属于 Session。
- Profile 属于明确的 Identity，并与 Memory 保持不同概念。
- 不同 `device_id` 下即使成员同名，也不能共享长期数据。

## 尚未确认

以下内容不应在实现前被默认决定。

### Device 与访问凭据

- Web `device_id` 使用 UUID、随机 MAC 样式值还是服务端生成 ID。
- `client-id` 的具体含义和是否固定为 `newtalk-web`。
- Token 的签发、过期、撤销、丢失恢复和重新绑定流程。
- localStorage、HttpOnly Cookie 或其他凭据保存方式。
- 家庭数据在浏览器数据被清除后的恢复方式。

### 成员与声纹

- 家庭成员允许录入和修改的个人字段。
- 声纹服务和真实 Provider。
- 声纹注册所需样本数量、时长和提示语。
- 原始音频是否临时保存、何时删除。
- 识别置信度阈值、超时和失败反馈。
- ASR 与声纹的并行方式，以及创建 Turn 前最多等待多久。

### Dialogue 细节

- 多位正式成员共享 Dialogue 时，消息发送给 LLM 的具体格式。
- Guest 出现时页面是否隐藏家庭 Dialogue，成员返回后如何切回。
- 家庭 Dialogue 与 Guest Dialogue 各自的轮次和字符预算。
- 助手回复在多人对话中是否需要记录主要回应对象。

### Memory

- 使用 Memos、本地数据库、其他服务或组合方案。
- 保存原始对话、结构化事实还是两者组合。
- 哪些内容值得保存，哪些内容必须忽略。
- 多人对话中的第三方描述、代词和事实归属。
- Memory Retrieval 的 Top-K、字符预算、超时、缓存和排序。
- 后台保存、幂等、去重、冲突、更新、过期和删除规则。
- 敏感信息、视觉内容和 Tool 结果是否允许进入 Memory。
- Memory 查看、确认、修改和删除的 Web 交互。

### Profile

- Profile 的固定字段与可扩展字段。
- 手工资料和自动推断信息的优先级。
- 来源、置信度、更新时间和冲突确认机制。
- Memory 何时以及如何沉淀为 Profile。

### 存储与并发

- 开发和云端部署使用的数据库。
- 并发连接、活动语音流和同时生成 Turn 的验收目标。
- Provider 并发上限、限流、超时和降级策略。
- 数据库事务、索引和跨 `device_id` 隔离测试。
- 性能指标、压力测试规模和面试演示方式。

### P7 交付拆分

- P7 各子阶段的最终顺序和每个 PR 的范围。
- 家庭成员管理、声纹、Memory 和 Profile 是否分别验收。
- 哪些功能必须进入 P7，哪些功能可以留给后续 Part。

## 讨论原则

- 先确认真实产品行为，再定义数据模型和接口。
- 不因为概念上可能扩展，就在 P7 第一版引入 Household、多设备家庭或 Session 恢复。
- Memory 和 Profile 的具体策略必须单独讨论，不能只因为已经确认归属关系就直接实现。
- 每个子阶段仍需形成可运行、可测试、可回退的纵向闭环。
