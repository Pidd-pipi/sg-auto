# sologsb 调度监控台 · 彻底重构

> 规划文档。对应服务：`http://127.0.0.1:8790`（本目录 `server.py`）。
> 本文所有相对路径均以本目录（`sologsb-monitor/`）为基准。

## Context

`http://127.0.0.1:8790` 是 `sologsb-0917` Pair-wise GSB 任务的调度监控台，代码就在本目录。当前实现有几个结构性问题：

**1. 性能已到瓶颈。** `/api/snapshot` 每次轮询返回 **1.1 MB** JSON（实测：`tasks` 字段 1.16 MB，其中
`candidates` 51.7 万字符、`sides` 23.9 万字符、`workflow` 7.9 万字符），3 秒一次全量推送。
服务端虽然 22 ms 能算完，但每次快照都要：`os.walk` 整棵任务树并对**每一层目录** `read_json(monitor/state.json)`
（[monitor_core.py:550](monitor_core.py:550)）；
每个 side 都跑一次 `ps` 子进程探活（[monitor_core.py:527](monitor_core.py:527)）；
扫 2.5 GB 的 `thread_history_1.sqlite` 且 `SELECT` 无 WHERE 无 LIMIT（[codex_sessions.py:97](codex_sessions.py:97)）。
前端则是每 3 秒把 67 个任务的完整 DOM 用 `innerHTML` 重建一遍（[static/index.html:1373](static/index.html:1373)），
`static/tasks.html` 另开一个 3 秒轮询 + 2 秒静态日志轮询，两页同时开就是 ~1.7 req/s 全量数据。
`/api/log`、`/api/history` 会把整个 2.5–8 MB 的 `stdout.jsonl` 读进内存再切尾段。
`TraceCache._entries` 永不淘汰，2610 个候选目录的状态常驻内存。

**2. 容量模型和需求不匹配。** 现在的 `capacity` 数的是"容器组数"，`maxContainers` 才是容器硬上限，
但 `containerRefillBelow=5` 用 `>=` 比较，实际在 5 个容器就停摆了，比 `maxContainers=6` 更早成为瓶颈。
启动预占位逻辑存在但散在三处（worker 自查 300 s、monitor reaper、`_sync_running_locked` 的 launching 老化），
且 `keyConcurrency.maxParallelRequests` / `reservedSlots` 只是显示，从未参与门禁。

**3. 配额没有真正的预扣/回补。** 现在只在入队时记一个 `quotaBefore` 快照，`POST /api/v1/tasks`
（真正扣配额的调用）发生在 ChatGPT 桌面端会话内部，监控台看不到结果，所以失败时无法把次数加回去。

**4. 日志功能是错的。** `queue_log.py` 是**单个任务的 ChatGPT rollout 渲染器**，不是调度日志。
全局调度日志目前只是 `.state/monitor.log`（32 MB，从不轮转）和 `.state/auto.json` 里 120 条的环形缓冲。

**5. 一堆废功能和死代码。**
- 页面级：[static/index.html](static/index.html) 的 `renderAutomation()` / `automationApi()`（1315-1371 行）整套是死的——它们操作的 `#automationPanel`、`#queueList`、`#capacityInput` 等 id 在 DOM 里**根本不存在**，每次 tick 都空跑一次 `querySelector` miss。`loadLog()` + `state.logCache` + `/api/log` 也是死的（没有任何元素带 `data-log-task`）。约 90 行 CSS 修饰从不被创建的类。`/api/config`、`/api/automation` GET 两端都没人调。
- 功能级："可选任务"（[static/tasks.html:203](static/tasks.html:203)）、多 roots + activeRoots 监控目录开关、`mergeProjectPool` 项目池开关、Codex App 会话扫描、`queue_log_tailer.py`（无任何引用）——这些要么没人用，要么该被更好的设计取代。
- 传输级：没有 gzip，两个 woff2 字体每次刷新都重新下载（`Cache-Control: no-store` 全覆盖）；`static/tasks.html` 没有 `visibilitychange` 处理，后台标签页照样 3 s + 2 s 轮询；两个搜索框都没有 debounce。

**6. 需求缺口。** 没有 ChatGPT/Codex app 的文件夹（project）列表读取和选择；任务监看不是卡片+弹窗日志；
没有任务状态筛选；没有项目管理地址/账号密码的设置页；没有"任务优先 vs 容器优先"模式切换；
没有定时纠错循环。

