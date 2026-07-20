# astrbot_plugin_codeforces_helper

Codeforces 训练、Rating 缓存、排行榜、定时播报与 Web 管理插件，适用于 AstrBot 的 QQ（aiocqhttp/OneBot）接入。

## 插件信息

- 插件 ID：`astrbot_plugin_codeforces_helper`
- 显示名称：`Codeforces 训练助手`
- 当前版本：`1.2.0`
- 维护者：`Zinc-acetate`
- 命令组：`/acm`
- 功能范围：仅面向 Codeforces；`/acm` 作为历史兼容命令前缀保留，不代表插件仍支持其他 OJ。
- 仓库：<https://github.com/Zinc-acetate/astrbot_plugin_codeforces_helper>
- 许可证：GNU Affero General Public License v3.0（AGPL-3.0）

## 功能

- 增量同步或按指定天数深度同步 Codeforces AC 记录。
- 缓存当前 Rating、历史最高 Rating 及对应段位。
- 提供近 7 日、近 30 日、当前 Rating、历史最高 Rating 排行榜。
- 提供群聊文本榜、图片榜、近期过题和近期比赛查询。
- 按统一间隔自动更新过题记录与 Rating。
- 按设定时间向指定 QQ 群播报近期过题。
- 提供带管理员会话保护的 Web 排行榜与成员管理后台。
- 支持批量新增、更新、删除成员和手动同步。
- 支持响应式布局以及可持久化的日间、夜间主题。

## 安装

### 从 AstrBot 插件市场安装

插件上架后，可在 AstrBot WebUI 的插件市场中搜索“Codeforces 训练助手”并安装。

### 从 GitHub 安装

在 AstrBot WebUI 的插件管理页面使用以下仓库地址安装：

```text
https://github.com/Zinc-acetate/astrbot_plugin_codeforces_helper
```

