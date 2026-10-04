# Newtalk Codex 交接说明

更新时间：2026-10-04

本文面向接手 Newtalk 后续开发的 Codex。开始工作前必须先阅读本文，并结合当前工作区执行 `git status`，不得只根据 README 或规划文档判断功能是否完成。

## 1. 项目目标与固定边界

Newtalk 是从小智项目经验中重新搭建的 Web 多模态家庭陪伴机器人，不依赖旧小智项目运行。当前产品边界已经确定：

- Web 是主要客户端，目标包含文字、麦克风实时语音、流式 TTS、VAD 打断、摄像头/图片理解。
- ASR、LLM、TTS、VLLM 和 Memory 保留 Provider 思想，但只在实现对应 Part 时定义实际需要的最小接口，不提前建立完整 Provider 世界。
- 当前最主要的架构控制目标是避免形成旧项目那种过胖的 `ConnectionHandler`。Newtalk 允许 `ConnectionRuntime` 暂时编排连接内任务，但新的持久化业务、Memory 和 Profile 不应继续全部塞入其中。
- Vision 后续按统一对话输入重做，不复制旧小智“图片转描述，再伪装成 listen 文本”的链路。
- 家庭陪伴重点是 Device、Identity、VoicePrint、Profile 和 Memory。复杂多模态情绪融合不在当前核心范围。
- Tool Calling 后续只保留真正需要的工具，不完整迁移 IoT、Device MCP 和大量外围插件。
- 原小智代码只作为只读参考，不修复、不作为 Newtalk 运行时依赖。

## 2. 仓库与 Git 真实状态

仓库路径：

```text
D:\Desktop\Newtalk
```

远端：

```text
origin https://github.com/jitangfeibing/Newtalk.git
```

P7.2 交付记录：

```text
提交：33ca97e feat: complete P7.2 voiceprint enrollment
PR：https://github.com/jitangfeibing/Newtalk/pull/10
合并提交：69e2a64 Merge pull request #10 ... P7.2
```

P7.3 交付记录：

```text
提交：629db35 feat: complete P7.3 speaker-aware turns
PR：https://github.com/jitangfeibing/Newtalk/pull/11
合并提交：f5c1247 Merge pull request #11 ... P7.3
```

P7.4 交付记录：

```text
提交：e5ff324 feat: complete P7.4 profile snapshots
PR：https://github.com/jitangfeibing/Newtalk/pull/13
合并提交：6fdeb8d Merge pull request #13 ... P7.4
状态：代码和自动测试已合并；真实 MemOS 验收尚未执行
```

P7.5 交付记录：

```text
提交：dc979c8 feat: complete P7.5 memory tools
PR：https://github.com/jitangfeibing/Newtalk/pull/14
合并提交：1de762e Merge pull request #14 ... P7.5
状态：代码、文档、自动测试和 PostgreSQL 集成验证已通过 CI 合并；真实 MemOS 验收尚未执行
```

P7.6 交付记录：

```text
提交：800c156 feat: complete P7.6 memory center
PR：https://github.com/jitangfeibing/Newtalk/pull/16
合并提交：bdd0bd8 Merge pull request #16 ... P7.6
状态：Memory Center、完整成员删除、文档、自动测试和 PostgreSQL 集成验证已通过 CI 合并；真实 MemOS 验收尚未执行
```

P7.7 交付记录：

```text
提交：544f582 feat: complete P7.7 session recovery
PR：https://github.com/jitangfeibing/Newtalk/pull/18
合并提交：9cd2bf2 Merge pull request #18 ... P7.7
状态：Session/Dialogue 持久化、刷新恢复、文档、自动测试和真实 PostgreSQL 集成验证已通过 CI 合并
验证：148 passed, 2 skipped；5 项真实 PostgreSQL 集成测试和真实浏览器刷新恢复通过
```

从远端 `main` 阅读本文时，应先用下列命令核对最新状态：

