# Newtalk HTTP、VoicePrint、Memory 与 WebSocket 协议

WebSocket Endpoint 为 `GET /ws`，协议版本 `0.11`。建连前必须通过 HTTP Device API 获得同源 HttpOnly Cookie；缺少或使用失效凭据时以 code `4401` 拒绝连接。

WebSocket 仍以 JSON 帧传控制事件、二进制帧传 PCM。`hello.session_id` 标识当前 `device_id` 的持久化 Dialogue Session；页面刷新或短暂重连后复用该 ID。服务端读取 Family/Guest 兼容 lane 后按时间合并为设备级短期 Dialogue。

## Device 与成员 HTTP API

- `GET /api/device`：读取当前 Cookie 对应的 Device，未注册返回 `401`。
- `POST /api/device`：创建家庭空间并设置 Cookie；恢复码只在首次创建响应中出现。
- `POST /api/device/recover`：使用恢复码重新绑定家庭并轮换设备凭据。
- `POST /api/device/recovery-code`：轮换恢复码，旧码立即失效。
- `GET /api/members`：列出当前 `device_id` 的成员。
- `POST /api/members`：创建成员。
- `PATCH /api/members/{identity_id}`：修改当前家庭成员。
- `DELETE /api/members/{identity_id}`：返回 `202 deletion_pending`；成员立即不可访问，后台删除 VoicePrint、MemOS Memory/Profile 后物理删除 Identity。
- `POST /api/members/{identity_id}/voiceprint`：上传名为 `samples` 的三个 WAV multipart part，录入或覆盖当前成员模板。
- `DELETE /api/members/{identity_id}/voiceprint`：删除声纹模板但保留成员资料。
- `GET /api/members/{identity_id}/profile`：读取当前成员 Profile。
- `PATCH /api/members/{identity_id}/profile`：新增、编辑、删除或锁定 Profile 字段。
- `GET /api/members/{identity_id}/memories`：分页、按类型查看或按查询文本搜索长期记忆。
- `PATCH /api/members/{identity_id}/memories/{memory_id}`：校验该 Memory 属于当前成员后修改标题和内容。
- `DELETE /api/members/{identity_id}/memories/{memory_id}`：校验归属后删除长期记忆。

所有成员读写都由服务端从 Cookie 解析 `device_id`，客户端不能在请求体中指定其他家庭。MemOS API Key、远端 user_id、conversation_id 和 Profile Template 由服务端管理，不接受浏览器指定。

## 上下文调试 HTTP API

