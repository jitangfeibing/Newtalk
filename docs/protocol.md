# P7.5 HTTP、VoicePrint、Memory 与 WebSocket 协议

WebSocket Endpoint 为 `GET /ws`，协议版本 `0.7`。建连前必须通过 HTTP Device API 获得同源 HttpOnly Cookie；缺少或使用失效凭据时以 code `4401` 拒绝连接。

WebSocket 仍以 JSON 帧传控制事件、二进制帧传 PCM。`hello.session_id` 标识当前连接运行时；Family Dialogue 和 Guest Dialogue 当前都在断线后清空。

## Device 与成员 HTTP API

- `GET /api/device`：读取当前 Cookie 对应的 Device，未注册返回 `401`。
- `POST /api/device`：创建家庭空间并设置 Cookie；恢复码只在首次创建响应中出现。
- `POST /api/device/recover`：使用恢复码重新绑定家庭并轮换设备凭据。
- `POST /api/device/recovery-code`：轮换恢复码，旧码立即失效。
- `GET /api/members`：列出当前 `device_id` 的成员。
- `POST /api/members`：创建成员。
- `PATCH /api/members/{identity_id}`：修改当前家庭成员。
- `DELETE /api/members/{identity_id}`：删除成员行时一并删除同一行的声纹模板；跨 MemOS/Profile 完整删除任务在 P7.6 接入。
- `POST /api/members/{identity_id}/voiceprint`：上传名为 `samples` 的三个 WAV multipart part，录入或覆盖当前成员模板。
- `DELETE /api/members/{identity_id}/voiceprint`：删除声纹模板但保留成员资料。

所有成员读写都由服务端从 Cookie 解析 `device_id`，客户端不能在请求体中指定其他家庭。

## 内部 VoicePrint API

内部接口使用 Bearer Token，不直接暴露给浏览器：

- `GET /health`：数据库和服务状态。
- `POST /v1/templates`：`device_id`、`identity_id` 和三个 `samples`，生成平均模板。
- `POST /v1/identify`：`device_id` 和一个 `sample`，只匹配当前家庭模板。
- `DELETE /v1/templates/{identity_id}?device_id=...`：按家庭和成员双重条件删除。

录音格式固定为 16kHz、单声道、PCM16 WAV，每段默认 2～15 秒。原始 WAV 不持久化；PostgreSQL 只保存归一化 float32 Embedding、维度、模型名和录入时间。

## 建连

服务端 `hello` 同时声明输入和输出格式：

```json
{
  "type": "hello",
  "protocol_version": "0.7",
  "session_id": "generated UUID",
  "device_id": "02:11:22:33:44:55",
  "profile": {"enabled": true},
  "audio": {
    "input": {"codec":"pcm_s16le","sample_rate":16000,"channels":1,"frame_duration_ms":20},
    "output": {"codec":"pcm_s16le","sample_rate":24000,"channels":1}
  }
}
```

## 麦克风输入

客户端先声明一次采集，然后持续发送二进制 PCM：

```json
{
  "type": "audio_input_start",
  "event_id": "client ID",
  "capture_id": "capture ID",
  "format": {"codec":"pcm_s16le","sample_rate":16000,"channels":1,"frame_duration_ms":20}
}
```

服务端确认 `audio_input_ready`。Silero 检测到边界时发送：

```json
{"type":"vad_speech_start","capture_id":"capture ID","utterance_id":"UUID","probability":0.87,"audio_ms":96.0}
{"type":"vad_speech_end","capture_id":"capture ID","utterance_id":"UUID","probability":0.08,"audio_ms":1248.0}
```

`vad_speech_start` 会取消当前旧 Turn。ASR 事件为：

```json
{"type":"asr_partial","utterance_id":"UUID","text":"中间文本"}
{"type":"asr_final","utterance_id":"UUID","text":"最终文本"}
```

ASR 和 VoicePrint 使用相同 `utterance_id`。非空 `asr_final` 会先发给浏览器，再在配置的有限期限内等待声纹结果：

```json
{
  "type": "speaker_resolved",
  "utterance_id": "UUID",
  "identity_id": "member UUID or null",
  "display_name": "小明 or Guest",
  "matched": true,
  "score": 0.82,
  "reason": "matched",
  "provider_elapsed_ms": 153.4,
  "elapsed_ms": 160.2
}
```

`reason` 可能为 `matched`、`no_match`、`timeout`、`unavailable`、`invalid_audio`、`failed` 或 `identity_missing`。只有 `matched` 创建 Member Turn，其他结果均安全降级为 Guest；声纹失败不会关闭连接或阻止聊天。

识别失败不会关闭客户端连接：

```json
{"type":"asr_failed","utterance_id":"UUID","code":"recognition_failed","message":"Unable to recognize speech"}
```

停止采集：

```json
{"type":"audio_input_stop","event_id":"client ID","capture_id":"capture ID"}
```

服务端完成当前音频收尾后返回 `audio_input_stopped`。同一连接同时只允许一个 `capture_id`。

## Turn 和打断

文本通过可选 `identity_id` 显式选择当前家庭成员；`null` 表示 Guest：

```json
{"type":"text_input","event_id":"client ID","text":"你好","identity_id":"member UUID or null"}
```

服务端会验证成员属于当前 `device_id`。文本或完成说话人汇合后的 ASR Final 都进入相同 Turn 流，依次可能产生 `turn_started`、`text_delta`、`audio_start`、服务端二进制 PCM、`audio_end` 和 `turn_completed`。

```json
{
  "type": "turn_started",
  "turn_id": "UUID",
  "source": "voice",
  "speaker": {
    "identity_id": "member UUID or null",
    "display_name": "小明 or Guest",
    "guest": false
  },
  "profile": {"enabled": true, "ready": true, "fields": 4}
}
```