```powershell
git status --short --branch
git log --oneline --decorate -10
gh pr list --state all --limit 20
```

当前正常状态是 P7.2-P7.7 已通过 PR/CI 合并，P7 的代码阶段完成，下一阶段为 P8 统一 Vision 输入。若实际状态不同，以 Git 和 GitHub 输出为准，不得执行 `git reset --hard`、`git checkout -- .` 或 `git clean` 来“修正”状态。

若 GitHub CLI 未登录或授权过期，应先检查 `gh auth status`，不要反复创建重复 PR。

## 3. 当前已完成能力

### P1-P6

- FastAPI/Uvicorn 应用、原生 HTML/CSS/JavaScript Web、`/health`、WebSocket hello/close。
- 一个用户行为只创建一个 Turn，Fake LLM 和 OpenAI-compatible 流式 LLM。
- 豆包 V3 双向流式 TTS，服务端二进制 PCM，浏览器 AudioWorklet 边收边播。
- 浏览器麦克风采集并重采样为 16kHz、单声道、PCM S16LE。
- Silero VAD、本地语音边界、语音开始打断旧 Turn。
- Fake ASR 和豆包 2.0 双向流式 ASR，partial/final 事件。
- 每连接有限 Dialogue Window；只提交成功完成的 Turn，取消和失败 Turn 不污染上下文。

### P7.1

- PostgreSQL、SQLAlchemy Async 和 Alembic。
- 服务端签发随机 MAC 样式 `device_id`、HttpOnly Device Cookie 和家庭恢复码。
- Device/Identity 数据隔离及家庭成员 CRUD 页面。
- `/ready` 检查数据库是否可用。
- WebSocket 建连前必须通过 Device Cookie 鉴权。

### P7.2

- 仓库内独立 `services/voiceprint` FastAPI 服务，主进程不直接导入 Torch/ModelScope。
- 正式后端使用 3D-Speaker CAM++：`iic/speech_campplus_sv_zh-cn_3dspeaker_16k`。
- 浏览器录制三段 4 秒 WAV，用于成员声纹录入、重新录入和删除。
- 每段 16kHz、单声道、PCM16 WAV 分别提取向量，归一化后求平均并保存。
- VoicePrint 查询只在当前 `device_id` 的 Active Identity 模板内匹配。
- 已完成真实 CAM++ CPU 加载、浏览器三段录入和 PostgreSQL 512 维模板写入验证。
- `deterministic` 后端仅供自动测试和 CI，不能作为产品声纹识别结果。

## 4. P7.3-P7.7 当前实现

### P7.3 说话人 Turn

P7.3 目标是把声纹识别正式写入 Turn 身份，并建立 Member 与 Guest 的对话边界。

当前 `main` 已经实现：

- 文本输入可显式选择家庭成员或 Guest。
- 语音输入的同一段 PCM 同时送给流式 ASR 和 VoicePrint。
- ASR 与 VoicePrint 以 `utterance_id` 为汇合键。
- ASR Final 先发送给浏览器显示，随后在创建 Turn 前最多等待声纹 `1.5s`，该值由 `NEWTALK_VOICEPRINT_JOIN_TIMEOUT_SECONDS` 配置。
- 匹配成功后，服务端使用 `device_id + identity_id` 再次读取 Identity，避免客户端或声纹服务越权指定其他家庭成员。
- 超时、无匹配、音频过短、VoicePrint 不可用、服务错误或 Identity 已删除，均降级成 Guest，不让聊天失败。
- Member 使用同一连接内共享的 Family Dialogue；Guest 使用独立 Guest Dialogue。
- Family Dialogue 中每条用户消息发给 LLM 时携带说话人姓名和家庭关系。
- Turn 创建时固定 `device_id`、`speaker_identity_id`、显示名和关系；迟到的声纹结果不能修改已创建 Turn。
- WebSocket 协议版本从 `0.5` 升级到 `0.6`。
- 新增 `speaker_resolved`，并在 `turn_started` 中增加 `speaker`。

