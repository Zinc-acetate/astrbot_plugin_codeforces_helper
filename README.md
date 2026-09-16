# astrbot_plugin_codeforces_helper

Codeforces 训练、Rating 缓存、排行榜、比赛提醒、赛后战报、定时播报与 Web 管理插件，适用于 AstrBot 的 QQ（aiocqhttp/OneBot）接入。

## 插件信息

- 插件 ID：`astrbot_plugin_codeforces_helper`
- 显示名称：`Codeforces 训练助手`
- 当前版本：`1.3.4`
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
- 按白名单、比赛类型和自定义提前量向指定 QQ 群发送比赛提醒。
- 保存启用后出分的比赛战报，按排名生成成员表格图片，支持立即或定时发送。
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
- `webui_auto_start`：插件启动或热重载后是否自动打开管理后台；后台启动、关闭命令会同步更新该选项。
- `admin_qq_id`：用于接收重要错误通知的管理员 QQ 号。
- `contest_reminder`：比赛订阅提醒设置，包含启用开关、群聊白名单、提醒时间和比赛类型过滤。
- `contest_report`：赛后战报设置，默认关闭，可选择立即或定时发送，并设置发送群。
- `cf_api_key`：可选的 Codeforces API Key。
- `cf_api_secret`：与 API Key 配套的可选 Secret。

对应服务器配置文件为：

```text
/AstrBot/data/config/astrbot_plugin_codeforces_helper_config.json
```

Codeforces API 凭证属于敏感信息，请只在 AstrBot 配置页面或服务器本地配置文件中填写，禁止提交到公开仓库。

比赛提醒默认关闭。启用后，`group_whitelist` 只允许手动配置的群号接收通知，非白名单群聊不会收到提醒。`reminder_times` 使用空格分隔多组时间，纯数字默认按小时解析，并支持 `1min`、`1s`、`1h`、`1d` 等写法；允许范围为 1 秒至 365 天，默认值 `24 1` 表示提前 24 小时和 1 小时提醒。Div. 2、Div. 3、Div. 4、Educational 和其他比赛均可在插件设置界面独立开关。

## 赛后战报

在 AstrBot 插件配置的“赛后战报”中设置：

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `enabled` | `false` | 是否启用出分检测及战报发送 |
| `send_mode` | `immediate` | `immediate` 为发现出分并生成图片后立即发送；`scheduled` 为定时发送 |
| `send_time` | `09:00` | 定时模式下北京时间每天的发送时刻，格式 `HH:MM` |
| `group_whitelist` | `[]` | 留空沿用 `/acm set group` 配置的播报群；填写后只向列表中的群发送 |

- 抓取与现有数据同步共用 `sync_interval_minutes`，默认 60 分钟，可在后台修改为 5～1440 分钟。每轮自动同步结束后检查战报；立即发送指检测到新出分后发送。
- 按官方 `ratingUpdateTimeSeconds` 判断启用边界，不按开赛时间过滤。启用时间持久化，热重载不会重置；关闭会取消本轮待发记录，再次开启重新起算，不补发关闭期间的场次。
- 每轮最多核查 8 场候选，优先近期已结束比赛，较早候选持续保留并分批续查。空返回、未出分和暂时失败不会被标成已完成。
- 同一比赛只生成一次快照。一轮发现多场出分时，各场分别存储、分别发送；没有本地成员实时参赛数据的比赛不发送。
- 统计 `CONTESTANT` 和 `OUT_OF_COMPETITION`，因此超出计分范围、现场参加但不计 Rating 的成员也会展示。排除虚拟比赛、赛后练习及管理者测试提交。多个 QQ 绑定同一 Handle 时，图片仅列一行。
- 图片标题为比赛名称，列依次为无表头的序号、`Rank`、`Handle`、`Solved`、`Rating change`、`New rating`。计分成员按 Rank 升序排列；未计分成员统一放在最后，按 Solved 降序排列。
- Rank 和 Rating 变动取自 `user.rating`，与官网个人 Contests 页一致，不使用受限公共 standings 中重新编号的 Rank。成员参赛类型及比赛时段内的通过记录通过 `user.status` 分页核对；不计入赛后补题。
- 未计分成员的序号、Rank、Rating change 和 New rating 四列均显示 `—`，Solved 保留实际题数；计分成员实际涨跌为零时显示 `0`。
- 多场比赛共享一次成员资料获取，按需要分页覆盖最早比赛起点。请求失败或数据不完整时保留待处理比赛，不会把缺失数据当成未参赛或零过题。
- 定时战报在抓取后的下一个设定时刻发送。待发快照和逐群、逐页发送确认保存在数据库中；发送失败、重启或定时任务错过时，由现有一分钟设置检查任务继续处理已到期记录。
- 未配置发送群时保存待发记录；人数较多时每 40 人一页，保持全场排序和序号。`/acm status` 可查看开关和发送方式。

