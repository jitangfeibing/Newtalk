# Newtalk HTTP、VoicePrint、Memory 与 WebSocket 协议

WebSocket Endpoint 为 `GET /ws`，协议版本 `0.18`。建连前必须通过 HTTP Device API 获得同源 HttpOnly Cookie；缺少或使用失效凭据时以 code `4401` 拒绝连接。

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
  "protocol_version": "0.18",
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
  "music": {"enabled": true},
  "audio": {
    "input": {"codec":"pcm_s16le","sample_rate":16000,"channels":1,"frame_duration_ms":20},
    "output": {"codec":"pcm_s16le","sample_rate":24000,"channels":1}
  }
}
```

`dialogue.items` 包含当前窗口内已落库的最终用户输入，按接受时间排序。`reply_status=completed` 表示助手完整回答已补全；`user_only` 表示只有用户输入，`assistant_text` 为空串，可能来自进行中、被打断、失败或补全未确认的轮次，不能据此猜测具体原因。前端只显示用户气泡，不创建空助手气泡；该条输入仍可参与下一轮上下文。Family/Guest 计数按输入条目而非完整问答对计算。

恢复查询每 lane 默认最多加载 256 条（`NEWTALK_DIALOGUE_MAX_TURNS`），计数不是数据库全部原文数量，也不代表这些消息最终全部发送给模型。正式模型对每次完整请求按 `NEWTALK_CONTEXT_INPUT_TOKENS` 预算独立取舍，默认 32768，当前为保守 Token 估算；取舍明细见设备鉴权的上下文看板。旧原文不会因为本轮预算或恢复窗口限制而从数据库删除。

浏览器只能接收快照，协议不接受客户端提供 `session_id` 或请求其他家庭 Session。同一设备多页面共享持久化 Session，但聊天列表不实时广播，重连时重新读取快照。接受到内存尚未落库的输入不保证出现在此快照；确认前的进程崩溃或落库失败可能导致刷新无法恢复。已停用成员的历史不会恢复。

## 音乐与媒体播放

`hello.music.enabled` 表示当前服务已配置音乐演示，不保证每个搜索结果具有可播放音频。模型的 `music_control` 工具支持 search/play/pause/resume/stop；功能页直接控制不经过 LLM：

```json
{"type":"music_control","event_id":"client UUID","action":"play","query":"洛天依","index":null}
{"type":"music_control","event_id":"client UUID","action":"pause","query":null,"index":null}
{"type":"music_control","event_id":"client UUID","action":"resume","query":null,"index":null}
{"type":"music_control","event_id":"client UUID","action":"stop","query":null,"index":null}
```

play 的 query 至多 80 字，省略时默认“轻音乐”；index 为 1～100 的正整数，query=null 且提供 index 时选择当前设备尚未过期的搜索结果。非 play 不接收 query/index。直接控制不支持 search，候选查询由模型工具处理。event_id 可省略，由服务端生成；提供后在本连接最近 128 个提交中去重，同编号不同参数拒绝。

语音操作和按钮统一提交给连接内 MediaSession；工具立即返回 status=accepted 和 operation_id，模型可简短确认正在处理。接受后的解析不随聊天打断取消，换歌／停止使旧解析失效，暂停保留解析且设置就绪后静音，断开取消未完成任务。搜索解析不在接收循环内等待。尚未接受的旧 Turn 意图不能在取消后补提交。

解析与控制使用独立事件，不带原聊天 turn_id，也不再以 turn_cancelled.reason=media_control 结束内置音乐工具轮。服务端状态示例（track 仅含已准备曲目的标题、歌手；target 为待处理目标）：

```json
{"type":"media_status","enabled":true,"operation_id":"UUID","revision":1,"operation_state":"resolving","action":"play","target":"洛天依","desired_playing":true,"client_status":null,"track":null,"error_code":null,"message":null}
{"type":"media_control","request_id":"UUID","operation_id":"UUID","revision":1,"action":"play","autoplay":true,"track":{"track_id":"UUID","title":"歌曲标题","artist":"歌手","cover":"https://y.qq.com/...","source":"QQ音乐","stream_url":"/api/music/tracks/UUID/audio"}}
{"type":"media_result","request_id":"UUID","action":"play","status":"playing"}
{"type":"media_state","request_id":"UUID","action":"play","status":"paused"}
```

operation_state 为 idle/resolving/ready/completed/stopped/failed；它描述操作进度，client_status 是网页报告的播放状态，二者不能混为一谈。暂停与继续复用当前操作编号/版本，换歌与停止递增 revision。网页忽略低于已见版本的控制、状态和源错误。非 play 的 track=null；play.autoplay=false 表示准备曲目并回报 paused，不自动出声。

网页对控制 request_id 去重；等待聊天/提醒时报告 waiting，真实 playing 事件报告 playing，其余回执为 paused/stopped/ended/failed/no_track。waiting 续期，其余回执结束该次等待；仅接受本连接、动作匹配且未超过 90 秒的请求，最多保留 16 个。之后的自动暂停、自然结束和错误用 media_state 回报，沿用最近控制的 request_id/action；只接受最近控制关联的 playing/paused/stopped/ended/failed 状态，旧控制的迟到状态不覆盖新状态。网页回执仅表示客户端观测，不等于服务端确认扬声器实际出声。

错误示例：

```json
{"type":"media_error","operation_id":"UUID","revision":1,"code":"no_resource","message":"前几个匹配版本均没有可播放地址，请换一首试试。"}
```

解析错误含 operation_id/revision；参数校验等接受前错误可省略二者。code 包括 no_resource/upstream_error/upstream_timeout/invalid_response/invalid_request/timeout/internal_error/busy/unavailable/cancelled。播放器失败另经 media_result/media_state 反馈，事实快照记 player_failed。错误不关闭聊天，不自动启动新模型轮，不把无资源推断为版权或会员限制。

- `GET /api/music/status`：设备 Cookie 鉴权，返回 enabled/source。
- `GET /api/music/tracks/{track_id}/audio`：设备 Cookie 鉴权和资源归属校验，支持单段 Range，转发 200/206/416 及音频必要头，Cache-Control=no-store；不存在、跨设备或过期返回 404，上游不可用返回 502。

前端不接收第三方密钥或带票据的源地址，不允许客户端提交任意音频 URL。已开始的音乐被聊天/提醒暂停后，等待用户明确继续；尚未开始的新点播/继续请求等待聊天/提醒全部结束再尝试播放。音量仅由网页本地设置。刷新、断线不保留播放进度或自动续播。

### 喜马拉雅听书

hello 增加 `podcasts: {"enabled": true}`；音乐和听书共用 media_control/media_result/media_state/media_status/media_error。media_status 增加 source（music/podcast），表示当前操作所属内容源；客户端控制动作仍仅有 play/pause/resume/stop，后端的 next/change 解析完成后统一发送 play。

听书的 track 示例：

```json
{"track_id":"UUID","title":"单集标题","artist":"主播","source":"喜马拉雅","media_type":"podcast","album":"专辑标题","cover":"","stream_url":"/api/podcasts/tracks/UUID/audio"}
```

音乐 track 的 media_type 为 music、album 为空；原有字段仍保留。网页按同源受限路径播放，不接受客户端指定外部地址。既有 music_control 的暂停/继续/停止按钮作用于当前媒体源；play 仍用于音乐，听书首版通过对话入口选择，不新增一套网页点播表单。

模型工具 podcast_search(query, kind=auto/series/program) 返回本连接 selection_id 和至多 5 个精简候选。成功搜索后仅允许一次 podcast_control 后续调用；纯查找可以直接介绍或澄清，不自动播放。podcast_control(action=play/next/change/pause/resume/stop, index, selection_id) 中，play 必须使用真实当前候选编号和 selection_id，其他动作两参数填 null。下一集与换专辑同样只返回 accepted，由连接独立解析并发送控制。

- `GET /api/podcasts/status`：设备 Cookie 鉴权，返回 enabled/source。
- `GET /api/podcasts/tracks/{track_id}/audio`：设备归属校验，支持单段 Range，200/206/416 与关闭上游行为同音乐接口；404 表示资源不可用/不归属当前设备，502 表示源失败。

额外错误 code 包括 no_selection（候选过期/尚未选定）和 selection_changed（源返回专辑与候选不一致）。候选与选集在连接关闭时清理；不提供跨连接续听进度。

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
{"type":"vad_speech_start","capture_id":"capture ID","utterance_id":"UUID","probability":0.87,"audio_ms":96.0,"media_only":false}
{"type":"vad_speech_end","capture_id":"capture ID","utterance_id":"UUID","probability":0.08,"audio_ms":1248.0,"last_voice_audio_ms":608.0,"media_only":false}
```

