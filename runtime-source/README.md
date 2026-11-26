# 实验 runtime 源码快照

[English](README.en.md) · [首页](../README.md)

`workswarm/`、`openjiuwen/`、`replay_agent_recorder/` 均从实验使用的原 wheel 提取，包含其全部 Python 模块、文本源码和许可证。组件文件数见 [提取汇总](snapshot-summary.json)。二进制资源、编译后的前端和精确安装请使用 [原版 ZIP](https://github.com/qwqwqwqwqwqwqwqwqe/jiuwenswarm-rewind-bench/releases/tag/runtime-20261112-173855)。上游开发仓库中未打包的开发文件不属于这次发行快照。

`package/` 提供版本清单、驱动、说明和模式配置。安装脚本要在原 ZIP 的完整解压目录运行，该目录包含 `wheels/`。对应许可证和 NOTICE 位于各组件的 `*.dist-info/licenses/`。源码对应的上游提交和 wheel 哈希见 [manifest](package/manifest.json)。