### 目标

一个单端口、推送驱动、卡片化、可筛选、带全局调度日志和定时纠错的调度台，支持两种调度模式，
配额按任务生命周期预扣/结算/回补，并能在 ChatGPT app 的前 10 个文件夹中选择全局监听位置。

---

## 关键发现（实现时必须利用的既有资产）

| 发现 | 位置 | 用途 |
|---|---|---|
| ChatGPT app 的文件夹列表就是 Codex 的 project 表 | `~/.codex/state_5.sqlite` 的 `projects` + `project_roots` 表，按 `position` 排序，当前 111 条 | "前 10 个文件夹"直接 `SELECT ... ORDER BY position LIMIT 10`，每个 folder 带 `roots[].path`。`idx_threads_project_id` 索引已存在 |
| `threads.project_id` 已建索引但当前全为 NULL | 同上 | 深链接带上 `projectId` 后，新会话就能归属到选中文件夹，并且能用索引快速反查某文件夹下的活跃会话 |
| 深链接格式 | `codex://threads/new?path=&mode=work&prompt=`（[queue_worker.py:68](queue_worker.py:68)） | **已验证支持 `projectId` 参数**，见下方说明 |
| Solo Manager 凭据三元组已可用 | [monitor_core.py:2327](monitor_core.py:2327)-2340：`username` + Keychain `solo-manager-password` | 设置页直接改这三项即可，认证/续期逻辑整体保留 |
| 跨进程项目占用锁已可用 | `~/.codex/skills/sologsb-0917/scripts/project_claims.py` | 队列领取项目时继续用它，避免和 solo2-auto 抢同一个项目 |
| 容器槽位跨进程锁已可用 | `~/.codex/skills/sologsb-0917/scripts/side_runner.py:390` 的 `_ContainerLimiter`（flock + `reservations/*.json` 标记文件） | 容器优先模式的槽位账本直接复用它，不要另造一套 |
| 真正的配额扣减调用 | `~/.codex/skills/solo-annotation-loop/scripts/platform_bridge.py:1517` 的 `POST /api/v1/tasks` | 需要在其前后埋点，才能实现预扣/回补 |
| `platform_bridge.list_projects()` 已正确分页 | `platform_bridge.py:1127`（page=1..50, size=100, 读到 totalPages） | 替掉 [monitor_core.py:2514](monitor_core.py:2514) 的固定 `size=200` 截断 |
| 单实例锁 | [server.py:62](server.py:62) `MonitorInstanceLock` | 保留，防止两个调度器互相打架 |

---

## 设计

### 总体架构

保留 Python 单进程单端口，但内部重切：

```
server.py            HTTP + SSE（薄封装，只做路由/鉴权/序列化）
api/                 拆成 5 个小模块，替代 5188 行的 monitor_core.py
  tasks.py           任务发现 + 快照（带 mtime 增量缓存）
  scheduler.py       队列状态机 + 两种模式的容量账本 + 定时纠错
  platform.py        Solo Manager 适配（认证/列表/领取/配额）
  folders.py         ChatGPT app 文件夹读取（state_5.sqlite）
  logs.py            全局调度日志（结构化落盘 + 订阅推送）
static/              前端（组件化 vanilla JS，无构建链）
state/               调度日志、队列、设置（JSONL / JSON）
```

**推送替代轮询。** 服务端持有一个 `SnapshotHub`：后台线程每 1.5 s 生成一次增量快照（只含变化的 task），
通过 SSE 推给所有连接的客户端；客户端只在 SSE 断线时回退到轮询。日志用同一条 SSE 通道按 `logId` 增量下发。
这一项改动单独就能消掉绝大部分卡顿——前端不再每 3 秒重建 DOM，服务端不再每 3 秒全量扫盘。

**快照瘦身。** 列表接口只返回卡片需要的字段（`id / name / status / phase / updatedAt / projectCode /
needsAttention / activeContainers`），把 `candidates / sides / workflow / promptText / localHead` 全部移到
按需的 `GET /api/tasks/:id`。1.1 MB 的量级会掉到 10 KB 以下。