完整语音链：

```text
Browser getUserMedia
-> AudioWorklet: 16kHz mono PCM S16LE
-> WebSocket binary frames
-> AudioInputSession
-> Silero VAD speech_start
   |-> 创建 utterance_id
   `-> 打断旧 Turn
-> 同一 utterance PCM
   |-> DoubaoStreamingASR -> partial/final
   `-> PCM 转 WAV -> HttpVoicePrintClient -> CAM++ /v1/identify
-> ConnectionRuntime 按 utterance_id 有限等待汇合
   |-> 匹配当前家庭成员 -> Member Turn -> Family Dialogue
   `-> 其他情况 -> Guest Turn -> Guest Dialogue
-> ChatService -> LLM 流 -> TTS PCM 流
-> 浏览器 AudioWorklet 播放
-> 成功完成后提交对应 Dialogue
```

主要代码入口：

- `src/newtalk/app.py`：进程组合入口，构造各 Provider、IdentityService 和 WebSocket 路由。
- `src/newtalk/transport/websocket.py`：WebSocket 鉴权、建连和帧分派。
- `src/newtalk/transport/runtime.py`：P7.3 当前汇合点、Turn 生命周期、Family/Guest Dialogue 和发送队列。
- `src/newtalk/audio/session.py`：VAD utterance 切分、pre-roll、ASR 音频队列和完整 utterance PCM 回调。
- `src/newtalk/audio/wav.py`：把项目 PCM S16LE 包装成 VoicePrint 所需 WAV。
- `src/newtalk/voiceprint/client.py`：主服务调用独立 VoicePrint 的最小 HTTP 契约。
- `src/newtalk/chat/models.py`：带不可变设备/说话人字段的 Turn。
- `src/newtalk/chat/session.py`：Dialogue Window 和说话人标签。
- `web/app.js`：`speaker_resolved` 展示及语音消息说话人更新。
- `web/identity.js`：成员列表与文本说话人选择。
- `tests/test_websocket.py`：P7.3 的主要行为测试。

### P7.4 Profile Snapshot

P7.4 已合并实现：

- `newtalk.profile.ProfileProvider` 最小契约，以及默认关闭和 MemOS 两种实现。
- MemOS `/get/memory` 只请求 Profile；缺少配置模板时调用 `/bind/profile_template` 后重新读取。
- `ProfileScope` 使用服务端可信的 `device_id + identity_id` 生成 MemOS `user_id`。
- `SessionProfileCache` 在 WebSocket 启动后后台预取全部 Active Identity，并按 `identity_id` 缓存 Snapshot。
- Member Turn 只读取当前说话人的已就绪 Snapshot，并以有长度预算和防提示词注入说明的 `system` 消息提供给 LLM。
- Guest、关闭 Memory、预取未完成和 MemOS 失败都不等待远端服务，继续原聊天链。
- WebSocket 协议已升级为 `0.7`，`hello.profile.enabled` 和 `turn_started.profile` 可观察当前状态。

主要新增入口：

- `src/newtalk/profile/models.py`：Scope、字段、Snapshot 和 Prompt 映射。
- `src/newtalk/profile/provider.py`：最小 Provider 契约和关闭实现。
- `src/newtalk/profile/memos.py`：官方 HTTP API 的异步绑定与读取。
- `src/newtalk/profile/session.py`：连接内后台任务和 Identity Snapshot 缓存。
- `tests/test_profile.py`：API 请求、绑定流程、字段解析和非阻塞缓存测试。

### P7.5 Memory Tool 与后台写入

P7.5 当前分支已经实现：

- `ChatModel` 增加一次 `ModelToolCall` 和 Tool Result 的最小契约，OpenAI-compatible 流能聚合分片参数。
- 只有 Member 且 Memory 开启时提供 `memory_search`，每个 Turn 最多调用一次；Guest 完全看不到该 Tool。
- 查询的 `device_id + identity_id` 固定来自 Turn，LLM 只能提供查询文本。
- MemOS Search 失败会作为受控 Tool Result 返回第二轮模型，不中断普通聊天；中间 Tool 事件不进入 TTS。
- 只有成功提交 Dialogue 的 Member Turn 才进入 PostgreSQL `memory_jobs`，`turn_id` 在本地唯一。
- 后台 Worker 使用 `FOR UPDATE SKIP LOCKED`、处理租约和有限重试调用 MemOS Add Message 异步模式。
- 新增迁移 `20261004_03_memory_jobs.py`；应用生命周期启动和关闭 Worker。

主要新增入口：

- `src/newtalk/memory/models.py`：规范化长期记忆与写入回执。
- `src/newtalk/memory/provider.py`：当前实际需要的 Memory 契约与关闭实现。
- `src/newtalk/memory/jobs.py`：PostgreSQL Outbox、Worker、租约和重试。
- `src/newtalk/chat/service.py`：Tool 注册、单次调用上限、Scope 绑定和第二轮 LLM。
- `src/newtalk/chat/openai_compatible.py`：Tool Call 分片解析和消息序列化。
- `src/newtalk/profile/memos.py`：MemOS Search Memory 与 Add Message。
- `tests/test_memory.py`、`tests/integration/test_postgres_memory_jobs.py`：Memory 行为和真实数据库验证。

### P7.6 Memory Center 与完整删除

- Web 可以按当前家庭成员查看、搜索、修改和删除长期记忆，并编辑、删除或锁定 Profile 字段。
- 所有管理请求都通过 Device Cookie 和 Active Identity 归属校验，浏览器不能构造 MemOS Scope。
- 成员删除先进入 `deletion_pending`，再由 PostgreSQL Job 清理 VoicePrint、MemOS Memory/Profile，最后物理删除 Identity。
- 活动 Profile Cache 在人工修改和成员删除后同步更新。

### P7.7 Session/Dialogue 恢复

- 一个 `device_id` 唯一对应一个 PostgreSQL Dialogue Session；刷新和短暂重连复用稳定 `session_id`。
- Family 与 Guest 分别保存有限窗口，只持久化成功完成的交换。
- WebSocket `hello.dialogue` 携带恢复快照；浏览器重建消息，Runtime 恢复 LLM 上下文。
- `turn_id` 唯一约束和 Session 行锁保证幂等及同家庭并发裁剪；不同设备仍完全隔离。
- 成员完整删除同步清理该成员的持久化交换和活动 Family Dialogue。
- Member Turn 提交前重查 Active Identity，避免删除中的旧 Turn 重新写回 Dialogue 或 Memory。
- 主要入口为 `src/newtalk/chat/persistence.py`、迁移 `20261004_05`、`transport/websocket.py` 和 `transport/runtime.py`。

## 5. 验证与持续校准

P7.3 的功能交付以自动测试、P7.2 真实 CAM++ 录入验证、协议检查和故障降级测试作为完成标准。以下真实家庭语音测试用于后续校准识别阈值与等待期限，不阻塞 P7.3 合并：

1. 启动 PostgreSQL、VoicePrint CAM++ 和 Newtalk 主服务。
2. 使用已有家庭 Cookie 和已录入声纹的成员连接页面。
3. 说一段至少 2 秒、建议 3～5 秒的自然中文。
4. 确认先出现 `asr_final`，随后出现 `speaker_resolved`。
5. 已录入成员应得到 `matched=true`、正确 `display_name`、`reason=matched` 和可记录的 `score`。
6. 陌生人或低分样本应成为 Guest，聊天仍正常。
7. 用两个正式成员分别说话，确认 Family Dialogue 能看到双方历史且说话人标签正确。
8. 用 Guest 对话，确认 Guest 不读取 Family Dialogue。

预期事件示例：

```json
{
  "type": "speaker_resolved",
  "utterance_id": "UUID",
  "identity_id": "member UUID",
  "display_name": "小明",
  "matched": true,
  "score": 0.82,
  "reason": "matched",
  "provider_elapsed_ms": 153.4,
  "elapsed_ms": 160.2
}
```

真实测试后重点记录：

- 同人分数和异人分数。
- CAM++ CPU 的 P50/P95 识别耗时。
- `1.5s` 汇合期限是否经常误降级 Guest。
- 默认阈值 `0.72` 是否适合当前麦克风和家庭环境。

不要为了让单次测试通过而盲目降低阈值。阈值必须同时观察同人和异人样本。

P7.5 自动测试基线是主项目 137 项中 135 项通过、2 项付费 Provider live 测试跳过；声纹服务在本机 PostgreSQL 下 4 项全部通过。真实 MemOS 验收尚未完成，接手时必须使用用户本地 `.env`，不能让用户把 Key 发到聊天或写入仓库：

```dotenv
NEWTALK_MEMORY_BACKEND=memos
NEWTALK_MEMOS_BASE_URL=https://memos.memtensor.cn/api/openmem/v1
NEWTALK_MEMOS_API_KEY=本地密钥
NEWTALK_MEMOS_PROFILE_TEMPLATE_ID=控制台模板ID
NEWTALK_MEMOS_TIMEOUT_SECONDS=5
NEWTALK_PROFILE_MAX_CHARS=2000
NEWTALK_MEMORY_SEARCH_LIMIT=5
NEWTALK_MEMORY_SEARCH_RELATIVITY=0.55
NEWTALK_MEMORY_RESULT_MAX_CHARS=4000
NEWTALK_MEMORY_JOB_POLL_SECONDS=1
NEWTALK_MEMORY_JOB_MAX_ATTEMPTS=3
```

验收时创建或选择至少两个 Member，连接 WebSocket 后观察 `profile_prefetch_completed`；分别用两个身份发消息并检查 `turn_started.profile`。再测试一条需要历史信息的问题，确认模型调用 `memory_search`；完成 Member Turn 后检查 `memory_jobs` 和 MemOS 新增内容。还应临时使用错误配置或停止外部服务，确认 Profile 未就绪和查询失败时 Turn 仍正常完成。

## 6. 本机环境与启动方式

当前本机状态（2026-10-04 核对）：

```text
Python 虚拟环境：D:\Desktop\Newtalk\.venv
PostgreSQL 5432：正在监听
Alembic：20261004_05 (head)
Newtalk 8006：未运行
VoicePrint 8010：未运行
根 .env：已配置 openai-compatible LLM、豆包 ASR/TTS 和 VoicePrint URL；尚未配置 MemOS
```

不得把 `.env`、API Key、Token 或数据库生产密码提交到 Git。

### 安装或恢复环境

```powershell
cd D:\Desktop\Newtalk
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pip install -e ".\services\voiceprint[campplus,dev]"
python -m pip check
```

### 数据库迁移

```powershell
cd D:\Desktop\Newtalk
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

