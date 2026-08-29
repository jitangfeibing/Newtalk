# P7 Session、Identity 与长期数据边界

本文记录 P7 已经确认的产品和数据边界，以及需要通过真实环境确定的实现参数。

当前状态为“总体设计基线已确认，P7.1 已进入实现”。P1-P6 的聊天和语音行为保持不变。

Memory 的读取、写入、Provider 和前端管理方向已记录在本文中。所有内容仍是设计基线，不表示功能已经实现。

## 已确认边界

### Device

P7 第一版使用 `device_id` 作为家庭数据隔离边界：

```text
一个 device_id = 一个家庭空间
```

- 不同 `device_id` 的 Session、Identity、Dialogue、VoicePrint、Memory 和 Profile 必须隔离。
- 第一版不引入 Household 实体，也不处理一个家庭绑定多个设备。
- 继续使用旧小智的 `device-id` 概念，但不把 Web 的标识误认为物理网卡 MAC。
- ESP32 后续可以使用真实硬件 MAC；Web 首次使用时由服务端签发随机 MAC 样式的 `device_id` 和独立设备凭据。
- 浏览器保留设备凭据时继续进入原家庭；清除浏览器数据后，网页不能读取物理 MAC 自动识别原设备。
- 首次建家时生成独立家庭恢复码。清除数据或更换浏览器后，用户通过恢复码重新绑定原 `device_id`，家庭数据不会因浏览器数据清除而删除。
- 家庭恢复码不是 `device_id`，服务端只保存安全摘要；恢复成功后轮换浏览器设备凭据。
- 不使用浏览器指纹、IP 地址或其他不稳定信息代替设备凭据。
- `client-id` 第一版固定为 `newtalk-web`，只标识客户端类型，不作为安全边界。
- Web 设备凭据使用同源 HttpOnly Cookie；生产环境启用 Secure，WebSocket 握手自动携带 Cookie。
- 家庭恢复码首次建家时展示，用户可以主动轮换；恢复接口必须限速，恢复成功后旧浏览器凭据失效。

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

第一版成员基础字段确定为：

```text
identity_id
device_id
display_name
nickname
relationship
avatar
status
created_at
updated_at
```

- `display_name` 必填，`nickname`、`relationship` 和 `avatar` 可选。
- 兴趣、偏好、长期目标和当前项目属于 Profile，不放入 Identity 基础表。
- 用户删除成员时，产品语义是完整删除：立即禁止访问，随后删除声纹、Profile、MemOS 全部记忆和本地成员映射。
- 外部删除未完成时使用 `deletion_pending` 隔离该成员并后台重试；全部成功后再物理删除本地记录。

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

### 长期数据归属与 Memory 基线

当前确认归属关系和总体实现方向，具体接口与参数仍需在对应子阶段根据真实 API 确定：

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
- Dialogue 保存当前 Session 的短期上下文。
- Profile 保存稳定、长期、经常使用的成员信息，并在 Member Session 中驻留。
- MemOS 保存更具体、更久以前的历史记忆。
- 主 LLM 按需调用 `memory_search`，P7 不增加独立 Memory Router，也不在每轮 LLM 前强制查询 MemOS。
- Member Turn 完成后在后台写入长期记忆和更新 Profile，不阻塞当前回答。
- Memory 是可关闭的增强能力；关闭后 Dialogue 和聊天主链必须正常。
- Guest 不加载 Profile、不注册 `memory_search`、不读写长期记忆。

### VoicePrint 实现基线

P7 不把 `xinnan-tech/voiceprint-api` 作为外部项目运行时依赖，而是在 Newtalk 仓库内实现自己的声纹服务：

