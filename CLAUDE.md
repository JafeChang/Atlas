# Atlas — 工作区说明

## 唯一事实来源

本项目的**目标、已定契约、任务 DAG、延后决策，全部以 [`SPEC.md`](./SPEC.md) 为准**。

与任何其它文档冲突时，以 `SPEC.md` 为准。`docs/` 下只保留目标与架构的**原始出处**，不具规范效力；旧的文档治理体制（`.claude/config` 及其任务清单 / 测试报告模板）已废弃。

## 工程约束（强约束，变更需显式提出）

| 项 | 约束 |
|---|---|
| Runtime | Python 3.13.x |
| 依赖管理 | `uv`；不使用裸 `pip install`；依赖最小化、显式声明 |
| 操作系统 | Linux（当前开发环境为 WSL Ubuntu-24.04） |
| 数据源 | 仅公开可访问数据；不绕过反爬、登录、验证码 |
| 优先级 | 工程可持续性 > 可迁移性 > 可审计性 > 性能 |

## 三条硬规则（来自归档基线的失败教训）

1. **"完成"必须能用跑通的数据流证明**——不得用"有文件 / 有字段 / 有测试报告"证明。
2. **不允许用 `except` 掩盖接线错误**；未实现的部分必须响亮失败（`NotImplementedError`），不得返回编造的结果。
3. **只保留单一事实来源的文档**，不再建立平行的文档体系。

## 环境

开发在 WSL 内进行（Windows PowerShell 无法执行项目 venv）：

```bash
wsl bash -lc 'cd /mnt/c/Users/bestz/Documents/projects/Atlas && ./.venv/bin/python -c "import atlas"'
```

- **项目 venv**：`.venv`（Python 3.13.9）。**旧系统副本与运行中的 Docker 旧栈共用它**，因此新结构需要独立环境时请显式指定：
  `UV_PROJECT_ENVIRONMENT=.venv-new uv sync`
  直接跑 `uv sync` 会按精简后的依赖裁剪共享 venv，从而破坏旧副本。
- **旧系统可跑副本**：`../Atlas-legacy`（git worktree @ `main`），共享 `data/` 与 `.venv`。
  采集入口：`PYTHONPATH=src ./.venv/bin/python -m atlas collect`
- **归档基线**：tag `archive/growth-baseline-2026-09-25`（目标重梳理前的完整历史，被弃用的代码都可从这里取回）。

## 不要做的事

- 不要在 `SPEC.md` 之外另建进度文档、任务清单或测试报告模板。
- 不要把 `data/` 纳入 git（它本地保留，且运行中的 Docker 旧栈挂载了它）。
- 不要用"简化实现""先占位后面补"的方式交付——归档基线里那 25,937 行代码就是这么来的。
