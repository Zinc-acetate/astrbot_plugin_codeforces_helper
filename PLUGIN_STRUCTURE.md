# Codeforces Helper 当前结构说明

## 1. 定位

该插件是 Codeforces-only 的 ACM 训练辅助插件，负责成员管理、CF AC 记录同步、Rating 缓存、排行榜、定时播报与 Web 管理后台。

## 2. 核心文件

- `main.py`：插件入口、数据库初始化、定时任务、聊天命令和图片排行榜。
- `core/crawler.py`：Codeforces `user.status` 与 `user.info` 请求、AC 记录及分数缓存。
- `backend/api.py`：排行榜、CF 分数查询、后台登录、成员管理和手动同步接口。
- `webui.py`：Quart Web 服务入口。
- `public/index.html`：Web 仪表盘与管理后台。
- `_conf_schema.json`：命令前缀、Web 端口、管理员 QQ 与可选 CF API 凭证配置。
- `data/codeforces_helper.db`：SQLite 运行数据库，不纳入 Git。

## 3. 数据模型

`users` 保存 QQ、姓名、CF Handle、身份、学校、同步时间，以及 CF 当前分、最高分、段位和缓存更新时间。

`submissions` 只保存 `platform='codeforces'` 的去重 AC 记录。唯一键为成员、平台和题目 ID。

`settings` 保存定时播报、统一同步间隔和后台密码哈希等运行设置。

## 4. 同步流程

- 普通同步通过 `user.status` 拉取增量 AC 记录。
- 深度同步分页拉取指定天数内的 AC 记录。
- `user.info` 更新当前 Rating、最高 Rating 与对应段位。
- 刷题记录和 CF 分数使用统一更新间隔，默认 60 分钟，可在 Web 后台设置为 5 至 1440 分钟。
- 聊天命令和网页查询均优先读取本地缓存，不因普通页面刷新直接请求 CF。

## 5. Web 排行榜

公开排行榜支持四种白名单模式：

- `solved_7`：近 7 日 CF 过题数。
- `solved_30`：近 30 日 CF 过题数。
- `current_rating`：CF 当前分。
- `max_rating`：CF 历史最高分。

Handle 可点击进入对应 Codeforces 主页，并按当前 Rating 着色；最高分按历史最高 Rating 独立着色。

## 6. 后台管理

后台使用密码会话保护，支持：

- 修改统一同步间隔与管理密码。
- 按 `QQ号,姓名,CF Handle,身份,学校` 批量新增或更新成员。
- 删除成员及其过题记录。
- 更新选中成员、更新全部成员、指定天数深度同步。

## 7. CF-only 迁移记录

- 2026-07-17：全局移除非 Codeforces 功能。
- 删除其他 OJ 的爬虫、配置项、成员字段、前后端字段、排行榜列和遗留测试代码。
- 数据库迁移删除非 Codeforces 提交记录并重建 `users` 表。
- 迁移前数据库快照保存在插件 `data` 目录，文件名以 `codeforces_helper_before_cf_only_` 开头。

## 8. 安全与维护

- Git 不跟踪数据库、实际配置与敏感凭证。
- 排行榜模式使用服务端白名单，避免动态排序注入。
- 后台成员修改与同步接口必须经过管理员会话鉴权。
- CF API Key 与 Secret 为可选敏感配置，不应写入源码或文档。