也可以手动克隆到 AstrBot 插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/Zinc-acetate/astrbot_plugin_codeforces_helper.git
```

安装依赖并重启或重载 AstrBot 后生效。仓库根目录必须保持为插件目录，目录名建议与插件 ID 一致。

## 运行要求

- AstrBot `>=4.0.0,<5.0.0`
- Python 3.10 或更高版本
- QQ（aiocqhttp/OneBot）平台接入
- 服务器能够访问 Codeforces API
- Python 依赖见 `requirements.txt`

## AstrBot 配置

在 AstrBot WebUI 的插件配置页面设置：

- `command_prefix`：命令前缀提示项；当前命令组为 `/acm`。
- `webui_port`：Web 管理后台端口，默认 `8088`。
- `admin_qq_id`：用于接收重要错误通知的管理员 QQ 号。
- `cf_api_key`：可选的 Codeforces API Key。
- `cf_api_secret`：与 API Key 配套的可选 Secret。

对应服务器配置文件为：

```text
/AstrBot/data/config/astrbot_plugin_codeforces_helper_config.json
```

Codeforces API 凭证属于敏感信息，请只在 AstrBot 配置页面或服务器本地配置文件中填写，禁止提交到公开仓库。

## Web 管理后台

管理员在聊天中执行：

```text
/acm 后台启动
```

默认访问地址：

```text
http://服务器IP:8088
```

关闭后台：

```text
/acm 后台关闭
```

数据库首次初始化时，Web 后台默认密码为 `123456`。首次登录后必须立即修改，新密码至少 6 位。公网开放时建议使用 HTTPS 反向代理、访问控制和防火墙白名单。

后台支持：

- 四种排行榜模式切换。
- 姓名与 Handle 搜索。
- Codeforces Handle 主页跳转与官方 Rating 颜色。
- 批量新增或更新成员。
- 删除成员及其本地过题记录。
- 更新选中成员、更新全部成员、指定天数深度同步。
- 修改同步间隔与后台密码。

## 成员数据格式

在管理后台中每行填写：

```text
QQ号,姓名,CF Handle,身份,学校
```

QQ 号与姓名必填，其余字段可留空。例如：

```text
123456789,张三,tourist,正式队员,示例大学
```

相同 QQ 号会更新已有资料。

## 命令

### 查询命令

| 命令 | 说明 |
| --- | --- |
| `/acm rank` | 显示近 7 日文本 Top 10 |
| `/acm rank all` | 显示生涯文本 Top 10 |
| `/acm hourly [小时数]` | 查询近期过题 |
| `/acm contest` | 查询近期 Codeforces 比赛 |
| `/acm rating <Handle>` | 查询本地 Rating 缓存 |
| `/acm rating榜 [当前\|历史]` | 显示当前或历史最高 Rating 排行榜 |
| `/acm 查询 <QQ号>` | 查看成员最近 20 条过题 |
| `/acm 过题 <身份> [天数]` | 生成指定身份图片榜 |
| `/acm past <天数>` | 生成指定天数图片榜 |
| `/acm 总榜` | 生成生涯图片榜 |

### 管理员命令

| 命令 | 说明 |
| --- | --- |
| `/acm status` | 查看运行状态 |
| `/acm sync_user <QQ号> <天数>` | 同步指定成员 |
| `/acm sql <天数>` | 深度同步全部成员并生成榜单 |
| `/acm del_user <QQ号>` | 删除成员及其本地记录 |
| `/acm set group <群号>` | 设置播报群 |
| `/acm set cron <小时> <分钟>` | 设置播报时间 |
| `/acm report on\|off` | 启用或关闭播报 |
| `/acm set hourly_limit <数量>` | 设置近期播报条数上限 |
| `/acm 后台启动` | 启动 Web 管理后台 |
| `/acm 后台关闭` | 关闭 Web 管理后台 |

涉及删除、深度同步、播报设置和运行状态的命令受 AstrBot 管理员权限控制。

## 自动同步与数据

- 默认同步间隔为 60 分钟。
- Web 后台可设置为 5 至 1440 分钟。
- 配置保存后最多约一分钟应用。
- 新成员及升级后的既有成员首次成功更新会分页补全近 30 天记录，之后按同步游标增量抓取，避免每轮重复请求整段历史。
- 手动更新留空时采用同一套“30 天补全后增量”策略；指定天数时执行分页深度回查。
- 所有入口统一分页至时间边界，并通过跨进程锁、数据库唯一约束及 `INSERT OR IGNORE` 防止并发和重复入库。
- API 请求未完整成功时不会推进同步游标，避免失败期间的记录被跳过。
- 所有定时任务使用 `Asia/Shanghai` 时区。

运行数据库位于 AstrBot 的独立插件数据目录：

```text
data/plugin_data/astrbot_plugin_codeforces_helper/codeforces_helper.db
```

数据库保存成员资料、Rating 缓存、去重 AC 记录、同步与播报设置以及后台密码哈希。该目录独立于插件安装目录，从 GitHub 更新、重装或替换插件源码时不会被覆盖。

从 1.0.0 及更早版本首次升级时，插件会在启动阶段将旧路径 `data/plugins/astrbot_plugin_codeforces_helper/data/codeforces_helper.db` 安全复制到新路径：使用 SQLite 在线备份接口、执行完整性检查并原子启用新数据库。迁移完成后旧文件会保留用于回滚；新路径一旦存在，后续启动始终以新数据库为准，不会被旧文件覆盖。更新前仍建议额外备份重要数据。

数据库、实际配置、日志、缓存和用户数据均不纳入 Git。

## 安全说明

- 不要公开 CF API Secret、AstrBot 配置文件、数据库或用户资料。
- Web 管理接口使用管理员会话保护，后台密码以哈希形式保存。
- Web 会话密钥在后台进程启动时随机生成。
- 对外开放端口前应启用 HTTPS、访问控制和防火墙规则。
- 批量删除、深度同步或数据库结构调整前建议停止写入并备份数据库。

## 上游项目与修改说明

本插件基于 `astrbot_plugin_acm_helper` 开发和持续维护：

- 上游仓库：<https://github.com/FCYXSZY/astrbot_plugin_acm_helper>
- 上游 metadata 标注作者：`suzakudry`
- 上游仓库所有者：`FCYXSZY`
- 上游许可证：AGPL-3.0

本项目在上游基础上进行了 Codeforces 专项化、插件身份独立、数据模型与同步逻辑调整、Rating 缓存、排行榜和 WebUI 重构、安全加固及文档维护。详细说明见 `NOTICE.md`。

## 许可证

本项目及其上游代码按照 [GNU Affero General Public License v3.0](LICENSE) 发布。分发修改版本或通过网络向用户提供其功能时，请遵守 AGPL-3.0 的源代码提供、许可证保留和修改说明等义务。

## 问题反馈与贡献

欢迎通过仓库的 Issues 反馈问题，也欢迎提交 Pull Request：

<https://github.com/Zinc-acetate/astrbot_plugin_codeforces_helper/issues>