`vad_speech_start.media_only=false` 沿用对话打断，会立即停止浏览器聊天播放并取消仍在生成的旧 Turn；media_only=true 表示该语音段始于纯播放模式，客户端不得仅因 VAD 停止媒体或聊天音频，服务端也不取消旧 Turn。即使服务端已经完成 TTS 发送，只要浏览器尚未确认播放队列耗尽，服务端仍会发送 `audio_stop`。ASR 事件为：

```json
{"type":"asr_partial","utterance_id":"UUID","text":"中间文本"}
{"type":"asr_final","utterance_id":"UUID","text":"最终文本","disposition":"chat"}
```

asr_final.disposition 为 chat/ignored_media/media_control；旧客户端缺省按 chat 理解。纯播放时仅识别完整暂停/停止口令，media_control 的 text 是识别出的该条口令，后端直接提交暂停/停止，仍由 media_control/media_result 确认执行，不创建 LLM 回复。其余内容返回 ignored_media 且 text 为空，不在前端新增聊天消息；需要清理该 utterance 的时序与 user 忙闲标记。纯播放不发送中间转写，不等待声纹；该段始于哪个播放请求/操作固定到最终识别，迟到停止不能作用于新请求。识别错误仍发送 asr_failed，不因此暂停媒体。

media_status 增加 voice_mode=media_only/conversation，说明当前语音入口策略；音乐/听书实际 playing 后进入纯播放，暂停/停止/结束/失败后恢复对话，文字及按钮保持显式操作入口。纯播放只接受完整退出口令，换歌/换集/聊天需先暂停；这不是本地关键词唤醒，停播需等 ASR Final 确认。