```text
Newtalk repository
|-- src/newtalk/             # 主聊天服务
`-- services/voiceprint/     # 独立声纹进程与依赖环境
```

- 参考其 3D-Speaker/CAM++ 模型加载、16kHz WAV 处理、余弦相似度、注册、识别和删除流程。
- Apache-2.0 代码若被复用必须保留许可证和归属说明；优先按 Newtalk 边界重新实现最小代码，不整体复制旧服务。
- 声纹与 Newtalk 放在同一 Git 仓库，但保持独立进程和依赖环境，避免 Torch、ModelScope、NumPy 版本和模型内存影响主服务。
- Newtalk 主服务通过内部 HTTP Provider 调用声纹服务；浏览器不直接访问声纹服务。
- 声纹特征由 Newtalk 的 PostgreSQL 保存，不继续引入旧服务专用 MySQL。
- 声纹服务第一版只提供 Health、Register、Identify 和 Delete 四类能力。
- 一个 Identity 保存一个有效模板。前端录制三段清晰语音，服务分别提取归一化 Embedding 后取平均，生成一个模板。
- 识别时只比较当前 `device_id` 下处于 Active 状态的成员，不能全库匹配。
- 原始录音只用于提取特征，成功或失败后都删除，不作为长期文件保存。
- 阈值、最短有效语音和汇合等待时间做成配置，通过真实家庭录音校准，不直接照搬旧服务默认值。
- 声纹用于个性化身份识别，不作为高安全等级的生物认证或活体检测。

语音运行时链路：

```text
VAD speech_start
-> 创建 utterance_id
-> PCM 持续送入流式 ASR，同时为声纹缓存本段语音

VAD speech_end
-> 关闭 ASR 输入并取得 ASR Final
-> 声纹服务处理同一 utterance_id 的完整语音
-> 等待两者汇合
-> 有效匹配：创建 Member Turn
-> 超时、低分、过短或失败：创建 Guest Turn
```

ASR Final 先返回时，系统只额外等待声纹一个有限期限；声纹迟到后不能修改已经创建的 Turn。具体期限根据本机与云端实测的 P50/P95 决定。

## 已确认的 Memory 设计

### 三层记忆模型

```text
Dialogue
-> 当前 Session 的最近对话

Profile
-> 稳定、长期、经常使用的成员信息

MemOS
-> 更具体、更久以前的历史记忆
```

核心原则：

> 常用、稳定的信息直接提供给主 LLM；更久、更具体的历史记忆由主 LLM 按需通过 `memory_search` 查询 MemOS。

```text
                Newtalk Memory

          +----------+-----------+
          |          |           |
          v          v           v
      Dialogue    Profile      MemOS
      当前对话     稳定画像      长期历史
          |          |           |
          |          |      memory_search
          |          |           |
          +----------+----------> 主 LLM