从旧版本升级后，更新并重载插件，在 AstrBot 插件配置中开启 `contest_report.enabled`，再选择发送方式和群聊即可。战报默认关闭，升级本身不会开启；`/acm report on|off` 只控制近期过题播报，赛后战报使用独立开关。

图片样例与验收结果见 [1.3.4 验收报告](review/2026-09-16/ACCEPTANCE.md)。

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

执行 `/acm 后台启动` 会把 `webui_auto_start` 保存为开启，执行 `/acm 后台关闭` 会保存为关闭。AstrBot 保存插件设置并热重载插件时，旧后台进程会正常关闭；若该选项为开启，新插件实例初始化后会自动启动后台。

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

CF Handle 仅接受 3 至 24 位字母、数字、下划线、点和连字符。多个 QQ 可以绑定同一个 Handle，Rating 会同步更新。切换或清空 Handle 会在同一事务内清理旧账号的过题、提交判决、播报确认记录和 Rating 缓存；仅修改大小写会保留数据。

## 命令

### 查询命令

| 命令 | 说明 |
| --- | --- |
| `/acm rank` | 显示近 7 日文本 Top 10 |
| `/acm rank all` | 显示生涯文本 Top 10 |
| `/acm hourly [小时数]` | 查询近期过题 |
| `/acm contest` | 查询近期 Codeforces 比赛 |
| `/acm rating <Handle>` | 查询本地 Rating 缓存 |
| `/acm rating榜 [当前\|历史]` | 生成按 Rating 着色的当前或历史最高图片榜 |
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
| `/acm report on\|off` | 启用或关闭近期过题播报 |
| `/acm set hourly_limit <数量>` | 设置近期播报条数上限 |
| `/acm 后台启动` | 启动 Web 管理后台 |
| `/acm 后台关闭` | 关闭 Web 管理后台 |

涉及删除、深度同步、播报设置和运行状态的命令受 AstrBot 管理员权限控制。

## 自动同步与数据

- 默认同步间隔为 60 分钟。
- Web 后台可设置为 5 至 1440 分钟。
- 配置保存后最多约一分钟应用。
- 新成员默认分页补全近 30 天记录，之后在增量游标前保留 2 天重叠窗口；仍在判题或仅通过预判的提交会持续复查。预判 WA/TLE 等终态失败由周期性历史复核处理，避免每轮重扫旧历史；升级时会自动纠正旧库的错误待判标记。
- 每 7 天复核本地已经覆盖的历史区间，纠正较晚发生的重判。升级后的第一次同步也会复核旧库已声明的覆盖范围；API 失败时保留旧数据并重试。
- 手动更新留空时采用同一套策略；指定天数时执行深度回查，并同时补齐旧游标至今的缺口。
- 所有入口统一分页至时间边界，通过 Windows/Linux 跨进程锁、数据库事务和唯一约束防止并发写入与重复统计。
- 原始提交按提交 ID 保存判决，榜单按本地已覆盖历史中每题的首次有效 AC 计数。同题重复 AC 不会因导入顺序不同改变榜单，重判失效时仍保留该题其他有效 AC。
- “总榜/生涯榜”表示全部本地已同步记录；默认首次回查 30 天并不等于完整账号生涯。需要更早历史时应指定同步天数。
- 主插件与 WebUI 子进程共享 Codeforces API 限流状态，请求启动间隔不少于 2.1 秒；遇到官方限流响应时最多自动重试 2 次。
- 成员资料修改和删除与同步任务互斥；同步期间提交修改会返回忙碌提示，避免旧 Handle 的数据写入新账号。
- API 请求未完整成功时不会推进同步游标，避免失败期间的记录被跳过。
- Rating 请求会去重分批；发现无效 Handle 后拆批隔离，其他成员仍能更新。后台及聊天全员同步分别显示提交和 Rating 的失败人数；聊天单人同步分别显示两部分结果，Rating 失败不影响已成功保存的提交记录。
- 定时过题播报按群保存发送确认，延迟入库的记录和发送失败记录会在后续周期继续处理；条数上限仅限制单次发送，不会丢弃剩余记录。首次启用只纳入过去一小时及其后的提交，避免导入旧历史时刷屏。
- 比赛列表每 10 分钟刷新一次，提醒本身由一次性定时任务精确触发，不会因秒级提醒配置而高频请求 Codeforces API。
- 所有定时任务使用 `Asia/Shanghai` 时区；聊天查询、比赛提醒和 Web 后台缓存时间统一显示北京时间，不依赖服务器或浏览器的本地时区。