**文件读取策略。**
- `state.json` / `attempt.json`：按 `(path, mtime, size)` memoize，同一 tick 内绝不重复读（同一个 152 KB 的
  `state.json` 现在会被重复读 3 次以上）。
- `stdout.jsonl` / 日志：从文件尾部 `seek` 倒读 N 行，不再 `read_text()` 全量（现在一个 220 行请求要解码 2.5–8 MB）。
- 进程探活：一次 `ps -axo pid=,command=` 的结果在一个 tick 内共享，不要每 side 起一个子进程。
- `thread_history_1.sqlite`：改成 `WHERE` + `ORDER BY` + `LIMIT`，且只在任务状态可能变化时查（8492 行全表现在每次快照都扫）。
- `TraceCache`：加 LRU 上限（400 条）。
- `docker ps`：保留 2 s TTL，但错误不再进缓存（现在一次瞬时失败会让队列停摆 2 s 并拒绝启动任何任务）。

**前端渲染。** 组件化 vanilla JS。列表用 keyed diff：按 task id 复用 DOM 节点，只 patch 变化的文本节点，
不做整块 `innerHTML` 重建。卡片列表加 `content-visibility: auto` 和 `contain: layout paint`。
两个搜索框加 150 ms debounce。加 `visibilitychange`：标签页隐藏时暂停 SSE 消费。
`logCache` / `historyCache` 改 LRU（约 20 条）并去掉双重深拷贝。
所有 `fetch` 带 `AbortController` + 显式超时（现在 index.html 完全没有超时，一个挂起的 socket 能冻结整个页面）。

**传输。** 开 gzip；HTML/JS/CSS 加 `ETag`，字体改 `Cache-Control: max-age=315360, immutable`
（现在连 woff2 都是 `no-store`，每次刷新重下 65 KB）。

---

## 功能实现

### 1. 全局（顶栏）

**文件夹选择。** 新增 `api/folders.py`：只读打开 `~/.codex/state_5.sqlite`（`mode=ro`），
`SELECT p.id, p.name, p.position FROM projects p ORDER BY p.position LIMIT 10`，
再按 `project_id` 关联 `project_roots` 拿到 `path`。缓存 30 s（文件夹列表变化不频繁）。
顶栏放一个下拉，选中后写入设置（`.state/settings.json` 的 `defaultFolderId` / `defaultFolderPath`）。

深链接构造从 `codex://threads/new?path=…&mode=work&prompt=…` 扩展为
`…&projectId=<folderId>`。因为 `threads.project_id` 已有索引，之后反查"这个文件夹下有哪些活跃会话"就是一次索引扫描。

**正在执行的任务容器数。** 与 `docker ps` 结果按名字前缀 `sologsb-` 分组统计，但**排除非本任务的常规容器**
（当前 `_group_is_excluded` 已经用 `excludedProjectCodes` 做粗粒度排除，改为按"是否属于某个队列任务/某个候选组"判定）。
顶栏显示 `任务容器 N`。

**正在执行的任务数 / 上限。** 顶栏显示 `运行中 X / 上限 Y`，`X` 来自调度器账本，`Y` 来自当前模式的上限。

**当前模式。** 顶栏一个显式徽标：`容器优先` 或 `任务数量优先`，点击直接切换（设置页也可改）。

### 2. 任务监看页面

卡片网格，每张卡片：任务名、项目编号、状态徽标、当前阶段、活跃容器数、最近活动时间、是否需要人工介入。
点击卡片开弹窗，弹窗内分三个标签：

- **概览**：A/B 两侧状态、候选映射、产物完成度、工作流 8 步。
- **运行日志**：关联容器的日志。这里要**从尾部增量读取**，并且只在弹窗打开时订阅 SSE 增量；关闭即取消订阅。
  同时保留 `/api/log` 作为断线兜底。
- **提示词**：题目全文（现在被塞进每次快照的 `promptText` 是主要浪费源之一）。

状态筛选：`全部 / 等待中 / 运行中 / 需处理 / 已完成 / 失败`，筛选在客户端做（数据已经在本地），
但卡片列表要用虚拟滚动或分页，避免 67 个任务全量挂 DOM。

### 3. 任务队列管理