ASR 和 VoicePrint 使用相同 `utterance_id`。对话模式非空 `asr_final` 会先发给浏览器，再在配置的有限期限内等待声纹结果：

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

## 当前页面的设备控制

`device_control` 只操作当前 Cookie 鉴权的连接，不接受目标家庭、成员或其他连接标识。`action` 限定为 `stop_reply`、`microphone_off`、`camera_off`、`disconnect`。浏览器按钮可发送：

```json
{"type":"device_control","event_id":"client ID","action":"stop_reply"}
```

该入口要求非空、至多 128 字符的 `event_id`，连接内重复返回 `duplicate_event`，非法动作返回 `invalid_device_action`。本机关麦按钮直接复用采集停止协议；断开按钮先清理本页资源，再发送既有 `close` 并关闭本地 WebSocket。

完整语音识别或文字输入中的明确口令（如“别说了”“关闭麦克风”“关闭摄像头”“断开连接”）也可直接触发控制，不创建聊天 Turn、不加入 Dialogue、不等待成员身份查询。只匹配完整口令，支持句末句号／感叹号和一个“请”前缀；疑问、否定、引用和目标不明的“关闭”不会通过该入口执行。其他表达按普通聊天进入模型，模型可调用 `device_control` 工具；权限仍限当前页面，识别准确性取决于 ASR 与模型，不能仅靠口令规则保证任意自然表达都正确。

服务端为每次请求产生独立 `request_id`，发送：

```json
{"type":"device_control","request_id":"server UUID","event_id":"input ID","turn_id":null,"action":"microphone_off"}
```

来自模型工具的请求含对应 `turn_id`，直接口令／按钮为 `null`。模型控制轮保留已接受输入，但以 `turn_cancelled.reason=device_control` 结束生成，不发送空的 `turn_completed`，不生成告别 TTS 或完整交换记忆。客户端显示独立操作状态，清理空助手占位。

浏览器执行后回报：

```json
{"type":"device_control_result","request_id":"server UUID","action":"microphone_off","status":"completed"}
```

- 非断开动作可回报 `completed`、`not_active` 或 `failed`；断开只回报 `accepted` 或 `failed`，然后关闭连接。`accepted` 表示已收到断开请求并开始收尾，实际断开以 WebSocket 关闭事件为准。
- 服务端只接受本连接仍待处理、动作匹配且尚未回报的请求；未知、过期、重复或非法状态返回 `invalid_device_control_result`。结果是客户端报告，不是服务端独立验证硬件状态。
- 服务端以 `device_control_status` 返回同一组 `request_id`、`event_id`、`turn_id`、`action` 及 `status`；发出请求后 5 秒内无回执则状态为 `unknown`，不当作成功或自动重试。明确口令／按钮请求的执行放在独立 Task，不内联阻塞接收循环等待该次生成取消；每连接在途控制及待回执请求合计最多 16 个，超过返回 `device_control_busy`，断开时清理这些任务。
- 浏览器按 `request_id` 去重；资源清理单项等待上限为 2 秒，断开时一项清理失败也继续关闭其余资源及连接。迟到的旧连接操作不向新连接发送回执或关闭新连接。
- 当前尚无摄像头预览／视觉采集，`camera_off` 如实回报 `not_active` 并显示“摄像头未开启”；不控制其他应用的摄像头。已关闭麦克风或连接后，重新开启使用页面按钮。

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


