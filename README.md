# Newtalk

Newtalk 是一个以 Web 为主要客户端的多模态家庭陪伴机器人。

项目采用按 Part 逐步构建的方式，每个阶段都必须可运行、可测试、可演示。原小智项目仅作为只读参考，不作为 Newtalk 的运行时依赖。

详细规划见 [PROJECT_PLAN.md](PROJECT_PLAN.md)，实际开发进度见 [docs/PROGRESS.md](docs/PROGRESS.md)，Codex 接手状态见 [docs/CODEX_HANDOFF.md](docs/CODEX_HANDOFF.md)，默认协作方式见 [docs/DEVELOPMENT_WORKFLOW.md](docs/DEVELOPMENT_WORKFLOW.md)。

文档口径：

- `README.md`、`docs/architecture.md` 和 `docs/protocol.md` 描述当前 P7.7 运行时。
- `docs/PROGRESS.md` 记录已经完成并验证的历史，不把规划当作完成状态。
- `docs/P7_DESIGN.md` 是 P7 总体设计基线，其中 P7.1-P7.7 已完成代码和自动测试。
- `PROJECT_PLAN.md` 描述项目总体目标和后续路线。

## 当前阶段：P7.7 Session/Dialogue 刷新恢复已完成

P7.1 已完成家庭设备与成员基础。P7.2 在此基础上增加独立声纹服务：

- PostgreSQL 保存 Device 和 Identity，SQLAlchemy 提供数据访问，Alembic 管理 schema。
- Web 首次使用时主动创建家庭空间，获得随机 MAC 样式 `device_id` 和 HttpOnly 设备 Cookie。
- 家庭恢复码只在创建或主动轮换时返回，服务端只保存摘要。
- 家庭恢复会轮换设备凭据，使旧浏览器 Cookie 立即失效。
- 成员 API 和页面支持查看、新增、编辑和删除，并按 `device_id` 强制隔离。
- `GET /ready` 验证持久化服务是否可用。
- WebSocket 必须携带有效设备 Cookie，`hello` 返回当前 `device_id`。
- GitHub Actions 使用真实 PostgreSQL 执行 migration 和数据隔离集成测试。
- `services/voiceprint` 是独立 FastAPI 进程和依赖环境，主服务不导入 Torch/ModelScope。
- 每个成员保存一个声纹模板；三段 16kHz 单声道 PCM WAV 分别提取并归一化后取平均。
- 浏览器提供三段 4 秒录音、录入、重新录入和删除声纹入口。
- Newtalk 使用内部 HTTP Client 调用声纹服务，浏览器不能直接指定 `device_id`。
- 声纹服务故障只影响录入/删除，聊天主链和 `/health` 保持可用。
- `deterministic` 仅用于 CI；正式声纹识别使用 3D-Speaker CAM++。

P7.3 把声纹识别正式接入对话 Turn：

- 同一段语音的 PCM 同时交给流式 ASR 和 VoicePrint，并使用 `utterance_id` 汇合结果。
- ASR Final 立即显示；创建聊天 Turn 前最多等待声纹有限时间，超时、低分、过短或服务失败统一降级为 Guest。
- 有效声纹匹配由服务端再次解析为当前家庭成员，浏览器不能伪造语音身份。
- Member 使用家庭共享 Dialogue，并在发给 LLM 的每条用户消息中标注说话人；Guest 使用独立 Dialogue。
- 文本输入可以显式选择当前家庭成员或 Guest，同样遵守 Family/Guest Dialogue 边界。
- `Turn` 固定保存 `device_id` 和说话人信息，创建后不接受迟到声纹结果修改。

P7.4 增加按成员隔离的稳定 Profile：

- Memory 默认关闭，不配置 MemOS 时原有聊天链路不发起任何 Memory 请求。
- 开启后，WebSocket 建连会在后台预取当前家庭全部成员的 Profile，不阻塞 `hello`。
- 后端使用 `device_id + identity_id` 生成 MemOS `user_id`，浏览器不能指定 Memory Scope。
- 未绑定的既有成员会懒绑定到配置的 Profile Template，再读取 Profile Snapshot。
- 每个 Member Turn 只注入当前 `speaker_identity_id` 已就绪的 Profile；不同成员不会共用 Snapshot。
- Guest、预取尚未完成、MemOS 超时或失败均按无 Profile 继续聊天。
- Profile 读取仍采用连接后台预取，不阻塞 `hello` 或普通 Turn。