```

Dialogue 继续沿用 P6 的有限窗口，负责承接当前 Session 刚发生的事情。例如“我今天面试了 -> 感觉怎么样 -> 还不错”只依赖 Dialogue，不查询 MemOS。

Profile 保存姓名、称呼、喜好、兴趣、家庭关系、长期目标和当前长期项目等稳定信息：

- Member Session 启动时加载一次 Profile Snapshot，并在 Session 内驻留。
- 每一轮主 LLM 都可以看到当前成员的 Profile Snapshot。
- 新的稳定信息在回答结束后由后台处理，不阻塞当前回复。
- MemOS 自动更新在当前 Session 中不主动轮询；新的 Profile 默认在下一次 Session 启动时加载。
- 用户在 Memory Center 手工编辑 Profile 时，当前 Session 的 Snapshot 同步更新。
- Session 结束时可以补充校正，但不能作为唯一保存时机，因为异常断线不保证执行。
- 用户锁定的 Profile 字段不能被后台自动更新覆盖。

MemOS 保存上个月的一次面试、以前讨论过的项目方案、某次旅行、曾经提过的人和过去具体经历等情景型历史。这些内容不长期全部放进 Prompt。

### 技术方案比较

| 方案 | 普通聊天延迟 | 历史记忆判断 | 故障影响 | P7 结论 |
|---|---:|---|---|---|
| 每轮强制查询 MemOS | 每轮增加远程查询 | 不漏查询步骤，但容易注入无关结果 | MemOS 影响所有回复 | 不采用 |
| 独立 Memory Router | 普通轮次较快 | 路由器可能误判，额外模型还会增加调用 | 查询轮次受 MemOS 影响 | 暂不采用 |
| 每轮本地向量检索 | 延迟可控 | 不需要二元路由 | 需要自建索引、Embedding 和同步 | P7 暂不实现 |
| 主 LLM 调用 `memory_search` | 普通轮次无 MemOS 查询 | 由理解完整语义的主 LLM 决定 | 仅记忆轮次受影响，可降级 | P7 采用 |

采用主 LLM Tool Calling 的原因：

- 主 LLM 已经需要理解当前消息、Dialogue 和 Profile，不再增加重复的语义判断。
- 普通知识问答和当前会话承接不承担 MemOS 网络耗时。
- “那个面试”“医生之前怎么说”等隐含指代，比关键词规则更适合由主 LLM 判断。
- 查询失败时，主 LLM 可以说明没有获得记录并继续对话，而不是让整个 Turn 失败。

主 LLM 是否调用工具属于概率判断，不能保证每次都正确。允许模型在“是否需要回忆”上存在遗漏，但后端的数据隔离、权限和失败边界必须是确定性的。

### Memory 读取链路

普通 Member 对话：

```text
文本输入或 ASR Final
-> 创建带固定 Identity 的 Turn
-> 当前 Dialogue + Session 内 Profile Snapshot
-> 向主 LLM 提供 memory_search Tool
-> 主 LLM 直接产生最终文本
-> 流式文本与 TTS
-> Turn 完成并提交 Dialogue
```

需要历史记忆时：

```text
文本输入或 ASR Final
-> 创建带固定 Identity 的 Turn
-> Dialogue + Profile + memory_search Tool
-> 主 LLM 输出 Tool Call
-> Newtalk 校验 Tool Call
-> 后端绑定 device_id + speaker_identity_id
-> MemoryProvider.search()
-> MemOS Search Memory
-> 返回有限候选或明确的空结果
-> Tool Result 追加到本 Turn 的模型消息
-> 主 LLM 继续生成最终回答
-> 最终文本才进入 TTS
```

第一版约束：

- 每个 Turn 默认最多执行一次 `memory_search`，调整上限必须以真实测试为依据。
- LLM 只能提供查询文本，不能提供 `device_id`、`identity_id` 或数据范围。
- 查询 Scope 由服务器使用 Turn 中不可变的 `device_id + speaker_identity_id` 绑定。
- Tool Result 只属于当前 Turn，不提交到 Dialogue，也不再次写回 MemOS。
- Tool Call 参数、原始响应和错误不能进入 TTS，只有最终回答可以合成语音。
- MemOS 超时、异常和空结果转换为受控 Tool Result，主 LLM 继续回答或请求用户补充。
- 用户打断时，MemOS 请求、后续 LLM 和 TTS 必须随当前 Turn 一起取消。

概念 Tool 定义：

```json
{
  "name": "memory_search",
  "description": "Search the current member's long-term personal history when Dialogue and Profile are insufficient.",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "A concise description of the past information needed to answer the user."
      }
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

该定义只描述行为，不代表最终直接复制到某一家 SDK。

Guest 不加载 Profile、不获得 `memory_search`，也不读写长期数据。配置关闭 Memory 时同样不加载 Profile、不注册 Tool、不启动后台写入，但 Dialogue、ASR、LLM 和 TTS 必须正常。

### Memory 写入链路

```text
Member Turn 成功完成
-> 提交 Dialogue
-> 投递 MemoryWriteJob
-> 当前回复结束，不等待后台任务

后台 Memory Pipeline
-> 使用 turn_id 做幂等键
-> MemOS Add Message
-> async_mode=true 后立即结束当前 Job 请求
-> MemOS 后台提取事实、偏好、Profile 和事件
-> MemOS 按 Profile Template 和 algorithm_updatable 更新字段
-> 记录成功、失败和耗时
```

第一版规则：