正式 Newtalk 进程不使用 SQLite 替代 PostgreSQL。

### 启动真实 VoicePrint

VoicePrint 独立服务直接读取当前 PowerShell 进程环境变量，不依赖根 `.env` 自动加载：

```powershell
cd D:\Desktop\Newtalk
$env:VOICEPRINT_BACKEND="campplus"
$env:VOICEPRINT_DATABASE_URL="postgresql+asyncpg://newtalk:newtalk@127.0.0.1:5432/newtalk"
$env:VOICEPRINT_API_TOKEN="与根 .env 中 NEWTALK_VOICEPRINT_API_TOKEN 相同的本地值"
$env:VOICEPRINT_DEVICE="cpu"
.\.venv\Scripts\newtalk-voiceprint.exe
```

默认监听 `http://127.0.0.1:8010`。首次 CAM++ 启动可能下载或加载模型，等待日志出现服务启动完成后再测试。

### 启动 Newtalk

另开一个 PowerShell：

```powershell
cd D:\Desktop\Newtalk
.\.venv\Scripts\Activate.ps1
newtalk
```

主页面：<http://127.0.0.1:8006/>

检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8006/health
Invoke-RestMethod http://127.0.0.1:8006/ready
Invoke-RestMethod http://127.0.0.1:8010/health
```

不要用 `file://` 打开 Web 页面。

