"""Atlas 测试包。

存在理由：让 `from tests.<module> import ...` 在仓库根目录（以及 worktree 复核路径）
下都能稳定工作——pytest 的 `rootdir` 会把仓库根加进 `sys.path`，
因此显式包名比依赖"两个测试目录同名模块"的隐式解析更可靠。
"""
