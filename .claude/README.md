# Claude Code 配置目录

本目录包含 Atlas 项目的 Claude Code 开发配置。

## 📁 文件说明

### `config`
主配置文件，定义开发规范和文档规则。

**包含内容**：
- 📂 文档目录映射和分类规则
- ✅ 任务完成后的必做事项
- 🔍 文档创建检查清单
- 🚫 禁止的操作列表
- 📋 Git提交规范
- ⚠️ 开发提醒

## 🎯 使用方式

Claude Code 会自动读取此配置，在开发过程中：

1. **任务完成后**：提醒创建测试报告和更新状态
2. **创建文档前**：检查文档类型和目录
3. **提交代码前**：验证文档是否更新
4. **阶段转换时**：提醒归档和创建新文档

## 📋 快速参考

### 当前项目状态
```ini
[current_state]
stage = Growth
phase = Phase 1: 核心基础设施
current_tasks = [
    "TASK-001: MinIO集成 (已完成)",
    "TASK-002: PostgreSQL迁移 (进行中 67%)"
]
```

### 常用命令
```bash
# 查看配置
cat .claude/config

# 查看当前任务
cat docs/tasks/current-backlog.md

# 查看当前进度
cat docs/project/milestones/current-sprint.md
```

### 文档操作提醒
- ✅ 任务完成：创建测试报告
- ✅ 状态更新：更新 current-backlog.md
- ✅ 阶段转换：归档旧文档
- ⚠️ 禁止重复：检查已有文档

## 🔄 更新配置

配置随项目演进更新：

- **MVP → Growth**: 已更新（2025-12-25）
- **Growth → Scale**: 待更新

## 📞 相关文档

- [CLAUDE.md](../CLAUDE.md) - Claude开发指令
- [docs/documentation-system.md](../docs/documentation-system.md) - 文档体系规范
- [docs/README.md](../docs/README.md) - 文档中心

---

**最后更新**: 2025-12-25
**维护者**: Claude Sonnet
