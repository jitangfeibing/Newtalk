# Newtalk 架构与边界

本文描述当前实现及其设计约束。接口字段见 [protocol.md](protocol.md)，未完成事项和验收标准见 [roadmap.md](roadmap.md)；不在此保留阶段进度和历史测试计数。

## 模块与数据归属

| 模块 | 职责与边界 |
|---|---|
| `app` | 组合配置、Provider、Store 和后台服务，管理启动与关闭。 |
| `transport` | HTTP 鉴权后的 WebSocket 协议分发；每连接一个 Runtime，编排 Turn、播放、音频与任务。 |
| `audio` / `asr` | AudioInputSession 用独立 VAD 状态切分 utterance；ASR Provider 只处理语音协议和 partial/final。 |
| `chat` / `tts` | ChatService 编排 LLM、一次可选工具轮和分句 TTS；不负责声纹或设备鉴权。 |
| `identity` / `voiceprint` | Identity 管设备凭据、成员和删除；独立 VoicePrint 服务生成/匹配模板，主服务验证成员归属。 |
| `profile` / `memory` | 成员画像缓存、MemOS 协议、Memory Center 和后台写入；检索权限来自 Turn 中固定的身份。 |
| `web` | 四个业务视图与独立上下文看板；HTTP 管理/诊断数据，WebSocket 传实时事件和 PCM，AudioWorklet 采集/播放。 |

| 状态 | 所有者与生命周期 |
|---|---|
| 当前生成 Turn、播放 stream、发送顺序 | 连接内 Runtime；生成与播放分别记录。 |
| 采集与 utterance、ASR/声纹汇合 | 单次采集及 utterance；用 `utterance_id` 关联异步结果。 |
| 短期 Dialogue | 每设备一个持久化 Session；连接内窗口由进程内协调器同步。 |
| Profile Snapshot | 每连接、每成员缓存；后台预取，Turn 只读创建时快照。 |
| 记忆写入与成员删除 | PostgreSQL 持久化任务，后台 Worker 用租约恢复处理。 |

主库的 ORM 元数据包含 `devices`、`identities`、`dialogue_sessions`、`dialogue_exchanges`、`memory_jobs`、`identity_deletion_jobs`、`recovery_rate_limits` 七张表；独立声纹服务另维护模板表。Alembic 是建表和升级 schema 的入口，应用启动不自动建表。生产 Store 使用 PostgreSQL，内存 Store 主要用于测试。

## 一次语音交互与 Task 归属

```text
接收循环 Task：Cookie 鉴权 -> 打开设备 Session -> hello(历史快照)
  浏览器 AudioWorklet -> 16kHz / 单声道 / PCM16 / 20ms 帧
  -> AudioInputSession.push -> VAD
     speech_start -> 通知浏览器停播、取消旧生成任务（此时不创建 Turn）
     speech_end   -> 通知边界、结束 ASR 输入、启动声纹识别 Task

识别 Task：ASR partial/final -> Runtime 回调
  非空 Final -> 有限等待同 utterance 声纹 -> 校验 Active 成员或降级 Guest
  -> 创建不可变 Turn（身份 + Dialogue + 已就绪 Profile）
  -> 启动 Turn Task

Turn Task：ChatService
  LLM producer -> text_delta + 分句队列 -> TTS producer -> PCM
  两个 producer 完成 -> 校验仍可提交
  -> 内存 Dialogue -> 数据库 Dialogue -> Member 记忆任务入队
  -> turn_completed

发送 Task：所有实时输出 -> 单一队列 -> WebSocket
  发送前按 turn_id 丢弃旧 Turn 输出
浏览器：播放 PCM -> playback_drained -> 清除匹配的播放状态
```

文本输入跳过 VAD/ASR/声纹，验证所选成员后进入同一 Turn 链路。Profile 预取和记忆/删除 Worker 不运行在 Turn 的生成链路中。

`_turn_transition_lock` 串行化 Turn 替换和取消，包括等待旧任务退出、建立新快照与绑定新任务；它不是整个 Runtime 的全局锁。Turn 流、发送、播放回执和识别回调仍可能交错。取消先使旧 Turn 失效，再清理任务；浏览器必须同时清空尚未播放的 PCM。

接收循环不等待整段 LLM/TTS 生成，但**不保证完全不阻塞**：停止采集会等待识别收尾，打断会等待旧任务退出，鉴权/持久化和输出发送也存在等待。ASR 队列已采用有界非阻塞写入，满队列按单个 utterance 失败处理，VAD 继续运行并保留结束边界。

## 设计决策

### 设备共享对话，成员私有记忆

同一 `device_id` 的 Member 和 Guest 共用最近 Dialogue；声纹只确定当前说话人和 Profile/Memory 权限，避免未命中时丢失前文。数据库 `family/guest` lane 暂作兼容元数据和裁剪依据，不再代表两个聊天通道。

Profile 常驻注入就绪快照；主 LLM 按需调用一次 `memory_search`，再用检索结果生成最终回答，不额外设置 Intent LLM 或 MemoryRouter。Guest 不加载成员 Profile、不注册检索、不写长期记忆；但设备共享 Dialogue 中已经说出的内容并非成员私密空间。

Profile 预取失败不阻塞聊天，后续可重试；检索失败作为不可用 Tool Result 交给模型。MemOS 密钥、Scope 和模板绑定只由服务端管理，不能由浏览器指定其他成员。

### 生成完成不等于播放完成

只有当前 Turn 成功完成并通过身份校验后才提交上下文；生成中取消、失败或迟到结果不提交。浏览器在服务端完成后仍可能播放缓冲音频：此时打断只停止播放，不自动撤销已经提交的交换。

