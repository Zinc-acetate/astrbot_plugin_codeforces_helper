# Codeforces Helper

Codeforces Helper 是 Zinc-acetate 开发和维护的 AstrBot 插件，提供 Codeforces 刷题记录同步、Rating 缓存、训练排行榜、定时播报与网页管理控制台。

## 插件信息

- 插件名称：`astrbot_codeforces_helper`
- AstrBot 注册 ID：`astrbot_codeforces_helper`
- 作者：`Zinc-acetate`
- 版本：`1.0`
- 命令组：`/acm`
- 数据平台：Codeforces

## 主要功能

- 增量或按指定天数同步成员的 Codeforces AC 记录。
- 缓存当前 Rating、最高 Rating 和对应段位。
- 提供近 7 日、近 30 日、当前 Rating、历史最高 Rating 排行榜。
- 提供群聊文本榜、图片榜、近期过题查询和比赛查询。
- 按统一时间间隔自动更新刷题记录与 Rating。
- 按设定时间向指定 QQ 群播报近期过题。
- 提供带密码会话保护的网页管理控制台。
- 支持批量新增、更新、删除成员和手动深度同步。
- 提供日间与夜间主题，并将用户主题选择保存在浏览器本地。

## 配置

AstrBot 配置文件：

`/AstrBot/data/config/astrbot_codeforces_helper_config.json`

可配置项目：

- `command_prefix`：命令前缀配置项；当前命令组固定为 `/acm`。
- `webui_port`：网页控制台端口，默认 `8088`。
- `admin_qq_id`：接收重要错误通知的管理员 QQ。
- `cf_api_key`：可选 Codeforces API Key。
- `cf_api_secret`：与 API Key 配套的可选 Secret。

修改端口或 API 凭证后应重载插件。若网页进程正在运行，请先关闭再重新启动。

## 网页控制台

使用管理员账号在聊天中执行：

`/acm 后台启动`

默认访问地址：

`http://服务器IP:8088`

关闭网页控制台：

`/acm 后台关闭`

数据库首次初始化时，网页后台默认密码为 `123456`。首次登录后应立即修改，且新密码至少 6 位。

网页首页提供：

- 四种可切换排行榜。
- 姓名与 Handle 搜索。
- 本地 Rating 缓存查询。
- Codeforces Handle 主页链接和官方 Rating 颜色。
- 响应式桌面与移动端布局。
- 可持久化的日间、夜间主题。

## 成员管理

在管理控制台中按行批量填写：

`QQ号,姓名,CF Handle,身份,学校`

QQ 号与姓名必填，CF Handle、身份和学校可留空。例如：

`123456789,张三,tourist,正式队员,示例大学`

相同 QQ 号会更新原资料。删除成员时会同时删除该成员的本地过题记录。

## 群聊命令

普通查询：

- `/acm rank`：近 7 日文本 Top 10。
- `/acm rank all`：生涯文本 Top 10。
- `/acm hourly [小时数]`：查询近期过题。
- `/acm contest`：查询近期 Codeforces 比赛。
- `/acm rating <Handle>`：查询本地 Rating 缓存。
- `/acm 查询 <QQ号>`：查看成员最近 20 条过题。
- `/acm 过题 <身份> [天数]`：生成指定身份图片榜。
- `/acm past <天数>`：生成指定天数图片榜。
- `/acm 总榜`：生成生涯图片榜。

管理员命令：

- `/acm status`：查看运行状态。
- `/acm sync_user <QQ号> [天数]`：同步指定成员。
- `/acm sql <天数>`：深度同步全部成员并生成榜单。
- `/acm del_user <QQ号>`：删除成员及其记录。
- `/acm set group <群号>`：设置播报群。
- `/acm set cron <小时> <分钟>`：设置播报时间。
- `/acm report on|off`：启用或关闭播报。
- `/acm set hourly_limit <数量>`：设置近期播报条数上限。

## 自动同步

刷题记录与 Rating 使用统一更新间隔：

- 默认 60 分钟。
- 网页后台允许设置为 5 至 1440 分钟。
- 设置保存后最多一分钟应用，无需重启调度器。
- 定时同步使用增量抓取；指定天数同步使用分页抓取。

所有时间任务使用 `Asia/Shanghai` 时区。

## 数据文件

运行数据库：

`data/codeforces_helper.db`

数据库包含：

- `users`：成员资料、CF Handle、Rating 与同步时间。
- `submissions`：Codeforces 去重 AC 记录。
- `settings`：同步、播报与后台密码设置。

数据库和实际配置不纳入 Git。执行批量删除、深度同步或结构调整前建议先停止写入并备份数据库。

## 依赖与运行要求

- Python 3.10 或更高版本。
- AstrBot 插件运行环境。
- 服务器可访问 Codeforces API。
- Quart、Hypercorn、aiohttp、aiosqlite、APScheduler。
- Pillow：用于生成图片排行榜。

图片榜字体文件位于：

`resources/SourceHanSansSC-Bold.otf`

## 安全说明

- CF API Secret 和实际配置文件不得提交到公开仓库。
- 网页成员管理、设置、同步与删除接口需要管理员会话。
- 后台密码以哈希形式保存在数据库中。
- Web 会话密钥在网页进程启动时随机生成。
- 对外开放网页端口时，建议通过反向代理启用 HTTPS 和访问控制。

## 生效方式

插件目录或插件身份变更后，需要重载插件或重启 AstrBot。启动后检查日志，确认 `Codeforces Helper v1.0` 初始化成功，并重新启动网页控制台。
