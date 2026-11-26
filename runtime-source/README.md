# 实验 runtime 源码快照

`workswarm/` 包含 JiuwenSwarm 回退模块和 Agent 适配入口，`replay_agent_recorder/` 包含 Replay 可浏览 Python 源码，`openjiuwen/` 保留该 wheel 的许可证。完整包与三套 wheel 见 [GitHub Release](https://github.com/qwqwqwqwqwqwqwqwqe/jiuwenswarm-rewind-bench/releases/tag/runtime-20261112-173855)。

`package/` 提供版本清单、驱动、说明和模式配置。安装脚本要在原 ZIP 的完整解压目录运行，该目录包含 `wheels/`。对应许可证和 NOTICE 位于各组件的 `*.dist-info/licenses/`。源码对应的上游提交和 wheel 哈希见 [manifest](package/manifest.json)。