- `GET /api/debug/context?limit=30`：只列出当前 Cookie 对应设备在当前进程的近期轮次；`limit` 为 1～50。返回 `enabled`、`retention_seconds`、`scope` 和 `turns`。
- `GET /api/debug/context/{turn_id}`：返回该轮仍保留的 `events`，包括最终模型请求、身份/历史取舍、检索与提交状态。API 不提供思维链或模型理解质量评分。
- 未绑定设备返回 `401`；关闭调试时列表为 `enabled=false, turns=[]`，详情为 `503`；未捕获、已淘汰或属于其他设备的轮次统一返回 `404`。
- 成功响应与详情错误响应使用 `Cache-Control: no-store`。服务端只接受 Cookie 鉴权后的 Scope，不接受客户端指定其他 `device_id`。
- 只有显式开启 `NEWTALK_CONTEXT_DEBUG` 后才采集。快照含私人数据；缓存有时间与容量上限，不从日志文件恢复。设置与边界见 [上下文调试](architecture.md#上下文调试与回复规则)。

## 内部 VoicePrint API

内部接口使用 Bearer Token，不直接暴露给浏览器：

- `GET /health`：数据库和服务状态。
- `POST /v1/templates`：`device_id`、`identity_id` 和三个 `samples`，生成平均模板。
- `POST /v1/identify`：`device_id` 和一个 `sample`，只匹配当前家庭模板。
- `DELETE /v1/templates/{identity_id}?device_id=...`：按家庭和成员双重条件删除。

录音格式固定为 16kHz、单声道、PCM16 WAV。默认录入每段 2～15 秒，识别每段 0.8～15 秒；实际限制由服务端配置决定，格式或长度不符返回 `422`。原始 WAV 不持久化；PostgreSQL 只保存归一化 float32 Embedding、维度、模型名和录入时间。

## 建连

服务端 `hello` 同时声明输入和输出格式：

```json
{
  "type": "hello",
  "protocol_version": "0.11",
  "session_id": "stable dialogue session UUID",
  "device_id": "02:11:22:33:44:55",
  "dialogue": {
    "resumed": true,
    "family_turns": 1,
    "guest_turns": 0,
    "items": [
      {
        "lane": "family",
        "turn_id": "completed Turn UUID",
        "user_text": "我今天面试了",
        "assistant_text": "感觉怎么样？",
        "reply_status": "completed",
        "speaker": {
          "identity_id": "member UUID",
          "display_name": "小明",
          "guest": false
        },
        "created_at": "2026-10-04T12:00:00+00:00"
      }
    ]
  },
  "profile": {"enabled": true},
  "audio": {
    "input": {"codec":"pcm_s16le","sample_rate":16000,"channels":1,"frame_duration_ms":20},
    "output": {"codec":"pcm_s16le","sample_rate":24000,"channels":1}
  }
}
```

`dialogue.items` 包含当前窗口内已落库的最终用户输入，按接受时间排序。`reply_status=completed` 表示助手完整回答已补全；`user_only` 表示只有用户输入，`assistant_text` 为空串，可能来自进行中、被打断、失败或补全未确认的轮次，不能据此猜测具体原因。前端只显示用户气泡，不创建空助手气泡；该条输入仍可参与下一轮上下文。Family/Guest 计数按输入条目而非完整问答对计算。

恢复查询每 lane 默认最多加载 256 条（`NEWTALK_DIALOGUE_MAX_TURNS`），计数不是数据库全部原文数量，也不代表这些消息最终全部发送给模型。正式模型对每次完整请求按 `NEWTALK_CONTEXT_INPUT_TOKENS` 预算独立取舍，默认 32768，当前为保守 Token 估算；取舍明细见设备鉴权的上下文看板。旧原文不会因为本轮预算或恢复窗口限制而从数据库删除。

浏览器只能接收快照，协议不接受客户端提供 `session_id` 或请求其他家庭 Session。同一设备多页面共享持久化 Session，但聊天列表不实时广播，重连时重新读取快照。接受到内存尚未落库的输入不保证出现在此快照；确认前的进程崩溃或落库失败可能导致刷新无法恢复。已停用成员的历史不会恢复。

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
{"type":"vad_speech_end","capture_id":"capture ID","utterance_id":"UUID","probability":0.08,"audio_ms":1248.0,"last_voice_audio_ms":608.0}
```

`vad_speech_start` 会立即停止浏览器本地播放，并取消仍在生成的旧 Turn；即使服务端已经完成 TTS 发送，只要浏览器尚未确认播放队列耗尽，服务端仍会发送 `audio_stop`。ASR 事件为：

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

Member 和 Guest Turn 使用同一设备级短期 Dialogue，历史用户消息带说话人姓名和家庭关系。说话人字段在 Turn 创建后固定，迟到声纹结果不能修改当前回复归属；Guest 不获准加载成员 Profile 或检索长期 Memory；设备共享短期 Dialogue 的可见性边界见 [架构说明](architecture.md#设备共享对话成员私有记忆)。

`profile.enabled` 表示服务端是否配置 Profile Provider；`ready` 表示当前成员的 Snapshot 在本 Turn 创建时已经就绪；`fields` 是本次 Snapshot 的非空字段数。`ready=false` 不属于聊天错误，当前 Turn 会在不等待 MemOS 的情况下继续。Guest 的 `ready` 始终为 `false`。

新文本输入或 VAD 说话开始取消旧 Turn：

```json
{"type":"turn_cancelled","turn_id":"old Turn UUID","reason":"barge_in"}
{"type":"audio_stop","turn_id":"old Turn UUID","stream_id":"old stream UUID or null","reason":"barge_in"}
```

客户端收到 `audio_stop` 必须立即清空播放队列。服务端先使旧 `turn_id` 失效，再取消 LLM/TTS 任务；发送队列会丢弃已经失效 Turn 的迟到帧。

浏览器 AudioWorklet 播放完 `audio_end` 前已经收到的全部 PCM 后，上报：

```json
{"type":"playback_drained","event_id":"client ID","turn_id":"Turn UUID","stream_id":"stream UUID"}
```

服务端收到匹配当前播放流的回执后清除播放状态。迟到或不匹配的回执会被忽略，避免旧流影响新流。`turn_cancelled` 只表示生成中的 Turn 被取消；若 Turn 已完成但浏览器仍有缓冲音频，打断只发送 `audio_stop`。

## 开始新对话

客户端开始新对话时清除当前设备 Session 的全部短期 Dialogue，包括 Family 与 Guest 两个窗口：

```json
{"type":"dialogue_clear","event_id":"client ID"}
```

服务端先取消当前连接正在生成或播放的 Turn，再删除 PostgreSQL 中对应 lane，并同步清除本进程内同一设备的活动 Dialogue 缓存。成功响应为：

```json
{
  "type": "dialogue_cleared",
  "event_id": "client ID",
  "session_id": "stable dialogue session UUID",
  "scope": "all",
  "deleted_turns": 3
}
```

该操作只清除短期 Dialogue 和浏览器当前消息列表，不删除 Profile、MemOS 长期记忆、成员或声纹。声纹仍会在下一次语音输入时重新识别成员。

## 错误和生命周期

- 二进制音频未先开始采集：`audio_input_not_started`。
- 输入格式不匹配：`unsupported_audio_format`。
- PCM 字节数不是 2 的倍数：`invalid_audio_frame`。
- Provider 鉴权、超时或协议失败：`asr_failed`。
- 同一连接重复开始采集：`audio_input_active`。
- 文本 `identity_id` 类型错误：`invalid_identity_id`。
- 文本选择了当前家庭不存在的成员：`identity_not_found`。
- 清除短期对话持久化失败：`dialogue_clear_failed`。
- 待完成输入落库任务达到上限：`dialogue_busy`；本次输入未接受，当前 Turn 不因此取消，可稍后重试。
- `turn_failed.code=context_budget_exceeded`：系统信息、当前输入和必要工具交互仍超出输入预算，本次模型请求未发送；用户输入已接受到短期窗口，落库结果另行确认。需缩短输入或调整预算，不应无条件重发同一超长输入。
- `ping` 返回 `pong`；`close` 返回 `closing` 并以 code `1000` 关闭。
- `playback_started` 继续记录浏览器首次渲染 PCM 的时间；PCM 可能包含静音，不等同于首个声音，不创建 Turn。
- `playback_audible` 是可选的非静音输出计时回执，不改变 Turn 或播放状态，正常时不响应；非法数值返回 `invalid_playback_metric`。
- `playback_drained` 表示浏览器已经播放完指定流，不产生响应事件。


内部 Task 调用链、记忆后台状态及其限制统一见 [架构与边界](architecture.md)，不作为浏览器事件协议的一部分维护。

### 非静音输出计时

`last_voice_audio_ms` 是可空的、当前 capture 内 VAD 最后确认语音的样本位置；不是人工标注的词尾。旧客户端可以忽略该附加字段。客户端把它与采集 Worklet 的样本位置、AudioContext 时钟关联，不能直接用服务器时间减浏览器时间。

```json
{"type":"playback_audible","event_id":"client ID","turn_id":"Turn UUID","stream_id":"stream UUID","output_clock":"output_timestamp","metrics":{"speech_end_to_audible_ms":2200.0,"vad_to_audible_ms":1500.0,"turn_to_audible_ms":1300.0,"tts_to_audible_ms":60.0}}
```

播放器在首次输出幅度达到 -60 dBFS 的样本时记录音频时钟位置，优先通过 `getOutputTimestamp()` 映射至设备输出时间估计，无法取得时 `output_clock=render_clock`。这既不是 TTS 网络首包，也不是扬声器物理出声的实录。静音流没有此回执；迟到的旧流事件被浏览器忽略。

`turn_to_audible_ms` 必填；其他差值缺少基准时省略，不能填 0。`speech_end_to_audible_ms` 基于 VAD 语音末尾估计；`vad_to_audible_ms` 从浏览器收到判停事件起算；`tts_to_audible_ms` 从浏览器收到首帧起算。数值须为有限、非负、至多一小时的毫秒数。全部是客户端观测，不用于鉴权、计费或控制状态。