## 7. 测试基线

2026-10-04 重新执行的结果：

```text
主项目：148 passed, 2 skipped（使用真实本机 PostgreSQL）
VoicePrint：4 passed（使用真实本机 PostgreSQL）
pip check：No broken requirements found
```

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest services\voiceprint\tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

跳过项包含需要显式开启的真实 Provider/CAM++ 环境测试，不等于失败。普通 CI 不应调用付费 LLM、ASR 或 TTS。

## 8. 当前客观边界与风险

- Family/Guest Dialogue 已可刷新恢复，但第一版没有多个活动页面之间的实时消息广播，也不提供历史 Session 列表。
- Guest 是该连接内统一的访客窗口，不区分多个未知访客。
- VoicePrint CAM++ 推理有进程内锁，单实例并发推理会串行；主聊天通过有限等待降级 Guest，但仍需测并发容量。
- 真实扬声器环境只有浏览器回声消除，没有服务端 AEC；播放 TTS 时误触发 VAD/声纹仍需人工测试。
- 每个 utterance 当前新建豆包 ASR Provider WebSocket，尚未复用连接。
- `ConnectionRuntime` 仍承担较多编排职责；Dialogue SQL 位于 `newtalk.chat.persistence`，Memory HTTP、解析、任务领取和重试留在 `newtalk.memory`/`newtalk.profile.memos`。
- Profile Snapshot、`memory_search`、后台 Memory Job 和 Memory Center 已进入 Member 数据链；Vision 尚未实现。
- Profile 远端失败在同一 Session 内不会自动重试，重新连接后才会再次预取。
- P7.4/P7.5 尚未完成真实 MemOS 账号、Template、Search 和 Add Message 验收，不能仅凭 Mock 测试宣称外部集成完成。
- Outbox 本地按 `turn_id` 去重并提供至少一次投递；MemOS 端是否幂等尚未确认，崩溃发生在远端接收后、本地完成标记前时可能重复写入。
- 接手时仍应检查最新 GitHub PR 和 `origin/main`，不要只凭本地测试判断远端状态。

