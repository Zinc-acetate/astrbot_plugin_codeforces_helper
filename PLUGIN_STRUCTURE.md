# astrbot_plugin_codeforces_helper 结构说明

## 定位

面向 AstrBot QQ（aiocqhttp/OneBot）接入的 Codeforces 训练辅助插件，负责成员管理、AC 记录同步、Rating 缓存、排行榜、定时播报与 Web 管理后台。

## 核心文件

- `main.py`：插件入口、数据库初始化、定时任务、聊天命令和图片排行榜。
- `core/crawler.py`：Codeforces `user.status` 与 `user.info` 请求、AC 记录及 Rating 缓存。
- `backend/api.py`：排行榜、后台登录、成员管理和手动同步接口。
- `webui.py`：Quart Web 服务入口。
- `public/index.html`：Web 仪表盘与管理后台。
- `_conf_schema.json`：命令提示、Web 端口、管理员 QQ 与可选 CF API 凭证。
- `metadata.yaml`：AstrBot 插件市场和加载元数据。
- `NOTICE.md`：上游来源与修改声明。
- `data/plugin_data/astrbot_plugin_codeforces_helper/codeforces_helper.db`：位于插件安装目录之外的 SQLite 持久化数据库，不纳入 Git。

## 插件身份

- 插件 ID：`astrbot_plugin_codeforces_helper`
- 目录名：`astrbot_plugin_codeforces_helper`
- 配置文件：`data/config/astrbot_plugin_codeforces_helper_config.json`
- 仓库：<https://github.com/Zinc-acetate/astrbot_plugin_codeforces_helper>

插件 ID、目录名和配置文件名应保持一致。AstrBot 依据目录名确定插件配置文件路径。

## 数据模型

- `users`：QQ、姓名、CF Handle、身份、学校、同步时间、当前及最高 Rating 和段位。
- `submissions`：仅保存 `platform='codeforces'` 的去重 AC 记录。
- `settings`：同步间隔、播报设置和后台密码哈希等运行设置。

## 同步流程

- 普通同步通过 `user.status` 拉取增量 AC 记录。
- 深度同步分页拉取指定天数内的 AC 记录。
- `user.info` 更新当前 Rating、最高 Rating 与对应段位。
- 定时任务默认每 60 分钟同步，可在 Web 后台设置为 5 至 1440 分钟。
- 聊天命令和网页查询优先读取本地缓存。

## Web 与安全

- 公开排行榜支持近 7 日、近 30 日、当前 Rating 和历史最高 Rating。
- 后台使用密码会话保护；成员修改、同步和删除接口要求管理员会话。
- 数据库、配置、凭证、日志、缓存和备份不纳入 Git。
- 对外开放 WebUI 时应通过反向代理启用 HTTPS 和访问控制。

## 上游

本插件基于 <https://github.com/FCYXSZY/astrbot_plugin_acm_helper> 修改并独立维护，遵循 AGPL-3.0。详细修改声明见 `NOTICE.md`。