## 设备共享一次性提醒

以下 HTTP 接口均复用设备 Cookie 鉴权，设备范围由服务器确定；请求不能指定其他 `device_id`。未认证返回 401，不存在或不属于本设备返回 404，业务参数/状态冲突返回 409，请求结构错误返回 422。

| 接口 | 请求/响应 |
|---|---|
| `GET /api/reminders` | `{scope:"device", timezone:"Asia/Shanghai", items:[提醒]}`，最多 200 条，按到期时间降序。 |
| `POST /api/reminders` | `{content, due_at, request_key}`；成功 201，返回提醒。相同设备/请求键不重复创建，内容/时间不一致拒绝。 |
| `PATCH /api/reminders/{id}` | `{content, due_at}`，仅 scheduled 可修改。 |
| `DELETE /api/reminders/{id}` | 逻辑取消，返回 cancelled 提醒，并通知页面停止该条播放。 |
| `POST /api/reminders/{id}/replay` | 手动重播已错过/已结束投递，要求在线且没有待确认播放；产生新投递编号。 |
| `GET /api/reminders/{id}/audio?delivery_id=...` | 当前有效投递的短 WAV，包含提示音与固定正文；失败 503，失效/取消 404，不缓存。 |

`content` 为 1～160 字，`due_at` 必须是带时区的 ISO8601，未来一年内；网页按北京时间输入。`request_key` 为 1～128 字。返回提醒含 `reminder_id/content/due_at/created_at/status/delivery_id/delivery_status/delivery_deadline`，不向前端公开内部连接编号或请求键。`status` 与 `delivery_status` 的语义和状态列表见架构说明；播放结果不表示用户已完成事项。

服务端到点只发给一个选定连接，不归属聊天 Turn，也不混用聊天 PCM 音频流：

```json
{"type":"reminder_due","reminder":{"reminder_id":"UUID","content":"取快递","due_at":"2026-10-26T01:00:00+00:00","created_at":"2026-10-26T00:55:00+00:00","status":"triggered","delivery_id":"UUID","delivery_status":"pending","delivery_deadline":"2026-10-26T01:01:30+00:00"}}
```

创建/修改或接受播放回执后广播 `{"type":"reminders_changed"}`，页面重新查询列表；取消广播 `{"type":"reminder_cancelled","reminder_id":"UUID"}`。前端显示文字提醒，等当前聊天真实播放结束后优先完整播报，后续问题不能抢占。排队/准备播放时每 15 秒发送 status=waiting 续期，服务端仍保存为 pending，并把 delivery_deadline 延后 90 秒；开始播放后不再发送 waiting。真实开始播放及结束后分别回报：

```json
{"type":"reminder_playback","reminder_id":"UUID","delivery_id":"UUID","status":"playing"}
```

`status` 允许 `waiting/playing/completed/interrupted/failed/expired`；设备/连接由当前会话确定。结构非法或本连接队列已满返回 `invalid_reminder_playback`；不匹配或重复回执静默忽略，不承诺收到回执就已入库。每连接顺序提交，断开取消剩余回执任务。续期或开始播放后超过 90 秒仍无有效续期/终态则记 unknown；离线记 missed，不自动补播。浏览器阻止播放时保留文字卡片，并提供主动启用提示音/重新播放入口。

提醒排队或播报期间，新问题仍接受并保存，生成 Task 等待本连接所有在途提醒结束；当前到点前已开始的回复不受此门控影响。服务端先沿用 turn_started，再发送：

```json
{"type":"turn_waiting","turn_id":"UUID","reason":"reminder_playback"}
```

页面释放该轮 generation/user 忙碌标记，显示“提醒播报后回复”，但仍等待旧聊天 PCM 实际结束；普通输入不停止提醒音频。只有提醒结束后才开始下一轮模型生成，不在 WebSocket 接收循环中等待。

播完、明确停止、失败或投递超时后，服务端向目标连接发送以下事件，同时释放匹配投递的生成等待；其他旧投递编号不影响当前提醒：

```json
{"type":"reminder_delivery_ended","reminder_id":"UUID","delivery_id":"UUID","status":"completed"}
```

取消提醒继续使用 reminder_cancelled，并释放该提醒全部投递的等待。明确点击“停止提醒播放”、取消提醒、断开连接可终止播报；普通 VAD、新问题和聊天的停止指令不打断提醒。