Member Turn 使用家庭共享 Dialogue，历史用户消息带说话人姓名和家庭关系；Guest Turn 使用独立 Guest Dialogue。说话人字段在 Turn 创建后固定，迟到声纹结果不能修改当前回复归属。

`profile.enabled` 表示服务端是否配置 Profile Provider；`ready` 表示当前成员的 Snapshot 在本 Turn 创建时已经就绪；`fields` 是本次 Snapshot 的非空字段数。`ready=false` 不属于聊天错误，当前 Turn 会在不等待 MemOS 的情况下继续。Guest 的 `ready` 始终为 `false`。

新文本输入或 VAD 说话开始取消旧 Turn：

```json
{"type":"turn_cancelled","turn_id":"old Turn UUID","reason":"barge_in"}
{"type":"audio_stop","turn_id":"old Turn UUID","stream_id":"old stream UUID or null","reason":"barge_in"}
```

客户端收到 `audio_stop` 必须立即清空播放队列。服务端先使旧 `turn_id` 失效，再取消 LLM/TTS 任务；发送队列会丢弃已经失效 Turn 的迟到帧。

## 错误和生命周期

- 二进制音频未先开始采集：`audio_input_not_started`。
- 输入格式不匹配：`unsupported_audio_format`。
- PCM 字节数不是 2 的倍数：`invalid_audio_frame`。
- Provider 鉴权、超时或协议失败：`asr_failed`。
- 同一连接重复开始采集：`audio_input_active`。
- 文本 `identity_id` 类型错误：`invalid_identity_id`。
- 文本选择了当前家庭不存在的成员：`identity_not_found`。
- `ping` 返回 `pong`；`close` 返回 `closing` 并以 code `1000` 关闭。
- `playback_started` 继续用于记录浏览器首播时间，不创建 Turn。

## P7.3 调用链

```text
Browser getUserMedia
-> mic-recorder-worklet (resample -> 16kHz / 20ms / PCM S16LE)
-> WebSocket binary frames
-> AudioInputSession -> SileroVadStream
   |-> speech_start -> cancel old Turn -> turn_cancelled + audio_stop
   `-> same utterance PCM
       |-> SpeechRecognizer.stream -> ASR partial/final
       `-> WAV -> VoicePrint /v1/identify
-> utterance_id join with bounded wait
   |-> matched active member -> Member Turn -> Family Dialogue
   `-> timeout/no-match/failure -> Guest Turn -> Guest Dialogue
-> ConnectionRuntime.start_turn with immutable device/speaker
-> DialogueSession.messages_for -> speaker-labelled immutable Turn.messages
-> ChatService.stream_turn
-> text_delta + TTS binary PCM -> PcmPlayer / AudioWorklet
-> turn_completed -> DialogueSession.commit
```

## P7.4 Profile 调用链

```text
WebSocket authenticated
-> hello 立即进入发送队列
-> SessionProfileCache 后台列出当前 device_id 的 Active Identity
-> 每个 Identity 生成服务端 MemOS user_id
-> GET /get/memory（只请求 profile）
   |-> 已绑定 -> 缓存 Profile Snapshot
   `-> 未绑定 -> POST /bind/profile_template -> 再次读取 -> 缓存

Member Turn
-> 按 speaker_identity_id 只读当前 Session 缓存
   |-> 已就绪 -> 有字符预算的 Profile system message -> Dialogue -> LLM
   `-> 未就绪/失败 -> 无 Profile -> Dialogue -> LLM

Guest Turn
-> 不读取、不绑定、不注入 Profile
```

MemOS API Key 只存在于服务端环境变量。浏览器协议没有 `memos_user_id`、模板 ID 或任意 Memory Scope 参数。

## P7.5 Memory Tool 调用链

P7.5 没有新增 WebSocket 客户端消息，协议版本仍为 `0.7`。Tool Call 是 ChatModel 与 ChatService 之间的内部事件，不发送浏览器，也不进入 TTS。

```text
Member Turn + Memory enabled
-> 主 LLM 收到 memory_search Tool 定义
   |-> 不调用 -> 直接流式文本
   `-> 调用一次 -> ChatService 校验 JSON query
                 -> 使用 Turn.device_id + Turn.speaker_identity_id
                 -> POST /search/memory
                 -> assistant tool_call + tool result
                 -> 第二轮 LLM（不再提供 Tool）
                 -> 最终流式文本

Guest / Memory disabled
-> 不向主 LLM注册 memory_search
```

`POST /search/memory` 请求由后端生成 `user_id` 和 `conversation_id`，查询 `detail_factual`、`preference` 与 `event`。返回内容按相关度排序并受条数和字符预算限制。鉴权、超时、格式错误等异常会变成 `unavailable` Tool Result，当前 Turn 仍可继续生成回答。

## P7.5 长期记忆写入链

```text
当前活动 Member Turn 完成
-> Dialogue commit
-> INSERT memory_jobs（turn_id 唯一）
-> turn_completed / 当前回复结束

后台 Worker
-> FOR UPDATE SKIP LOCKED 领取到期任务
-> 设置 processing 租约并增加 attempts
-> POST /add/message（user + final assistant，async_mode=true）
   |-> accepted -> completed + provider_task_id
   `-> failed -> 延迟重试，超过上限标记 failed
```

只写入成功完成的 Member 用户消息和最终助手回答。Guest、被打断 Turn、生成失败 Turn、旧 Turn 迟到结果、Tool Result 和检索返回的旧记忆均不写入。该 Outbox 保证本地去重和至少一次投递；真实 MemOS 是否按 `info.turn_id` 提供远端幂等，需要在真实账号验收中确认。