P7.5 增加按需长期记忆读写：

- OpenAI-compatible `ChatModel` 支持流式 Tool Call 和 Tool Result；每个 Turn 最多调用一次 `memory_search`。
- 只有 Member 且 Memory 已开启时，主 LLM 才能看到 `memory_search`；Guest 和关闭 Memory 时不注册工具。
- Memory Scope 固定来自服务端 Turn 的 `device_id + speaker_identity_id`，不接受浏览器或模型指定身份范围。
- MemOS 查询失败会作为受控 Tool Result 返回主 LLM，普通聊天不因 Memory 故障而中止。
- 只有成功提交 Dialogue 的 Member Turn 才写入长期记忆；Guest、取消、失败和旧 Turn 均不写入。
- 写入先落 PostgreSQL `memory_jobs` Outbox，再由后台 Worker 调用 MemOS Add Message，不把远程写入延迟放进当前回复。
- `turn_id` 唯一约束避免本地重复入队；数据库任务使用领取租约和有限重试，支持服务重启和多 Worker 竞争。

P7.6 增加可见、可纠正的 Memory Center 与完整成员删除：

- 页面按家庭成员查看、搜索、筛选、编辑和删除 MemOS 长期记忆。
- 页面查看、添加、修改、删除 Profile 字段，并使用锁定状态控制 `algorithm_updatable`。
- 浏览器只访问 Newtalk API；后端从 HttpOnly 设备凭据验证成员归属并生成 MemOS Scope。
- 修改 Profile 后同步更新当前活动 WebSocket 的 Profile Snapshot。
- 删除成员立即改为 `deletion_pending`，后台再删除 VoicePrint、MemOS 记忆与 Profile。
- 删除任务持久化在 PostgreSQL，外部清理成功后才物理删除 Identity。

P7.7 增加同一家庭的短期 Dialogue 恢复：

- 每个 `device_id` 在 PostgreSQL 中只有一个当前 Dialogue Session，页面刷新或短暂重连后复用同一 `session_id`。
- Family 与 Guest 分别维护滑动窗口，只持久化成功完成的用户/助手交换；取消、失败和进行中的 Turn 不落库。
- WebSocket `hello.dialogue` 原子返回恢复状态和最近交换，浏览器据此重建消息列表。
- `turn_id` 唯一约束防止重复提交；同一 Session 的行锁保证并发页面提交时窗口裁剪一致。
- 不同 `device_id` 的 Session 和历史强制隔离；成员完整删除同时清理该成员的持久化交换和活动 Family 缓存。
- Member Turn 完成提交前重新校验 Active Identity，删除期间尚未结束的旧 Turn 不会重新写回 Dialogue 或长期 Memory。
- Dialogue 仍受 `NEWTALK_DIALOGUE_MAX_TURNS` 与 `NEWTALK_DIALOGUE_MAX_CHARS` 限制，不替代 MemOS 长期记忆。

继承能力包括：

- `GET /health` 健康检查。
- `WS /ws` WebSocket 握手、文本聊天和正常关闭。
- 每个 `text_input` 创建唯一 `turn_id`。
- `ChatModel` 定义当前聊天核心实际需要的最小流式契约。
- OpenAI-compatible 模型通过异步 SSE 流返回 `text_delta`。
- Fake LLM 继续用于本地开发、自动测试和 CI。
- `TextToSpeech` 定义当前真实调用需要的最小音频流契约。
- 豆包 V3 双向 WebSocket 接收分段文本并流式返回 PCM。
- Fake TTS 继续用于自动测试和不产生费用的本地验证。
- WebSocket 通过 JSON 发送音频元数据，通过二进制帧发送 PCM。
- 浏览器通过 AudioWorklet 缓冲、播放和停止 PCM。
- 记录 LLM 首 Token、TTS 首音频帧和浏览器开始播放时间。
- TTS 失败不会丢失已经生成的文本回复。
- 浏览器通过 `getUserMedia` 和 AudioWorklet 采集麦克风，重采样为 16kHz 单声道 PCM。
- 服务端通过 Silero VAD v6.2.1 检测语音开始和静音结束。
- `speech_start` 取消旧 LLM/TTS Turn，并要求浏览器立即停止旧音频。
- Fake ASR 继续用于自动测试；豆包 ASR 使用官方 V3 二进制 WebSocket 协议。
- 豆包 ASR 按 100ms 聚合 PCM，实时返回 partial，并在 final 时只创建一个 Turn。
- ASR 记录首个识别结果和完整识别耗时；失败会返回 `asr_failed`，不会关闭 WebSocket。
- 每条 WebSocket 连接从当前 `device_id` 的持久化 Session 恢复独立内存窗口；不同家庭不共享历史。
- 只有成功完成的用户/助手轮次进入 Dialogue History；取消或失败的 Turn 不写入历史。
- 上下文按最近轮次和总字符数双重限制，默认最多 8 轮、12000 字符。
- Fake 和 OpenAI-compatible LLM 使用同一个多轮消息契约。
- WebSocket 接收、当前 Turn 和单一发送队列并发运行，旧 Turn 的迟到结果会被丢弃。
- 环境变量配置和 Newtalk 应用日志。
- HTTP、WebSocket 与真实服务进程自动测试。