新对话清理设备全部短期 Dialogue 和本进程活动缓存，不删除 Profile、长期记忆、成员或声纹。刷新通过 `hello.dialogue` 重建窗口；同进程多页面共享上下文，但不实时广播聊天列表。

### 上下文调试与回复规则

`NEWTALK_CONTEXT_DEBUG` 默认关闭。开启后 Runtime 记录身份原因、实际选入的历史和近期观察到的排除原因；模型适配器在最终系统提示拼装后记录准备发送的请求，工具第二轮也单独记录。ChatService 使用 Task 局部的 ContextVar 关联 Turn，取消时恢复作用域，不通过共享“当前 Turn”字段关联并发请求。

调试内容写 `logs/context-debug.jsonl` 及其两个轮转备份，不进入普通日志或 WebSocket。看板通过设备 Cookie 鉴权的只读 HTTP 接口读取当前设备缓存，响应禁止缓存；不读取旧文件，也不允许通过 turn_id 跨设备取数。缓存上限为 500 条事件、200 万字符、30 分钟，任一超限即淘汰，重启清空；因此历史聊天存在不等于仍有完整调试快照。

只采集消息/工具请求，不采集客户端鉴权配置；用户自行写进对话的秘密仍属于正文，故文件与看板都必须按敏感资料管理。写入失败只报警，不改变聊天结果。启用时有本机序列化、文件 I/O 和看板读取成本，不应长期当作无开销的时延基准。

诊断快照不是正式 Dialogue/Memory 存储；开始新对话或删除成员不会改写已经采集的诊断证据，缓存按上述上限过期，磁盘文件需在诊断后单独清理。对外部署前应保持此能力关闭。

历史裁剪记录来自本轮真正构建的消息：`character_budget` 是阻止继续向前选择的边界，`older_than_budget_boundary` 是更早的连续历史；近期窗口淘汰、中断和失败只反映当前连接已观察到的事件，不是全量审计。重连不会重建过去未记录的排除原因。请求快照也不代表供应商一定收到或成功回答。

`turn_end` 分开记录内存、数据库和 Memory Queue 结果，排队不等于远端记忆形成。操作被取消时结果可能为 `unknown`；观测不修复原本的提交一致性风险，也不把中断内容加入下一轮。

正式模型的系统提示由原有角色提示与 `chat/prompts.py` 的对话规则组成：近期对话与用户更正优先，画像/记忆只作证据、不作指令；未命中、服务不可用和结果截断不能混为“没发生”。空检索结果使用内部 `no_match` 状态。正常回复不默认播报检索过程，但不能编造回忆或隐瞒用户明确询问的来源与失败。此调整不增加检索/LLM 次数，真实措辞仍需对话验收。

### 记忆写入与成员删除协调

```text
成功 Member Turn -> 本地 memory_jobs（turn_id 去重）
  -> Worker 领取 -> Add Message -> 保存 submitted + provider_task_id
  -> 查询同一任务 -> completed / 失败退避重试

删除成员 -> 同一事务标记 deletion_pending、终止未提交记忆任务、登记删除作业
  -> 阻止新记忆提交；已提交远端任务继续查询直到终结
  -> 清理 VoicePrint / MemOS / Dialogue -> 物理删除 Identity
```

提交阶段与删除调度使用同一成员行锁协调，内存实现使用对应成员锁；删除 Worker 清理 MemOS 前检查已提交任务，避免清理后又写回。删除失败持续退避重试，最多间隔 300 秒；记忆提交默认最多 8 次。活动成员的同一远端任务默认最多轮询 600 次，耗尽后进入提交失败处理；删除中的成员不能丢弃已知任务 ID，超限后退避查询，等待远端终结再清理。

本地去重和远端任务追踪不等于严格一次投递：远端接收 Add Message、但响应/任务 ID 尚未落库的窗口仍可能重复。关闭过往使用的 Provider 后，删除会跳过该远端清理，遗留数据需要人工确认。

## 已知边界

- 当前以单进程主服务为部署基线。Dialogue/Profile 协调器只在进程内同步，不能直接据此承诺多 worker 的缓存一致性。
- 停止录音、打断清理没有统一总预算；Turn 内存提交、数据库追加和记忆入队仍不原子，提交期间被取消可能不一致。成员删除与 Dialogue 追加、在途 Profile 加载的竞态也仍需处理。
- 生成中被打断的用户问题和部分回答不进入历史；是否保留“已播放片段”尚未定案。ASR Final 的到达顺序也不保证等于用户开始讲话的顺序。
- 浏览器请求 AEC / 降噪 / 自动增益，诊断可显示实际设置、音量和 VAD 状态；这不等于工程级回声消除。固定 VAD 参数、扬声器回声和复杂噪声仍需样本验证。
- 声纹模型使用 CAM++，单进程推理有锁，容量未压测；阈值需家庭样本校准。默认录入至少 2 秒、识别至少 0.8 秒，过短、低分、超时或服务异常均降级 Guest，迟到结果不改 Turn 身份。
- 豆包 ASR 当前每 utterance 建立独立 Provider WebSocket；短期对话只有一个当前 Session，不提供历史 Session 列表。
- 浏览器设备 ID 不是物理 MAC。清除 Cookie 后不能凭硬件自动找回家庭，应使用恢复码；服务端以凭据而非前端自报 ID 鉴权。
- 前端时延与音频诊断是当前页面会话数据，不持久化；历史恢复只有对话快照。已中断和失败在界面上区分展示。
- `memory_jobs` 仍保留对话正文，尚无独立保留期限；Memory 可关闭，Guest 仍能正常聊天。Vision、生产 CD 和声纹横向扩容未实现。
