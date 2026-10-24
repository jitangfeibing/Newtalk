# Newtalk 开发流程

本文档记录 Newtalk 当前默认采用的开发方式。它用于保持需求、代码、测试和文档同步，不是不可调整的强制制度。随着项目和团队变化，可以基于真实成本修改。

## 基本原则

- 一个 Part 聚焦一个可以运行、测试和审阅的纵向闭环。
- 优先完成当前功能，不提前设计尚未使用的完整 Provider 或 Manager 体系。
- Legacy 小智项目保持只读，只用于核对协议、参数和历史行为。
- 实际完成情况以 Git / PR 和测试证据为准；未完成事项集中维护在 [roadmap.md](roadmap.md)。
- CD 暂不启用；当前尚未确定云服务器、容器仓库、域名、Secret 和回滚策略，自动化只负责 CI 测试，不负责部署。

## 默认流程

```text
提出需求
-> 明确当前 Part 的目标和完成标准
-> 从 main 创建 Part 分支
-> 实现最小完整链路
-> 增加或更新测试
-> 本地运行完整测试
-> 更新受影响的架构/协议和路线图
-> Git commit
-> Push 到 GitHub
-> 创建 Pull Request 到 main
-> GitHub Actions 运行 CI
-> 人工审阅结果
-> 合并 main
```

### 1. 明确需求

开始编码前，先确认本次需要解决的问题、明确不处理的边界和可验证的完成标准。大型目标拆成多个 Part，不在一个提交里同时接入所有能力。

### 2. 创建分支

功能开发默认从最新 `main` 创建独立分支：

```text
codex/fix-asr-queue
codex/memory-retries
codex/frontend-diagnostics
```

分支名表达当前 Part 即可，不要求建立复杂编号制度。

### 3. 实现和验证

实现过程中优先形成最小闭环。测试范围根据改动决定：

- 纯 Python 逻辑至少增加单元测试。
- Transport、启动流程或 Provider 接入应增加集成测试。
- Web 交互需要验证页面行为和真实 WebSocket/HTTP 链路。
- 修复缺陷时尽量先增加能够复现问题的测试。

本地提交前默认执行：

```powershell
python -m pip check
.\.venv\Scripts\python -m pytest tests services/voiceprint/tests -q
```

### 4. 同步进度

每个 Part 在 PR 中记录完成能力、验证结果与未处理项；完成的待办从 `docs/roadmap.md` 移除或缩小范围。架构决策和已知边界只维护 `docs/architecture.md`，接口契约只维护 `docs/protocol.md`，不再单独复制一份进度文档。README 只保留项目入口和能力摘要。

### 5. 提交和 PR

一个 Part 尽量形成一个容易回退和审阅的提交。推送后创建 Pull Request 到 `main`，PR 用来查看代码差异、CI 结果和保留阶段记录。

合并前默认确认：

- 功能达到本 Part 的完成标准。
- 本地完整测试通过。
- GitHub Actions CI 通过。
- 没有提交 `.env`、API Key、虚拟环境、日志或 Legacy 源码。
- 受影响的架构、协议和路线图与代码一致。

是否立即合并由当前审阅结果决定，不为了保持流程而强行合并。

## 当前 CI

GitHub Actions 配置位于 `.github/workflows/ci.yml`。它在以下情况运行：

- 有 Pull Request 指向 `main`。
- 代码合并或直接推送到 `main`。
- 在 GitHub Actions 页面手工触发。

当前 CI 使用 Python 3.11，在干净的 Ubuntu 环境中执行：

```text
安装 Newtalk 和开发依赖
-> pip check
-> Alembic migration
-> Ruff 高置信静态检查
-> Node.js 24 前端脚本语法检查与录音诊断测试
-> 主项目 pytest + coverage 报告
-> VoicePrint pytest
```

Coverage 当前只生成趋势报告，不设置阻断阈值；Ruff 也只启用语法错误、未定义名称等高置信规则，避免把纯格式偏好当作交付问题。

当前 CI 不使用真实 ASR、LLM、TTS、Vision 或 Memory 密钥。Provider 测试默认使用 Fake 或 Mock；真实 LLM 冒烟测试标记为 `live`，只有显式设置 `NEWTALK_RUN_LIVE_LLM=1` 才会产生外部调用和费用。

## 当前 CD 边界

仓库目前没有自动部署工作流。这不是漏跑步骤，而是主动边界：在部署目标和 Secret 管理方式确认前，不让 `main` 的每次合并自动操作未知服务器。后续启用 CD 时，至少需要先确认目标主机或容器平台、镜像仓库、PostgreSQL 迁移时机、环境变量注入、健康检查和失败回滚；确认后再增加独立 deployment workflow，并让部署依赖 CI 成功。

## 可简化情况

以下改动可以根据风险缩短流程，不要求机械地为每个字词修改创建独立 Part：

- 纯文档错别字和链接修正。
- 不进入正式代码的临时调查或实验。
- 尚未确认方向的本地原型。

一旦改动影响运行行为、协议、配置、依赖或数据结构，就恢复完整测试和 PR 流程。

## 当前应用

功能交付采用“功能分支 -> 本地测试 -> PR -> GitHub Actions CI -> 合并 main”。下一阶段需求先完成设计确认再创建分支；除非部署目标已经明确，否则合并只触发 CI，不触发生产部署。