**项目管理接入。** 新增设置页，字段：`managerBaseUrl`（默认 `http://192.168.31.26:8080`）、
`username`（默认 `admin`）、`password`。保存时写 `.state/settings.json`（权限 0600），
密码**不落盘明文**——沿用现有 `keychain_write` 写 Keychain，设置页只显示"已保存/未保存"。
复用 [monitor_core.py:2327](monitor_core.py:2327) 起的整条认证链
（`/auth/me` → 401 时 `/auth/login` → `/tokens` 换发长期令牌）。

**配额预扣 / 结算 / 回补。** 队列项新增 `quota` 状态机：

```
pending      → claimed(领取时预扣除，写入 quota.deductedAt + quota.remainingBefore)
claimed      → settled(终态且成功：正式扣除，记 remainingAfter)
claimed      → refunded(失败/中止：把剩余次数加回，记 refundReason)
```

预扣除走 `POST /api/v1/tasks`（现有 `platform_bridge.py:1517`）。
**关键**：这个调用目前发生在 ChatGPT 会话内部，监控台看不见。需要在 `platform_bridge.select_project`
前后埋点，把 `taskId` / `taskNo` / 配额变化写回 `result.json`，监控台的 worker 轮询 `result.json` 时就能读到。
回补需要平台侧支持"释放任务次数"——**这一条要确认**，如果 Solo Manager 没有释放接口，退化为：
只在本地账本记录回补意图并显示，实际次数由平台的重试策略兜。

**队列两种模式。**

*任务数量优先*（现有语义）：`capacity` 个任务并行，不强制等容器。启动时给任务一个容器预占位，
预占位在 `startupTimeoutSeconds`（默认 300 s，配置范围 300–600 s）后未见到容器创建就终止该任务并重新入队。

*容器优先*（新）：目标是"容器里跑的任务容器数始终等于设定值"。
- 可设：`maxTasks`（最大任务数）、`maxContainers`（最大容器数）、`candidatesPerTask`（单任务备选容器数）。
  这三个值写进触发提示词，让执行器知道该拉几个候选。
- **预占位 5–10 分钟**：任务被领取的瞬间就占一个容器槽位（写 `reservations/<taskId>.json`，
  内容和 `side_runner._ContainerLimiter` 的标记文件格式保持一致，这样跨进程看得见）。
  若 `containerReserveSeconds`（默认 420 s，可配 300–600）内 `docker ps` 里没出现属于该任务的容器，
  终止任务、释放槽位、重新入队。
- 槽位账本：**直接复用** `side_runner.py:390` 的 `_ContainerLimiter`（flock + 标记文件 + 死 PID 回收），
  不另造一套。监控台通过读同一把锁和同一批标记文件来显示"已占用 / 可分配"。
- 低于 `containerRefillBelow` 时触发补位；预计超额时等待（修掉现在 `>=` 导致提前停摆的问题：
  改成"当前容器数 + 本批候选数 > 硬上限"才等待）。

**定时纠错。** 新增一个独立的 reconcile 循环（默认 60 s，可配），每轮核查并修复：

| 检查项 | 纠错动作 |
|---|---|
| 容器预占位超时（占了槽但没容器） | 终止 worker、释放槽位、任务重新入队 |
| 容器存在但队列项已终态 | 释放对应槽位标记，清理僵尸容器 |
| 槽位标记文件指向的 PID 已死 | 删除标记（`_ContainerLimiter` 已有此逻辑，纠错循环负责兜底扫描） |
| 队列项 `attempts` 异常膨胀（实测有 15473 / 686） | 对不可重试的终态停止累加 attempts，并告警 |
| `orphaned` + `capacityHeld` 卡死（实测 2 项） | 超过宽限期后自动释放名额并标记 `skipped`，不再永久占用 |
| 队列项与 `result.json` 状态不一致 | 以 `result.json` 为准重新同步 |
| 配额 claimed 超过 N 小时无结算 | 强制结算为 refunded 并记录 |

纠错的每个动作都写全局调度日志。

**全局调度日志。** 新增 `api/logs.py`：结构化 JSONL 落盘到 `.state/scheduler.jsonl`
（字段：`ts / level / event / taskId / projectCode / detail`），带 7 天/50 MB 轮转。
SSE 按 `afterSeq` 增量推送。原来的 per-task rollout 日志（`queue_log.py`）降级为任务详情弹窗里的
"运行日志"标签，用尾部增量读取。**删除** `queue_log_tailer.py`（无任何引用）。

