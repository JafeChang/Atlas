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

## 五条硬规则（来自归档基线的失败教训）

1. **"完成"必须能用跑通的数据流证明**——不得用"有文件 / 有字段 / 有测试报告"证明。
2. **不允许用 `except` 掩盖接线错误**；未实现的部分必须响亮失败（`NotImplementedError`），不得返回编造的结果。
3. **只保留单一事实来源的文档**，不再建立平行的文档体系。
4. **提交必须对测试结果做门禁**：提交前跑 `pytest`，**退出码为 0 才提交**。
   门禁范围是**本次提交涉及的代码**（纯文档提交可豁免，但需在提交信息里说明未改代码）。
   **工作区是绿的 ≠ 提交是绿的**——子代理边写边跑时，提交很容易抓到"写了一半的中间态"。
   因此提交后必须用**独立 worktree 复核该提交本身**：
   ```bash
   git worktree add --detach /tmp/atlas-verify-<任务号> <commit>
   cd /tmp/atlas-verify-<任务号> && PYTHONPATH=/tmp/atlas-verify-<任务号>/src \
     /mnt/c/Users/bestz/Documents/projects/Atlas/.venv-new/bin/python -m pytest tests -q
   ```
   （`PYTHONPATH` 必须指向该检出，否则可编辑安装仍会导入主工作区的 `src`。
   **worktree 路径必须带任务号**——固定用 `/tmp/atlas-verify` 在多个代理并行时会被互相抢占。）
   **⚠️ 绝不要把 pytest 的输出管道给 `tail`/`head`**：那样拿到的是 `tail` 的退出码。
   实测栽过一次——`pytest tests -q | tail -12` 报 exit 0，而 pytest 其实是 1，
   差一点把红的说成绿的。**正确做法：先重定向到文件，再单独取退出码**：
   ```bash
   ... -m pytest tests -q > /tmp/wt.txt 2>&1
   echo "PYTEST_EXIT=$?"; tail -20 /tmp/wt.txt
   ```
   **复核完就删掉 worktree**（`git worktree remove --force <路径>`，再 `git worktree prune`）。
   攒着会同时占磁盘与注意力——实测一次攒到 **10 个**已完工的检出。
   **否定性断言必须有"活对照"**：断言"X 被拒绝"时，必须有同一调用路径对**合法输入成功**的对照。
   否则**签名不匹配 / 异常类型不对**会伪装成"拒绝成功"——实测两次栽在这里
   （`build_anchor()` 参数名不对导致的 `TypeError`，看起来像"chunk_id 被拒绝"，其实什么都没验证）。
   一句话：**不会失败的测试不是测试；不会成功的否定断言同样不是验证。**
5. **并行代理的 git 纪律**（来自 T-205 / T-206 的实测事故：同一分支上发生 **3 次**提交被卷走 / 被孤立）：
   - **只 `git add <你自己的具体路径>`**。**禁止** `git add -A` / `git add .` / `git commit -a`——它们会把别人暂存中的文件一起卷进你的提交。
   - **禁止** `git commit --amend` / `git reset` / `git rebase` / `git checkout <branch>` / `git stash` 等会移动别人提交的操作。
   - 提交后**立刻**核对：`git show --name-only --format='%H %s' HEAD` 只能包含你改的文件。发现混入**停下来报告**，不要自行 amend 或 reset。
   - 主代理编排时：**git 写操作串行化**——同一时刻只允许一个代理处于"提交中"。

## 测试目录约定

- **`tests/__init__.py` 是允许的、且已被采用**：跨测试文件复用辅助模块用显式包名导入
  （`from tests.<module> import ...`）。目前 **9 个测试文件**这样做（T-003 的 6 个 + T-131 的 3 个）。
  它同时消除了"不同目录下同名测试模块"的隐式解析问题。
- **辅助模块不要用 `test_` 前缀**——pytest 会去收集它。用 `_` 前缀（如 `tests/_migrate_fixtures.py`，正确做法）
  或放进 `conftest.py`。`tests/test_cognition_support.py` 是反例：它纯是辅助模块却没有测试函数，
  收集结果是 0 个用例（无害，但会让人误以为有覆盖）。
- 真实数据测试**必须自跳过**：`data/` 不进 git，所以干净 worktree 里没有它。
  照 `tests/test_search_realdata.py` 的先例（`pytest.mark.skipif` / 模块级 `pytest.skip`），
  并在主工作区真的跑出真实数字。

## `tools/` 约定（立这条规矩的原因：三个任务往里混了三种东西）

**只放"可复现的测量/证据脚本"**——那种重跑一次就能产出数字、且**将来还有人会想再跑一次**的东西。
例：索引体积对比（`t205cjk_measure.py`）、边界探针（`t205cjk_boundary_probe.py`）、
真实调用证据（`t003_real_call.py`）、导入工具（`migrate_legacy.py`）。

**不放个人脚手架**，包括但不限于：`*_commit*.sh`、`*_msg*.txt`、`*_gate.sh`、`*_counts*.sh`、
`*_worktree_verify.sh`、`*_suite_report.sh`、调试脚本（`*_dbg*.py` / `*_smoke.py` / `*_probe*.py` 一次性的那些）。
这些是一次性过程产物，留在库里只会让下一个人分不清哪些还得跑。

命名带任务号以便追溯与清理。**仓库根一律不放临时脚本**——
历史教训：T-130 在仓库根留了 12 个 `scripts_t130_*.py`，T-131 留了 `.t131_probe/`。

> 判据很简单：**"这个东西下个月还有人会跑吗？"** 会 → 放 `tools/`；不会 → 别提交。

## 环境

开发在 WSL 内进行（Windows PowerShell 无法执行项目 venv）。**WSL 是镜像网络模式**，外网必须走宿主代理：

```bash
wsl bash -lc 'cd /mnt/c/Users/bestz/Documents/projects/Atlas && ./.venv-new/bin/python -m pytest tests -q'
```

- **新结构一律使用 `.venv-new`**（Python 3.13.9，由 `uv sync` 按 `uv.lock` 建立，与你声明的依赖严格一致）。
  需要联网时先导出代理：`export HTTPS_PROXY=http://127.0.0.1:7897`（直连会因 fake-IP DNS 失败，**必须走代理**）。
- **`.venv` 只服务旧系统副本**（`../Atlas-legacy`），**不要动它、不要对它执行 `uv sync`**。
- 新增依赖：改 `pyproject.toml` → `uv lock` → `uv sync`，全部只作用于 `.venv-new`。
- **旧系统可跑副本**：`../Atlas-legacy`（git worktree @ `main`），共享 `data/` 与 `.venv`。
  采集入口：`PYTHONPATH=src ./.venv/bin/python -m atlas collect`
- **归档基线**：tag `archive/growth-baseline-2026-09-25`（目标重梳理前的完整历史，被弃用的代码都可从这里取回）。

## 不要做的事

- 不要在 `SPEC.md` 之外另建进度文档、任务清单或测试报告模板。
- 不要把 `data/` 纳入 git（它本地保留，且运行中的 Docker 旧栈挂载了它）。
- 不要用"简化实现""先占位后面补"的方式交付——归档基线里那 25,937 行代码就是这么来的。