当前阶段尚未接入 Vision 和通用 Provider Registry。P7.7 自动测试与 PostgreSQL 集成测试已覆盖 Session 恢复、Family/Guest 分窗、窗口裁剪、重复 Turn、家庭隔离和成员删除清理；真实 MemOS API Key、Profile Template、Search/Add/Edit/Delete 返回数据仍需本地验收。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
docker compose up -d postgres
alembic upgrade head
newtalk
```

如果本机没有 Docker，可以安装独立 PostgreSQL，并让 `NEWTALK_DATABASE_URL` 指向已经创建好的数据库。Newtalk 正式进程不会使用 SQLite 或内存数据库代替 PostgreSQL。

如需覆盖默认运行参数，先复制 `.env.example` 为 `.env`。默认
`NEWTALK_LLM_BACKEND=fake`，不需要 API Key。

P7.7 数据库、设备、声纹和可选 Memory 配置：

```dotenv
NEWTALK_DATABASE_URL=postgresql+asyncpg://newtalk:newtalk@127.0.0.1:5432/newtalk
NEWTALK_DEVICE_COOKIE_NAME=newtalk_device
NEWTALK_DEVICE_COOKIE_SECURE=false
NEWTALK_DEVICE_COOKIE_MAX_AGE_DAYS=365
NEWTALK_RECOVERY_MAX_ATTEMPTS=5
NEWTALK_RECOVERY_WINDOW_SECONDS=900
NEWTALK_VOICEPRINT_URL=http://127.0.0.1:8010
NEWTALK_VOICEPRINT_API_TOKEN=local-voiceprint-token
NEWTALK_VOICEPRINT_TIMEOUT_SECONDS=30
NEWTALK_VOICEPRINT_JOIN_TIMEOUT_SECONDS=1.5
```

启用 MemOS Profile 时，再在本地 `.env` 设置以下内容；密钥不提交到 Git：

```dotenv
NEWTALK_MEMORY_BACKEND=memos
NEWTALK_MEMOS_BASE_URL=https://memos.memtensor.cn/api/openmem/v1
NEWTALK_MEMOS_API_KEY=replace-with-local-secret
NEWTALK_MEMOS_PROFILE_TEMPLATE_ID=replace-with-profile-template-id
NEWTALK_MEMOS_TIMEOUT_SECONDS=5
NEWTALK_PROFILE_MAX_CHARS=2000
NEWTALK_MEMORY_SEARCH_LIMIT=5
NEWTALK_MEMORY_SEARCH_RELATIVITY=0.55
NEWTALK_MEMORY_RESULT_MAX_CHARS=4000
NEWTALK_MEMORY_JOB_POLL_SECONDS=1
NEWTALK_MEMORY_JOB_MAX_ATTEMPTS=3
```

启用 Memory 前必须先运行 `alembic upgrade head` 创建 `memory_jobs`。普通回答只使用 Dialogue 与 Profile；只有主 LLM 明确调用 `memory_search` 时才产生 MemOS 查询和第二次模型调用。长期写入在回复完成后进入 PostgreSQL 队列，外部写入失败不会回滚已经完成的聊天。

CI 和接口联调使用轻量测试后端：

```powershell
docker compose up -d postgres voiceprint
```

真实声纹录入必须构建 CAM++ 环境：

```powershell
$env:VOICEPRINT_EXTRAS="campplus"
$env:VOICEPRINT_BACKEND="campplus"
docker compose build voiceprint
docker compose up -d postgres voiceprint
```

首次启动会下载 `iic/speech_campplus_sv_zh-cn_3dspeaker_16k`，模型缓存保存在 Docker Volume。`deterministic` 后端不具备真实说话人识别能力，不能用于产品演示。

生产 HTTPS 环境必须将 `NEWTALK_DEVICE_COOKIE_SECURE` 设为 `true`。仓库中的 Docker 密码只用于本地开发。

对话窗口配置：

```dotenv
NEWTALK_DIALOGUE_MAX_TURNS=8
NEWTALK_DIALOGUE_MAX_CHARS=12000
```

窗口只保存成功完成的对话，并在同一 `device_id` 页面刷新或短暂重连后恢复。Family 与 Guest 分别裁剪到最近 `NEWTALK_DIALOGUE_MAX_TURNS` 轮；取消、失败和未完成 Turn 不会恢复。

使用智谱或其他 OpenAI-compatible 服务时，在本地 `.env` 配置：

```dotenv
NEWTALK_LLM_BACKEND=openai
NEWTALK_LLM_API_KEY=replace-with-local-secret
NEWTALK_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
NEWTALK_LLM_MODEL=replace-with-enabled-model
NEWTALK_LLM_SYSTEM_PROMPT=你是 Newtalk，一个简洁、友善的家庭陪伴助手。
NEWTALK_LLM_TIMEOUT_SECONDS=30
```

使用豆包 V3 双向流式 TTS 时，在同一个本地 `.env` 配置：

```dotenv
NEWTALK_TTS_BACKEND=doubao
NEWTALK_TTS_APP_ID=replace-with-local-app-id
NEWTALK_TTS_ACCESS_TOKEN=replace-with-local-secret
NEWTALK_TTS_RESOURCE_ID=seed-tts-2.0
NEWTALK_TTS_VOICE_TYPE=replace-with-enabled-voice
NEWTALK_TTS_AUDIO_FORMAT=pcm
NEWTALK_TTS_SAMPLE_RATE=24000
NEWTALK_TTS_TIMEOUT_SECONDS=30
NEWTALK_TTS_USE_SYSTEM_PROXY=false
```

豆包默认直连，避免 `websockets` 自动读取 Windows 系统代理并增加实时链路延迟。只有网络环境明确要求豆包经过系统代理时，才将 `NEWTALK_TTS_USE_SYSTEM_PROXY` 改为 `true`。

使用豆包 2.0 双向流式 ASR 时，在本地 `.env` 配置：

```dotenv
NEWTALK_ASR_BACKEND=doubao
NEWTALK_ASR_API_KEY=replace-with-local-secret
NEWTALK_ASR_RESOURCE_ID=volc.seedasr.sauc.duration
NEWTALK_ASR_WS_URL=wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
NEWTALK_ASR_PACKET_DURATION_MS=100
NEWTALK_ASR_TIMEOUT_SECONDS=30
NEWTALK_ASR_USE_SYSTEM_PROXY=false
```

小时版资源使用 `volc.seedasr.sauc.duration`，并发版使用 `volc.seedasr.sauc.concurrent`。ASR 同样默认直连；只有网络明确要求时才启用系统代理。

`.env` 已被 Git 忽略。不要把真实 API Key 写入 `.env.example` 或提交到仓库。

打开 <http://127.0.0.1:8006/>。不要使用 `file://` 直接打开 `web/index.html`。

运行测试：

```powershell
pytest
pytest services/voiceprint/tests
```

普通测试不会调用真实 Provider。显式执行真实 Provider 冒烟测试：

```powershell
$env:NEWTALK_RUN_LIVE_LLM="1"
pytest -m live tests/live/test_llm.py

$env:NEWTALK_RUN_LIVE_TTS="1"
pytest -m live tests/live/test_tts.py
```

架构和协议说明见 [docs/architecture.md](docs/architecture.md) 与 [docs/protocol.md](docs/protocol.md)。