**删掉的废功能和死代码：**
- `static/index.html` 的 `renderAutomation()` / `automationApi()`（1315-1371 行，约 57 行 JS）——操作的 DOM id 全不存在；
  连带约 55 行 CSS 和 `state.automationOpen`。
- `static/index.html` 的 `loadLog()` + `state.logCache`（约 40 行）——没有元素带 `data-log-task`，永远不触发。
- `static/index.html` 约 90 行修饰不存在元素的 CSS（`.switch`、`.workflow-*`、`.prompt-block`、`.history-section`、
  `.attempt-*`、`.auto-log-list` 等）。
- `/api/config`、`/api/automation` GET、`/api/log`——两端都没人调（`/api/log` 保留给新弹窗的运行日志用）。
- `static/tasks.html` 的"可选任务"栏（203-207）、"监控目录"栏的 roots + activeRoots 多选（192-202）——
  改为单一的"工作根目录" + 上面的 app 文件夹选择。
- `mergeProjectPool` 项目池开关——改为设置页里的"包含项目池"勾选。
- `static/tasks.html:591` 的空 `change` 监听、`static/tasks.html:139-140` 从不生效的 `.log-panel[hidden]` 规则。
- `codex_sessions.py` 整套（262 行）——由 `threads` 表的索引查询替代。
- `queue_log_tailer.py`（60 行）——无任何引用。

### 4. 技能侧加固

需要同步更新的点（技能目录 `~/.codex/skills/sologsb-0917/`）：

- `SKILL.md` 第 24 行把"单 Key 默认最多 4 个候选容器"写成硬编码；改为引用配置，
  并明确"容器优先 / 任务优先"两种模式下执行器各自的行为。
- `scripts/side_runner.py` 的 `_ContainerLimiter` 加一个可查询接口（或让监控台直接读标记文件），
  使预占位对两端可见。
- `~/.codex/skills/solo-annotation-loop/scripts/platform_bridge.py` 的 `select_project`
  在 `POST /tasks` 前后埋点，把配额变化写进 selection-result，供监控台做预扣/结算/回补对账。
- 触发提示词模板（`config.json` 的 `automation.promptTemplate`）里补上
  `{{max_tasks}} {{max_containers}} {{candidates_per_task}} {{schedule_mode}}` 占位符，由调度器按当前模式渲染。
- `references/key-concurrency.md` 补充容器优先模式的说明。

---

## 实施顺序

1. **`api/folders.py` + 设置页 + 顶栏**（文件夹列表、容器数、任务数/上限、模式徽标）——先让新 UI 立起来。
2. **`SnapshotHub` + SSE + 快照瘦身**（`/api/snapshot` 拆分、文件读取策略修正）——性能地基，之后所有功能都受益。
3. **任务监看页**（卡片网格、弹窗三标签、状态筛选、虚拟滚动）。
4. **队列管理页**（Solo Manager 设置、配额状态机、两种模式、三个上限参数）。
5. **`api/scheduler.py` 的两种模式账本 + 容器预占位 + 超时终止**。
6. **`api/logs.py` 全局调度日志 + SSE 推送 + 轮转**。
7. **定时纠错循环**。
8. **删废功能 + 技能侧加固 + 文档**。

每一步结束都要能启动服务并手动验证，不要攒到最后。

---

## 验证

**启动**：`./run.sh` → `http://127.0.0.1:8790`。确认单实例锁仍然生效（第二个实例启动应退出码 2 并提示 owner pid）。

**性能（每项都有可测目标）**：
- `curl -s -o /dev/null -w "%{size_download}" http://127.0.0.1:8790/api/tasks` 应 < 50 KB
  （对比现在 `/api/snapshot` 的 1.1 MB）。
- 浏览器 Network 面板：两个页面同时开着，请求数应从 ~1.7 req/s 降到接近 0（SSE 长连接）。
- DOM 节点数不随 tick 增长：Performance 面板录 30 s，对比首尾快照的节点数。
- `ps` 子进程调用：`fs_usage -f fork` 或临时打点，确认一个 tick 内 `ps` 只跑 1 次（现在每 side 1 次，67 任务 = 上百次）。
- `/api/log?lines=220` 的耗时：现在要 268 ms（解码整个 2.5 MB 轨迹），改尾部倒读后应 < 20 ms。
- 首屏加载：现在 84 KB + 50 KB HTML 无 gzip、字体 65 KB 无缓存；开 gzip + 缓存后 HTML 应 < 20 KB 传输。

