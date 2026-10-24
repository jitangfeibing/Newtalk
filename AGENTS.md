# Newtalk 协作规则

## 开始前

- 始终用中文沟通。先读任务相关代码、直接调用者和被调用者，再说明范围、明确问题及不修改项。
- 当前实现与边界见 `docs/architecture.md`，待办见 `docs/roadmap.md`。审查证据在 `docs/audits/2026-10-22-code-audit.md`，它是历史快照，不能假定其中每个问题仍存在。
- 只改任务要求的部分，不顺手重构、改名、格式化或删文件。保护已有工作区改动；删代码前搜索调用者，改公共接口前搜索全部调用位置。

## 修改与测试

- Bug 先写可复现测试，确认修改前失败，再修改实现；不删测试、不弱化断言、不吞异常来让测试通过。
- 异步测试用 `asyncio.Event` / `Future` 门控，不用 `sleep` 控制竞态。可参考 `tests/test_transport_runtime.py`。
- 修改 `transport/runtime.py`、`audio/session.py`、`chat/service.py` 的运行逻辑，必须覆盖相关取消、超时或并发场景；纯注释改动不能夹带逻辑变化。
- 不向 WebSocket 接收循环新增内联的外部服务等待；现有等待边界尚未全部修复，不得在文档中声称接收循环完全不阻塞。
- 保持冻结 dataclass、Protocol、结构化 `key=value` 日志风格。中文注释只解释设计原因、生命周期、取消和特殊边界，不逐行翻译代码。
- 不为设计模式增加抽象；以降低理解成本为准，正常路径行为不变，除非任务明确修复 Bug。
- 未获任务授权，不改数据库 CHECK 约束、不新增 migration。自动生成迁移必须检查建删表、索引、约束和数据影响，不能生成后直接提交。
- 运行相关测试及完整回归：`.venv/Scripts/python -m pytest tests services/voiceprint/tests -q`。说明跳过项、警告和未验证范围，不将跳过描述为通过。
- 完成后检查 `git diff` 与无关改动；中文说明改了什么、原因、测试、外部行为变化和刻意未处理项。未经要求不自动提交或合并。

## 文档分工

- README 只写入口、启动、测试和索引；架构决策/已知边界只维护 `docs/architecture.md`。
- 协议变化更新 `docs/protocol.md`；未完成事项维护 `docs/roadmap.md`；提交流程见 `docs/DEVELOPMENT_WORKFLOW.md`。
- 不恢复重复的 PROGRESS / PROJECT_PLAN / HANDOFF 文档；阶段历史、测试计数和交付记录放 Git / PR，临时审查报告归档到 `docs/audits/`。
