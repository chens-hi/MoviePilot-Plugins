# MoviePilot-Plugins

个人 MoviePilot V3 插件市场仓库。

## 插件列表

| 插件 | 版本 | 说明 |
| --- | --- | --- |
| [Bark消息推送](plugins.v3/barknotify) | v1.3.1 | 使用 [Bark](https://bark.day.app) 转发 MoviePilot 通知：多用户、按用户选择通知类型、生效时间段（支持跨天）与强提醒，时段外静默推送 |

各插件的详细文档见其目录内 README；版本更新记录见 `package.v3.json` 的 `history`。

## 安装

在 MoviePilot「设置 → 插件市场」中加入本仓库地址：

```
https://github.com/chens-hi/MoviePilot-Plugins
```

保存后即可在插件市场中搜索安装。

## 说明

- 仅支持 MoviePilot V3（`system_version >= 3.0.0`）
- 插件开发与测试在本地工作区完成，发版时同步至本仓库；版本号遵循 V3 插件规范（`plugin_version` 与 `package.v3.json` 一致）
