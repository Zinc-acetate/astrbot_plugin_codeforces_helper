# 上游来源与修改声明

本项目 `astrbot_plugin_codeforces_helper` 是 `astrbot_plugin_acm_helper` 的修改和独立维护版本。

## 上游信息

- 项目：`astrbot_plugin_acm_helper`
- 仓库：<https://github.com/FCYXSZY/astrbot_plugin_acm_helper>
- 上游仓库所有者：`FCYXSZY`
- 上游 `metadata.yaml` 标注作者：`suzakudry`
- 上游许可证：GNU Affero General Public License v3.0（AGPL-3.0）

## 当前项目

- 项目：`astrbot_plugin_codeforces_helper`
- 仓库：<https://github.com/Zinc-acetate/astrbot_plugin_codeforces_helper>
- 维护者：`Zinc-acetate`
- 许可证：GNU Affero General Public License v3.0（AGPL-3.0）

## 主要修改

当前项目在上游代码基础上进行了持续修改，包括但不限于：

- 移除非 Codeforces 平台功能，调整为 Codeforces 专项插件。
- 统一插件 ID、目录名、配置名和公开仓库名称。
- 调整数据库结构、同步流程和去重逻辑。
- 增加当前及历史最高 Rating、段位与更新时间缓存。
- 增加多维排行榜、图片榜、定时同步和播报设置。
- 重构 Web 排行榜与管理后台，增加日间和夜间主题。
- 增加后台会话鉴权、密码哈希及管理接口保护。
- 完善运行数据隔离、敏感信息忽略规则和发布文档。

本声明用于保留上游来源并说明修改版本。完整授权条款见仓库根目录的 `LICENSE`。