- Guest、被取消和未完成的 Turn 默认不写入长期记忆。
- Job 必须携带 `device_id`、`speaker_identity_id`、`session_id` 和 `turn_id`。
- `turn_id` 防止后台重试产生重复写入。
- 后台失败只记录日志并有限重试，不影响已完成的聊天。
- 视觉观察和 Tool Result 默认不写入，除非后续为具体类型制定策略。
- 不把检索结果再次原样保存，避免记忆自我复制。
- 不把整个家庭 Dialogue 无区分地写给某一个成员。
- 第一版使用 PostgreSQL `memory_jobs` 持久化任务表，不使用仅存在于进程内存中的队列。
- Worker 领取 Pending Job，成功后标记 Completed；失败按有限次数重试，最终进入 Failed 供日志和管理页面查看。

Profile 字段由 MemOS Profile 实例保存，Newtalk 在 Session 中只保留 Snapshot。Memory Center 将字段映射为：

```text
value
updated_at
locked <- not algorithm_updatable
```

更新优先级：

```text
用户在 Memory Center 手工锁定
> 用户明确纠正
> 新的明确自述
> 后台自动推断
```

P7 先利用 MemOS 的提取、去重、冲突处理和 Profile 自动更新能力，不自建复杂 Memory Summarizer、Embedding、Rerank 和冲突引擎。真实运行后再观察总结质量、垃圾记忆、重复记忆、检索准确率和新旧冲突。

MemOS 官方 Profile 能力已确认：用户绑定 Profile Template 后，`add/message` 会自动更新允许算法更新的字段；`edit/profile` 支持新增、修改、删除字段和通过 `algorithm_updatable=false` 锁定字段。因此 P7 第一版不增加额外 LLM 画像提取调用。

```text
创建 Member
-> 使用 Newtalk identity_id 生成全局唯一 MemOS user_id
-> 绑定配置的 Profile Template

Member Turn 完成
-> 后台 add/message(async_mode=true)
-> MemOS 提取事实、偏好、Profile 和事件
-> 下一次 Session 启动时加载新值

Memory Center 手工编辑 Profile
-> 更新 MemOS Profile
-> 同步更新当前 Session 的 Profile Snapshot
```

Profile Template 在 MemOS 控制台创建。实现到该阶段时，通过环境变量配置 API Key、Base URL 和 `profile_template_id`。

### Memory Center 管理链路

Memory Center 让 Member 查看 Newtalk 保存了什么，避免长期记忆成为后台黑盒。

第一版目标：

- 选择当前家庭成员。
- 按全部、Profile、偏好、经历和事实查看内容。
- 搜索当前成员的 Memory。
- 编辑、纠正和删除 Memory。
- 编辑 Profile，并锁定或解锁字段。
- 显示类型、更新时间和来源；实际字段取决于 MemOS 返回结构。
- 浏览器只请求 Newtalk 后端，不直接访问 MemOS，也不持有 MemOS Token。

Newtalk 对浏览器提供以下概念接口，正式请求和响应模型在实现该子阶段时定稿：

```text
GET    /api/members/{identity_id}/profile
PATCH  /api/members/{identity_id}/profile

GET    /api/members/{identity_id}/memories
PATCH  /api/members/{identity_id}/memories/{memory_id}
DELETE /api/members/{identity_id}/memories/{memory_id}
```

后端从设备凭据确定 `device_id`，并校验目标 Identity 属于该设备。前端传入的成员 ID 不能直接作为访问授权。

### Memory Provider 边界

上层不能直接拼接 MemOS URL 或请求体。P7 接入真实 MemOS 时，根据实际 API 定义最小 `MemoryProvider`，不预先建立完整 Provider Registry。

概念能力：

```text
search(scope, query, limit, timeout)
add_messages(scope, conversation_id, turn_id, messages)
list_memories(scope, page, filters)
update_memory(scope, memory_id, patch)
delete_memory(scope, memory_id)
load_profile(scope)
update_profile(scope, patch)
```

`scope` 由 Newtalk 后端构造，至少包含 `device_id` 和 `identity_id`。正式 Python 签名和返回模型在实现时根据已确认的 MemOS API 响应定义。

旧小智 Python 代码只实际接入：

```text
POST /add/message
POST /search/memory
POST /get/message    # 异步任务轮询
```

