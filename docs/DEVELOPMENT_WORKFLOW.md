# Newtalk 开发流程

本文档记录 Newtalk 当前默认采用的开发方式。它用于保持需求、代码、测试和文档同步，不是不可调整的强制制度。随着项目和团队变化，可以基于真实成本修改。

## 基本原则

- 一个 Part 聚焦一个可以运行、测试和审阅的纵向闭环。
- 优先完成当前功能，不提前设计尚未使用的完整 Provider 或 Manager 体系。
- Legacy 小智项目保持只读，只用于核对协议、参数和历史行为。
- 实际完成情况记录在 `docs/PROGRESS.md`，规划不能代替代码证据。
- CD 暂不启用；当前自动化只负责 CI 测试，不负责部署。

## 默认流程

```text
提出需求
-> 明确当前 Part 的目标和完成标准
-> 从 main 创建 Part 分支
-> 实现最小完整链路
-> 增加或更新测试
-> 本地运行完整测试
-> 更新 PROGRESS 和相关文档
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
codex/p1-bootstrap
codex/p2-text-chat
codex/p3-llm-stream
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
python -m pytest
```

### 4. 同步进度

每个 Part 完成后更新 `docs/PROGRESS.md`，至少写明：

- 实际完成的能力。
- 主要调用链和代码入口。
- 测试结果。
- 仍未实现的边界。
- 下一阶段可能处理的内容。

架构或协议发生变化时，同时更新对应文档。README 只保留项目入口和当前能力摘要。

### 5. 提交和 PR

一个 Part 尽量形成一个容易回退和审阅的提交。推送后创建 Pull Request 到 `main`，PR 用来查看代码差异、CI 结果和保留阶段记录。

合并前默认确认：

- 功能达到本 Part 的完成标准。
- 本地完整测试通过。
- GitHub Actions CI 通过。
- 没有提交 `.env`、API Key、虚拟环境、日志或 Legacy 源码。
- `PROGRESS.md` 与代码状态一致。

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
-> pytest
```

当前 CI 不使用真实 ASR、LLM、TTS、Vision 或 Memory 密钥。未来 Provider 测试默认使用 Fake 或 Mock；确实需要真实 API 的测试应单独分类，并明确控制密钥和调用成本。

## 可简化情况

以下改动可以根据风险缩短流程，不要求机械地为每个字词修改创建独立 Part：

- 纯文档错别字和链接修正。
- 不进入正式代码的临时调查或实验。
- 尚未确认方向的本地原型。

一旦改动影响运行行为、协议、配置、依赖或数据结构，就恢复完整测试和 PR 流程。

## 当前应用

P1 使用 `codex/p1-bootstrap` 分支完成。加入 CI 后，通过第一个 Pull Request 验证这套流程；P2 及后续 Part 默认沿用该方式，并根据实际问题调整。