## 9. P7 后续顺序

已经确认的后续拆分：

```text
P7.4：Profile Template 绑定、后台预取并按 Identity 缓存 Profile Snapshot、关闭 Memory 时正常降级（代码和自动测试完成，待真实验收）
P7.5：主 LLM Tool Calling、memory_search、PostgreSQL 后台写入任务（代码和自动测试完成，待真实验收）
P7.6：Memory Center、Profile 锁定、记忆编辑删除、成员完整删除（代码和自动测试完成，待真实 MemOS 验收）
P7.7：Session/Dialogue 持久化、页面刷新恢复和恢复边界测试（PR #18、CI 已通过并合并）
P8：统一 Vision 输入（尚未开始）
```

Memory 基线不可改回旧小智的“每轮先查 Memory 再调用 LLM”：

```text
Dialogue -> 当前 Session 短期上下文
Profile  -> 稳定资料，Session 建连后后台预取并按 Identity 缓存
MemOS    -> 主 LLM 按需调用 memory_search 查询长期情景记忆
```

规则：

- Guest 只有 Dialogue，不读取或写入 Profile/MemOS。
- 同一 Session 可以出现多个 Member，Profile Snapshot 必须使用 `identity_id` 分别缓存；每个 Turn 只能向 LLM 注入当前 `speaker_identity_id` 对应且已经就绪的 Profile。
- Profile 预取不能阻塞 WebSocket `hello` 或 Turn 主链；尚未完成、超时或失败时，本轮按无 Profile 继续聊天。
- Member Turn 成功完成后，后台异步写入长期记忆，不阻塞当前回复。
- 普通聊天不强制查询 MemOS。
- `device_id` 和 `identity_id` 必须由服务端绑定到 Memory Scope，不能信任 LLM 或浏览器传入的数据范围。
- Memory Provider 可关闭；关闭后 Dialogue、ASR、LLM 和 TTS 必须继续工作。
- 第一版使用 MemOS 已有 Add/Search/Profile 能力，不自建 Embedding、Rerank、知识图谱或通用 Agent Framework。