运行数据库位于 AstrBot 的独立插件数据目录：

```text
data/plugin_data/astrbot_plugin_codeforces_helper/codeforces_helper.db
```

数据库保存成员资料、Rating 缓存、原始提交判决、首次有效 AC、同步覆盖起点、群播报确认、赛后战报启用状态与比赛快照、逐群逐页发送确认、同步与播报设置以及后台密码哈希。新增表会在启动时自动迁移，旧 AC 数据在成功复核前保留。已保存的赛后战报作为历史快照独立保留。该目录独立于插件安装目录，从 GitHub 更新、重装或替换插件源码时不会被覆盖。

同一目录中的 `codeforces_api_rate_limit.db` 和 `codeforces_helper.sync.lock` 仅用于跨进程协调 API 请求与同步任务，不保存成员资料。

从 1.0.0 及更早版本首次升级时，插件会在启动阶段将旧路径 `data/plugins/astrbot_plugin_codeforces_helper/data/codeforces_helper.db` 安全复制到新路径：使用 SQLite 在线备份接口、执行完整性检查并原子启用新数据库。迁移完成后旧文件会保留用于回滚；新路径一旦存在，后续启动始终以新数据库为准，不会被旧文件覆盖。更新前仍建议额外备份重要数据。

数据库、实际配置、日志、缓存和用户数据均不纳入 Git。

## 安全说明

- 不要公开 CF API Secret、AstrBot 配置文件、数据库或用户资料。
- Web 管理接口使用管理员会话保护，后台密码以哈希形式保存。
- Web 会话密钥在后台进程启动时随机生成。
- 修改密码会撤销其他设备的管理员会话，当前修改密码的会话继续有效。
- 网页成员字段会编码 HTML 属性中的引号，HTML 页面和 API 响应不缓存。升级后应重启插件并强制刷新一次后台页面，清除旧版本此前留下的浏览器缓存。
- 对外开放端口前应启用 HTTPS、访问控制和防火墙规则。
- 批量删除、深度同步或数据库结构调整前建议停止写入并备份数据库。

## 上游项目与修改说明

本插件基于 `astrbot_plugin_acm_helper` 开发和持续维护：

- 上游仓库：<https://github.com/FCYXSZY/astrbot_plugin_acm_helper>
- 上游 metadata 标注作者：`suzakudry`
- 上游仓库所有者：`FCYXSZY`
- 上游许可证：AGPL-3.0

本项目于 2026 年 7 月 17 日在上游基础上进行了 Codeforces 专项化、插件身份独立、数据模型与同步逻辑调整、Rating 缓存、排行榜和 WebUI 重构、安全加固及文档维护。详细说明见 `NOTICE.md`。

## 许可证

本项目及其上游代码按照 [GNU Affero General Public License v3.0](LICENSE) 发布。分发修改版本或通过网络向用户提供其功能时，请遵守 AGPL-3.0 的源代码提供、许可证保留和修改说明等义务。

## 问题反馈与贡献

开发验收可在安装 `requirements.txt` 后执行以下命令；前端测试额外需要 Node.js：

```text
python -W error -m unittest discover -s tests -v
node --test tests/test_frontend.cjs
git diff --check
```

测试使用临时数据库，AstrBot 宿主和网络响应由测试替身提供；真实 SQLite、Quart、Pillow、APScheduler 及本机进程锁保留。前端测试执行实际页面脚本并控制响应顺序。最新验收记录见 [1.3.4 验收报告](review/2026-09-16/ACCEPTANCE.md)，可运行 `python -X utf8 -B review/2026-09-16/verify_release.py` 复验。

版本更新记录见 [CHANGELOG.md](CHANGELOG.md)。

欢迎通过仓库的 Issues 反馈问题，也欢迎提交 Pull Request：

<https://github.com/Zinc-acetate/astrbot_plugin_codeforces_helper/issues>