旧实现每轮 LLM 前强制查询，并在 `async def` 中调用同步 `requests.post(timeout=10)`；异步搜索还可能继续轮询。Newtalk 不复制这一阻塞方式，MemOS Provider 必须使用真正异步 I/O。

当前 MemOS 官方文档已经确认：

```text
POST /add/message             # 支持 async_mode、role_id、role_name、tags 和 info
POST /search/memory          # 返回事实、偏好、Profile、事件及相关度
POST /get/memory             # 按用户分页查看各类记忆
POST /update/memory          # 修改记忆内容或标题
POST /delete/memory          # 按 memory_ids 删除或按 user_id 清空
POST /bind/profile_template  # 为用户建立 Profile 实例
POST /edit/profile           # 编辑、删除和锁定 Profile 字段
POST /delete/profile         # 删除用户的 Profile 实例
```

这套 API 足以支持 P7 的后台写入、`memory_search`、Memory Center、Profile 自动更新和成员完整删除。实现时仍需用真实 API Key 做 Live Test，确认账号权限、配额、异步任务返回和错误码。

P7 的 MemOS 映射确定为：

```text
MemOS user_id         <- Newtalk 全局唯一 identity_id 映射
conversation_id       <- Newtalk session_id
message role_id/name  <- 当前 Turn 的 Identity
info.device_id        <- 当前家庭 ID
info.identity_id      <- 当前成员 ID
info.turn_id          <- 当前 Turn ID
async_mode            <- true
```

默认只允许 MemOS 从成功完成的 Member Turn 中形成事实、偏好、Profile 和事件记忆，不生成 Tool Memory，也不提交检索得到的旧记忆、Vision 观察或被取消 Turn。

### 对当前代码的影响

当前 P6 只支持文本增量模型流，后续至少需要：

- `Turn` 增加不可变的 `device_id` 和 `speaker_identity_id`。
- 模型消息能够表达 System、Tool Call 和 Tool Result。
- `ChatModel` 从纯字符串流扩展为 Text 与 Tool Call 事件流。
- `OpenAICompatibleChatModel` 解析流式 Tool Call，并将 Tool Result 继续发送给主 LLM。
- `ChatService` 增加有上限的 Tool 执行循环，只有最终文本进入 TTS。
- Profile Snapshot 由 Session/Identity 相关对象持有，不继续加重 `ConnectionRuntime`。
- Memory 后台任务和当前 Turn 任务分离，但携带完整归属和幂等键。

这不是一次性引入通用 Agent Framework。P7 只实现 `memory_search` 所需的最小 Tool Calling 闭环，其他工具留在对应 Part 再扩展。

### Memory 验收重点

- Dialogue 或 Profile 足够时，主 LLM 可以不调用 MemoryProvider。
- Tool Call 只能查询服务器绑定的当前成员 Scope。
- Guest 看不到 `memory_search`，也不产生写入任务。
- MemOS 超时、异常和空结果不使 Turn 失败。
- 打断会取消 MemOS 查询和后续 LLM/TTS。
- 关闭 Memory 后文本和语音聊天保持正常。
- 同一 Turn 的后台重试不重复写入。
- 不同 Device 和 Identity 的查询、修改与删除不串数据。
- Profile 锁定字段不会被自动更新覆盖。
- 记录普通轮次和记忆轮次各自的 LLM 首 Token、MemOS 查询、二次 LLM 和 TTS 首帧耗时。

### Memory 暂不实现

```text
独立 MemoryRouter
每轮强制向量检索
Newtalk 自建 Embedding Pipeline
复杂 Rerank
知识图谱
完整冲突推理引擎
通用 Agent Tool Framework
为大规模分布式部署预建完整 Memory 基础设施
```

## 待实测与实现参数

以下内容属于实现参数或真实环境验收项，不再改变 P7 的总体架构。

### Device 与访问凭据

- Cookie 名称、过期时间和恢复接口的具体限流数值。
- 家庭恢复码页面的最终文案和二次确认交互。

### 成员与声纹