**功能**：
- 顶栏文件夹下拉列出 10 个文件夹，名称与 Codex app 侧栏一致；切换后新建任务的深链接带 `projectId`，
  在 app 里确认会话落在选中文件夹下，并用 `SELECT project_id FROM threads WHERE id=<新线程>` 确认有值。
- 容器优先模式：`maxContainers=2`、`candidatesPerTask=2`，启动 3 个任务，
  确认同一时刻 `docker ps` 里 `sologsb-` 容器数不超过 2，且第三个任务的预占位标记文件存在于
  `~/.codex/sologsb-0917/container-slots/reservations/`。
- 预占位超时：把 `containerReserveSeconds` 调到 30 s 并阻止容器创建，确认任务被终止、槽位释放、
  重新入队，且调度日志有对应记录。
- 配额：领取一个任务，日志出现"预扣除 + remainingBefore"；让任务失败，确认出现"回补剩余次数"。
- 日志：`/logs` 视图能看到调度事件实时滚动；打开任务弹窗的运行日志，确认只读尾部且关闭弹窗后 SSE 取消订阅。
- 状态筛选：`全部 / 等待中 / 运行中 / 需处理 / 已完成 / 失败` 六个筛选各自结果正确。

**回归**：`python3 -m pytest tests/ -q`。`tests/test_queue_worker.py` 和 `tests/test_server.py` 的锁测试应原样通过；
`tests/test_monitor_core.py`（2007 行）和 `tests/test_queue_log.py` 需要跟着重构改写。

---

## 风险与未决

1. **配额回补依赖平台接口。** 如果 Solo Manager 没有"释放任务次数"的端点，回补只能在监控台本地记账。
   需要确认；不确认就先做本地记账 + UI 提示，不阻塞其他部分。
2. **深链接 `projectId` 参数已经确认存在。** 从 `/Applications/ChatGPT.app/Contents/Resources/app.asar`
   里反编译出的路由表明确写着：`case 'threads': if (Q(t)[0]==='new') return
   ['browserUrl','mode','originUrl','path','prompt'].some(...) ? WD(t) : {kind:'newThread'}`，
   而 `WD(e)` 里就是 `let o=$(e,'projectId') ... return {codexAppMode:s, kind:'newThread',
   originUrl:i, path:a, projectId:o==null?void 0:n.Bo(o), ...t}`。
   也就是说 `projectId` 是**白名单外但确实被解析并透传**的参数（不在触发 `WD` 的那五个参数里，
   但 `WD` 自己会读它）。

   **仍需实测的一点**：`n.Bo()` 是把 UUID 规范化成 app 内部 ID 的转换函数（代码里所有
   `n.Bo((0,g.randomUUID)())` 用法都产出 UUIDv7 形状的 ID，`projects` 表 111 条 id 全是 UUIDv7 形状）。
   所以深链接传入的应该是 `state_5.sqlite` 里 `projects.id` 的**原值**，不需要我们自己转换。
   实施第一步用一个临时项目实测一次即可确认：
   `open "codex://threads/new?path=/tmp&mode=work&prompt=test&projectId=<id>"`，
   然后 `SELECT project_id FROM threads` 看新行是否有值。若为空，说明要传别的 ID 形态，
   退化为只用 `path`（文件夹选择这时只影响工作目录，UI 上标注为"仅设置工作目录"）。
3. **删 `codex_sessions.py` 的影响面。** 它同时被 `queue_log.discover_rollout` 和快照的
   `appSessions` 用。前者随 rollout 日志一起降级，后者由 `threads` 索引查询替代——需要确认
   "会话是否还活着" 的判断不能只靠 DB（DB 里可能残留已退出的会话），保留 rollout 文件 mtime 作为辅助判据。
4. **重构期间服务不可用。** 当前有 2 个 `orphaned` 任务永久占着名额、13 个 pending 卡在容器阈值。
   建议重构先在分支上做，切换时先 `queue-release` 手工清掉卡死项，避免新旧调度器对同一批任务双重重启。
