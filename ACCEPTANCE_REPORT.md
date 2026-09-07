# 已知问题修复与验收

日期：2026-09-07。基于提交 `20a6c172a0e7d6ca29d44c357ddd1835761d8f6c` 的本地修改，更新说明位于 `CHANGELOG.md` 的“未发布”条目。

结论：审查报告列出的 10 项问题及会话撤销缺口已修复，本地自动化、隔离网页和公共 Codeforces API 验收通过。尚未部署到真实 AstrBot/QQ 实例，也未进行 Linux 实机验收。

## 验收结果

| 检查 | 结果 |
| --- | --- |
| 全量自动化测试 | **71 项通过，0 失败，0 错误，0 跳过**，耗时 12.397 秒 |
| Windows 原生进程锁 | 同进程排他、异常退出释放、独立子进程排他及释放后重新获取均通过 |
| 真实数据库与 Web 框架 | SQLite、aiosqlite、Quart；不再替换这些组件 |
| 旧库迁移 | 数据保留、升级覆盖起点、失败保留旧记录、成功纠正及重复启动均通过，完整性检查为 `ok` |
| 事务和游标 | 分页失败、无效响应、写入中途失败均不会破坏旧快照或跳过同步区间 |
| 播报 | 迟入库、发送失败重试、条数限额、重启后去重、并发互斥通过；发送对象为模拟 OneBot |
| 图片和调度器 | 真实 Pillow 生成两种 PNG 榜单；真实 APScheduler 初始化、任务注册和关闭通过 |
| 浏览器 | 正常登录/保存/刷新通过；非法 Handle 拒绝；旧恶意字段显示为文本，无注入事件属性、无执行标记 |
| 实网 API | 混合有效与无效 Handle，经过 3 次受限流约束的请求，有效成员成功刷新、无效成员单独失败 |
| 静态检查 | Python 编译、网页 2 段 JavaScript 语法、`git diff --check` 均通过 |

测试环境：Windows，Python 3.10.10，SQLite 3.39.4，aiohttp 3.14.1，aiosqlite 0.22.1，Quart 0.20.0，APScheduler 3.11.3，Pillow 12.0.0。

详细证据与源文件 SHA-256：[acceptance.json](review/2026-09-07/acceptance.json)。实网记录：[live-api.json](review/2026-09-07/live-api.json)。本机完整测试输出：[tests.log](review/2026-09-07/tests.log)（日志按仓库规则不纳入 Git）。

## 问题逐项处理

| 原问题 | 修复后的行为 |
| --- | --- |
| 判题延迟导致增量漏题 | 保存提交 ID 和判决；增量回看 2 天，对待判及预判记录持续复查，超过重叠窗口仍会回查 |
| 短天数手动同步跳过历史 | 主插件、后台共用同步计划，深度回查同时补齐旧游标至今的缺口 |
| 定时播报漏报 | 按群持久化成功发送的记录；迟入库或发送失败后继续处理，单次限额不会丢弃剩余记录 |
| 网页属性注入 | 对引号等属性字符编码，同时校验新增 Handle；旧库中恶意字段也不能生成事件属性 |
| Windows 导入失败 | Windows 使用 `msvcrt`，Unix 使用 `fcntl`，均执行真实排他锁 |
| 切换 Handle 残留 Rating | 同一事务清理原始提交、AC 汇总、发送确认、当前/最高 Rating、段位、时间戳及同步状态 |
| 无效 Handle 阻断整批 Rating | 请求去重分批，对账号错误拆批隔离；普通网络故障不会触发逐成员重试风暴 |
| 共享 Handle 只更新最后一人 | 保留 Handle 到多名成员的映射，忽略大小写且每人更新一次 |
| 导入顺序改变周榜 | 根据原始提交计算本地覆盖范围内的首次有效 AC，深度回查可纠正已有时间 |
| 重判后仍保留错误 AC | 重建有效 AC 汇总；某题还有其他有效提交时继续保留，全部失效时移除 |
| 修改密码不撤销旧会话 | 持久化会话版本，修改密码后其他会话失效；登录同时读取密码和版本，避免并发换密绕过撤销 |

另外修复了验收发现的两点：管理页面/API 不缓存；CF 请求固定协商 `gzip, deflate`，避开本机实测的 `br` 响应解压错误。改用 gzip 后，同一公共 API 成功读取，完整批量隔离验收也通过。

## 数据与运行说明

- 新增 `cf_submission_records`、`solve_report_state`、`solve_report_receipts`，以及用户同步覆盖起点/复核时间；启动时自动迁移。旧记录不会因 API 请求失败而被清空。
- 每 7 天复核本地已覆盖历史，升级后的第一次同步也会复核旧覆盖区间。较晚重判会在下一次成功复核中纠正；超出已同步范围的更早历史仍需指定天数补抓。
- 原始提交存储和历史复核会增加数据库容量及部分周期的 API 请求量，继续共用 2.1 秒请求间隔。
- 定时播报与 `/acm hourly` 分开：前者处理未发送记录，后者仍严格查询指定小时窗口。旧审查脚本直接查询时间窗口的播报用例已由实际发送入口的回归测试替代。
- 正常发送确认后不重复播报；若进程恰在 QQ 接收成功与本地确认写入之间退出，无法保证严格一次发送。当前接口未提供可用的消息幂等确认机制。
- 更新运行实例时，需要重启/重载插件并强制刷新一次后台网页，清除旧版本已留下的浏览器缓存。

## 复验命令

安装 `requirements.txt` 后，在仓库根目录执行：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py webui.py core backend tests
git diff --check
```

本次使用本地 `.venv`。完整修复回归主要在 [test_runtime_regressions.py](tests/test_runtime_regressions.py)，原生锁测试在 [test_sync_lock.py](tests/test_sync_lock.py)。只替换不可用的 AstrBot 宿主和模拟网络/发送响应，数据库、Web 框架、锁、图片库、调度器保留真实实现。故障注入测试中的断网、发送失败及关闭定时任务时的取消日志不代表用例失败。

可选实网与隔离网页复验：

```powershell
python review/2026-09-07/live_api_check.py
python review/2026-09-07/serve_fixture.py
```

后者只监听 `127.0.0.1:18788`，使用临时数据库，按 Ctrl+C 结束。此次验收的临时服务和浏览器页面已关闭，未改动现场数据库、账号配置或发送真实 QQ 消息。

## 验证边界

本机没有完整 AstrBot/OneBot 接入实例，也没有已安装的 Linux 运行环境；真实 QQ 收发、宿主热重载和 Linux 实机结果不在本次“通过”的范围内。验收在本地工作区完成，发布与部署状态以实际 Git 记录和运行实例为准。

实现依据：[Codeforces 提交状态](https://codeforces.com/apiHelp/objects#Submission)、[Python Windows 文件锁](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)。