- 三段声纹录音各自的最终时长和页面提示语。
- 识别置信度阈值、最短有效语音、汇合期限和失败提示的实测值。
- VoicePrint 独立进程的 CPU/GPU 环境、模型启动耗时和并发上限。

### Dialogue 细节

- 多位正式成员共享 Dialogue 时，消息发送给 LLM 的最终文本格式。
- Guest 与家庭 Dialogue 在页面上的切换和隐私提示。
- 助手回复的主要回应对象是否需要成为持久字段。

P7 第一版的 Family Dialogue 和 Guest Dialogue 均沿用 P6 的 8 轮、12000 字符默认预算，分别维护窗口。

### Memory

- MemOS 真实账号的项目权限、配额、异步任务返回和错误行为。
- `allow_memory_view`、Search Memory 类型过滤和结果注入的最终字段。
- 多人对话中的第三方描述、代词和事实归属。
- `memory_search` 的结果上限、字符预算、超时和 Tool Call 次数在真实测试后的取值。
- MemOS 的幂等、去重、冲突、更新、过期和删除行为是否满足需求。
- 敏感信息、视觉内容和 Tool 结果是否允许进入 Memory。
- Memory Center 第一版具体页面布局和字段展示。

### Profile

- MemOS 控制台中第一版 Profile Template 的具体字段树。
- MemOS 自动更新完成时间和下一 Session 加载结果的真实验收。

### 存储与并发

- PostgreSQL 本地开发和云端部署方式，以及连接池参数。
- 并发连接、活动语音流和同时生成 Turn 的验收目标。
- Provider 并发上限、限流、超时和降级策略。
- 数据库事务、索引和跨 `device_id` 隔离测试。
- 性能指标、压力测试规模和面试演示方式。

### P7 交付拆分

P7 按以下顺序交付，每个子阶段单独形成可运行、可测试的 PR：

```text
P7.1：PostgreSQL、Device 凭据、家庭恢复与成员管理页面
P7.2：独立 VoicePrint 服务、声纹录入、删除与服务测试
P7.3：ASR/VoicePrint 汇合、Identity/Guest 映射和多人 Dialogue
P7.4：Profile Template 绑定、Session Profile Snapshot 与关闭 Memory 降级
P7.5：主 LLM Tool Calling、memory_search 和 PostgreSQL 后台写入任务
P7.6：Memory Center、Profile 锁定、记忆编辑删除和成员完整删除
```

P7.1-P7.4 先建立可靠的数据归属，P7.5-P7.6 再开放长期记忆读写，避免 Memory 在 Identity 尚未稳定时产生污染。

## 剩余主要风险

- P7 运行时将包含 Newtalk 主服务、PostgreSQL、VoicePrint 独立服务和 MemOS 外部 API，开发启动与部署需要在后续 Docker/脚本中统一。
- VoicePrint 模型推理是计算密集型任务，单实例并发、模型预热和 CPU/GPU 资源必须实测；超时只能降级 Guest，不能阻塞聊天。
- 当前 `ChatModel` 只有文本增量，P7.5 需要增加 Tool Call/Tool Result 和第二次模型调用，同时保证中间事件不进入 TTS。
- MemOS 查询轮次会增加一次远程查询和第二次 LLM 调用，必须分别记录耗时并设置可降级超时。
- 成员完整删除横跨 PostgreSQL、VoicePrint 和 MemOS，必须依赖持久化删除任务和重试，不能伪装成单数据库事务。
- 主 LLM 是否调用 `memory_search` 和 MemOS 如何抽取记忆都具有概率性，必须通过真实对话样本、Memory Center 人工纠正和隔离测试控制风险。

## 讨论原则

- 先确认真实产品行为，再定义数据模型和接口。
- 不因为概念上可能扩展，就在 P7 第一版引入 Household、多设备家庭或 Session 恢复。
- 已确认的 Memory、Profile 和 VoicePrint 基线不得在子阶段实现时被隐式改回 Legacy 行为。
- 每个子阶段仍需形成可运行、可测试、可回退的纵向闭环。
