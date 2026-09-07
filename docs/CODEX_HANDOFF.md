# Newtalk Codex 交接说明

更新时间：2026-09-07

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

本文是 P7.3 交付内容的一部分；从远端 `main` 阅读本文时，应先用下列命令核对最新状态：

```powershell
git status --short --branch
git log --oneline --decorate -10
gh pr list --state all --limit 20
```

正常交接状态应为 P7.2 和 P7.3 都已通过 PR/CI 合并，工作区没有遗留的 P7.3 代码改动。若实际状态不同，以 Git 和 GitHub 输出为准，不得执行 `git reset --hard`、`git checkout -- .` 或 `git clean` 来“修正”状态。

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

## 4. P7.3 已完成实现

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

## 5. P7.3 验证与持续校准

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

## 6. 本机环境与启动方式

当前本机状态（2026-09-07 核对）：

```text
Python 虚拟环境：D:\Desktop\Newtalk\.venv
PostgreSQL 5432：正在监听
Alembic：20260829_02 (head)
Newtalk 8006：未运行
VoicePrint 8010：未运行
根 .env：已配置 openai-compatible LLM、豆包 ASR/TTS 和 VoicePrint URL
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

2026-09-07 重新执行的结果：

```text
主项目：105 passed, 3 skipped
VoicePrint：3 passed, 1 skipped
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

- P7.3 的 Family/Guest Dialogue 仍只存在于当前 WebSocket 连接内；刷新后 Session 恢复尚未实现。
- Guest 是该连接内统一的访客窗口，不区分多个未知访客。
- VoicePrint CAM++ 推理有进程内锁，单实例并发推理会串行；主聊天通过有限等待降级 Guest，但仍需测并发容量。
- 真实扬声器环境只有浏览器回声消除，没有服务端 AEC；播放 TTS 时误触发 VAD/声纹仍需人工测试。
- 每个 utterance 当前新建豆包 ASR Provider WebSocket，尚未复用连接。
- `ConnectionRuntime` 仍承担较多编排职责。P7.3 不做大拆分，但 P7.4-P7.6 的 Profile/Memory 不应继续无边界堆入其中。
- Profile、MemOS、`memory_search`、后台 Memory Job、Memory Center、Vision 均尚未进入运行链。
- 接手时仍应检查 P7.2/P7.3 的 GitHub PR 和 `origin/main`，不要只凭本地测试判断远端状态。

## 9. P7 后续顺序

已经确认的后续拆分：

```text
P7.4：Profile Template 绑定、后台预取并按 Identity 缓存 Profile Snapshot、关闭 Memory 时正常降级
P7.5：主 LLM Tool Calling、memory_search、PostgreSQL 后台写入任务
P7.6：Memory Center、Profile 锁定、记忆编辑删除、成员完整删除
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

详细设计见 `docs/P7_DESIGN.md`。实现 P7.4 前先核对 MemOS 真实账号、Profile Template 和 API 响应，不根据文档猜测最终 Python 接口。

### P7.4 开始编码前

下一位 Codex 应先从用户处确认或通过真实 Live Test 得到：

- MemOS API Base URL 和本地 `.env` 中的 API Key；密钥不能写入本文、测试夹具或 Git。
- MemOS 控制台创建的 `profile_template_id`。
- 第一版 Profile Template 的字段树，以及哪些字段允许算法更新、哪些字段默认锁定。
- `bind/profile_template`、Profile 查询和编辑接口的真实成功/失败响应样例。
- 已存在 Identity 的补绑定策略：Session 后台预取时懒绑定，或一次性后台补齐。不能只处理 P7.4 以后新增的成员。

P7.4 的最小完成边界：

- Memory 默认可关闭；关闭时不调用 MemOS，现有文本和语音聊天测试继续通过。
- 开启后按 `device_id + identity_id` 建立服务端 Scope，但映射到 MemOS 的 `user_id` 必须由后端生成，不能接受浏览器指定。
- Session 建连后异步预取当前家庭所有 Active Identity，且不阻塞 `hello`；新建成员应触发该 Identity 的后台预取。
- 每个 Identity 的 Profile Snapshot 独立缓存；成员切换测试必须证明 A 的画像不会注入 B 的 Turn。
- Guest 不绑定 Profile Template、不加载 Profile，也不获得任何长期 Memory 能力。
- Profile 尚未就绪、MemOS 超时、鉴权失败或服务不可用时，本轮聊天降级为无 Profile，不得阻塞或终止 Turn。
- P7.4 只实现 Profile 绑定、读取与缓存，不提前实现 P7.5 的 `memory_search`、Tool Calling 或后台记忆写入。

## 10. 文档阅读顺序

1. `docs/CODEX_HANDOFF.md`：当前交接状态和执行顺序。
2. `docs/PROGRESS.md`：已经实现并验证过的历史。
3. `docs/architecture.md`：当前 P7.3 运行时结构。
4. `docs/protocol.md`：HTTP、VoicePrint 和 WebSocket `0.6` 协议。
5. `docs/P7_DESIGN.md`：P7 已确认产品和 Memory 设计基线。
6. `PROJECT_PLAN.md`：总体路线，不代表所有内容已经实现。
7. `docs/DEVELOPMENT_WORKFLOW.md`：Git、PR 和 CI 协作流程。

接手原则：先保持现有测试和真实浏览器链路可用，再做下一个纵向闭环；每个 Part 都应有代码、自动测试、真实验收、进度文档、PR 和 CI。