详细设计见 `docs/P7_DESIGN.md`。P7.4/P7.5 代码已按官方 HTTP 文档实现，仍需真实账号确认权限、Profile Template、Search/Add Message 返回和远端幂等。

### MemOS 真实验收

P7.4-P7.6 的真实 Memory 联调仍需从用户本地配置和真实验收得到：

- MemOS API Base URL 和本地 `.env` 中的 API Key；密钥不能写入本文、测试夹具或 Git。
- MemOS 控制台创建的 `profile_template_id`。
- 按 `docs/P7_DESIGN.md` 的第一版字段树在 MemOS 控制台创建 Profile Template，并记录其 ID；首版字段允许算法更新，P7.6 再验证人工锁定。
- `bind/profile_template`、Profile 查询接口的真实成功/失败日志；日志不得包含 API Key。
- `search/memory`、`add/message` 的真实返回结构、任务状态和重复 `turn_id` 行为。
- 已存在 Identity 使用 Session 后台预取时懒绑定；连接后新增 Identity 在第一次 Member Turn 选择时异步调度，本轮不等待。

P7.4/P7.5 的验收边界：

- Memory 默认可关闭；关闭时不调用 MemOS，现有文本和语音聊天测试继续通过。
- 开启后按 `device_id + identity_id` 建立服务端 Scope，但映射到 MemOS 的 `user_id` 必须由后端生成，不能接受浏览器指定。
- Session 建连后异步预取当前家庭所有 Active Identity，且不阻塞 `hello`；新建成员应触发该 Identity 的后台预取。
- 每个 Identity 的 Profile Snapshot 独立缓存；成员切换测试必须证明 A 的画像不会注入 B 的 Turn。
- Guest 不绑定 Profile Template、不加载 Profile，也不获得任何长期 Memory 能力。
- Profile 尚未就绪、MemOS 超时、鉴权失败或服务不可用时，本轮聊天降级为无 Profile，不得阻塞或终止 Turn。
- 只有 Member 获得 `memory_search`，Scope 必须来自 Turn；普通聊天不强制查询 MemOS。
- 只有成功完成的 Member Turn 进入 `memory_jobs`；Guest、取消和失败 Turn 不写入。
- 查询或写入失败不得破坏已完成的聊天。

## 10. 文档阅读顺序

1. `docs/CODEX_HANDOFF.md`：当前交接状态和执行顺序。
2. `docs/PROGRESS.md`：已经实现并验证过的历史。
3. `docs/architecture.md`：当前 P7.7 运行时结构。
4. `docs/protocol.md`：HTTP、VoicePrint、Memory 和 WebSocket `0.8` 协议。
5. `docs/P7_DESIGN.md`：P7 已确认产品和 Memory 设计基线。
6. `PROJECT_PLAN.md`：总体路线，不代表所有内容已经实现。
7. `docs/DEVELOPMENT_WORKFLOW.md`：Git、PR 和 CI 协作流程。

接手原则：先保持现有测试和真实浏览器链路可用，再做下一个纵向闭环；每个 Part 都应有代码、自动测试、真实验收、进度文档、PR 和 CI。
