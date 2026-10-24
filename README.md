# Nymphea-bot

> 查分功能的**引擎**（游戏协议 / PRP 对接 / B50 渲染）在私有库 ParadigmReboot-Tracker（含游戏内置密钥，按原仓库要求不入公开库）。
> 本仓库只含 bot 指令壳：把 Tracker 放到 `D:\Developed\ParadigmReboot-Tracker` 或设 `TRACKER_CORE_DIR` 即可启用查分；未安装时其他功能不受影响，查分指令会提示引擎缺失。


基于 NoneBot2 + OneBot V11 适配器的 QQ 机器人骨架，配合 **NapCat** 使用。
设计目标：为《范式：起源》(Paradigm: Reboot) 查分功能预留插件的空壳框架。

## 项目结构

```
Nymphea-bot/
├── .venv/            # 项目虚拟环境（已创建）
├── plugins/
│   ├── ping.py       # 链路测试插件：/ping -> pong
│   ├── paradigm_score/  # 范式：起源查分插件（骨架占位版）
│   │   ├── __init__.py  # /范式登录、/范式绑定、/更新成绩、/b50、/范式解绑 指令
│   │   └── config.py    # 配置项（超时、token 文件路径）
│   ├── daily_fortune/   # 今日运势插件
│   │   ├── __init__.py  # /今日运势 指令（六档+分项+脱凶祝福+今日曲目）
│   │   └── config.py    # 配置项（数据文件、曲绘源、档位权重）
│   ├── song_query/      # 查歌插件
│   │   ├── __init__.py  # /查歌 指令（精确/别名/模糊匹配）
│   │   └── config.py    # 配置项（曲库/别名文件、候选数上限）
│   └── alias_manager/   # 别名收集插件
│       ├── __init__.py  # /别名添加、/别名查询、/别名取消（wiki_id 锚点会话式交互）
│       └── config.py    # 配置项（别名库路径、每日限额、会话超时）
│   └── alias_sync/      # 别名库 GitHub 自动同步插件（纯后台定时，无 QQ 指令）
│       ├── __init__.py  # 定时任务：投票白名单 → GitHub Contents API
│       ├── config.py    # 配置项（仓库/Token/路径/分支/间隔，对接 .env 的 ALIAS_SYNC_*）
│       └── github_client.py  # 轻量 REST 客户端（无需 git 二进制）
│   └── song_refresh/    # 曲库每日自动更新插件
│       ├── __init__.py  # 每日定时拉 PRP API → 聚合 → 原子重写 paradigm_songs.json
│       └── config.py    # 配置项（刷新时刻、API 地址、启动刷新开关）
│   └── bot_help/        # /help 使用指南插件
│       └── __init__.py  # 文字速览 + 合并转发详细卡片（转发失败自动降级纯文本）
├── bot_daemon.py        # bot 守护进程（崩溃自拉起；bot 输出重定向到 logs/bot.log）
├── napcat_daemon.py     # NapCat 守护进程（快速登录 + 自拉起）
├── setup_autostart.ps1  # 注册开机自启计划任务（已执行过；RunLevel=Limited 普通权限）
├── data/
│   ├── paradigm_songs.json  # 范式：起源曲库（434曲，来自 PRP API）
│   ├── paradigm_aliases.json # 别名库（wiki_id → [别名...]，开源库同格式）
│   ├── fortune_data.json    # 运势抽签记录
│   └── covers/              # 曲绘缓存
├── .env.prod         # 运行配置（驱动器 / 端口 / OneBot 连接）
├── bot.py            # 显式入口（等价 nb run 动态入口）
├── pyproject.toml    # 依赖与 NoneBot 配置
└── README.md
```

> 本项目为 nb-cli 新格式（`[tool.nonebot]` in pyproject.toml），**没有 bot.py**，
> `nb run` 会自动生成入口，无需手动维护。

## 启动

```powershell
cd D:\Developed\Nymphea-bot
.venv\Scripts\python bot.py     # 或安装 nb-cli 后用 nb run
```

插件：`ping`（本地，`/ping` → `pong`，链路自检用）
+ `paradigm_score`（本地，`/范式登录`/`/范式绑定` 绑定凭据，`/更新成绩` 拉取成绩，`/b50` 查询 Rating/B50）
+ `daily_fortune`（本地，`/今日运势` 六档运势 + 三分项 + 每日一签 + 脱凶祝福 + 今日曲目）
+ `song_query`（本地，`/查歌 <名称>` 曲名/别名/模糊查询 + 曲绘 + 四难度定数）
+ `alias_manager`（本地，`/别名添加 <锚点> [别名]` 收集别名 + `/别名查询` + 冲突检测 + 每日限额）。

## 群聊使用规范（to_me）

- **群聊内必须 @bot**（或回复 bot 的消息）指令才会生效，其他情况一律静默——防止刷屏与误触发
- **私聊不受影响**：直接发指令即可（适配器自动视为 to_me）
- **会话续聊免 @**：`/别名添加` 等多轮交互中，后续回复序号/别名**不需要**再 @（temp matcher 不继承 to_me 规则）
- 指令前缀 `/` 或无前缀均可；@ 后有无空格均可
- **群聊回复自动 @ 指令发送者**（`at_reply` 全局补件）：多人同群时一眼分清回复对象；
  私聊不受影响，合并转发类消息（如 /help 卡片）不加 @
- 可选扩展：`.env` 配 `NICKNAME=["小兰花", ...]` 后，群内以昵称开头喊 bot 也能唤醒（未配置则关闭）
- 实现方式：全部指令 matcher 加 `rule=to_me()`；内置 `echo` 插件已移除（无 to_me 保护）

## 对接 NapCat

1. 安装并登录 NapCat（QQ 客户端协议实现，见 https://napcat.napneko.icu/ ）
2. 打开 NapCat WebUI → 网络配置 → 新建 **反向 WebSocket (Reverse WebSocket)**：
   - URL: `ws://127.0.0.1:7999/onebot/v11/ws`
   - Token: 与 `.env.prod` 中 `ONEBOT_ACCESS_TOKEN` 一致（目前留空可不填）
3. 启动顺序：先启动 bot（监听 7999），再启用 NapCat 的反向 WS（会自动重连）
4. 验证：QQ 里给机器人发 `/ping`，应回复 `pong`

## 后续规划（查分器）

- [x] 阶段一 —— 游戏成绩提取（真实协议，已实机验证）：`/范式登录`（自动读取本机登录工具凭据）`/范式绑定` `/更新成绩` `/b50` `/范式解绑`
  - 协议移植自 paradigm-reboot-data 工具集（AES-128-CBC 通道，密钥为游戏编译期常量，`prd_api.py`）
  - `/范式登录` 直接读取 `D:\Developed\paradigm-reboot-data\data\` 的快爆凭据（跑一次 `tools/fetch_scores_hykb.py` 授权后即可）
  - 凭据支持 base64(creds JSON) 或 `session|uid` 简写；含快爆 credential 时 Session 失效自动静默续期（credential-only 也支持）
  - 会话预热：先调 `/server/user/login` 规避新会话首调重端点的偶发 500/400
  - **设备指纹防风控**：每账号一个稳定 device_id（首次生成永久绑定），严禁多号共享/频繁变更
  - 只读不写（绝不调用游戏写端点）；凭据仅存本地 data/，回复/日志只显示首尾掩码
- [x] 阶段二上半场 —— PRP 传分：`/绑定prp <用户名> <上传token>` + `/传分`
  - 游戏 chart_id → PRP chart_id 三层解析（PRP /songs 直映射 1326 条动态缓存 → 静态表 276 条 → slug 修正表 24 条）
  - chaotic 谱自动跳过（不计 rating）；未收录谱面明确列出
  - is_replace 覆盖模式：幂等可重传；PRP 核心逻辑在零依赖的 `prp_api.py`（便于将来拆独立项目）
- [x] 阶段二下半场 —— `/b50` 报告图（三种模式 + 拟合定数开关）
  - `/b50`（标准 B15+B35）· `/b50 overflow`（每池多取 5 条再截断）· `/b50 global`（全池按 rating 前 50 不分池）
  - `/fb50` 等前缀 f = 拟合定数模式（PRP 用 fitting_level 重算 rating 选曲，无拟合定数的谱面标 `*` 回退官方定数）
  - 渲染器 `b50_render.py`：Pillow 纯绘制（无浏览器依赖），版式对齐 PRP 官方导出报告
  - 模式语义逆向自 PRP 前端源码（scope + underflow 参数组合）
- [x] 阶段三 —— 自动传分 + 防风控冷却 + 项目拆分
  - `/更新成绩` 成功后**自动传分**（幂等覆盖，绑定 PRP 后全自动）；`/传分` 保留为手动兜底（不受冷却限制）
  - **游戏成绩拉取 6 小时冷却**（每账号，防频繁登录触发风控）；冷却内提示下次可拉取时间；`/传分`（PRP 侧）与 `/b50`（只读）不受限
  - **查分引擎已拆分**至私有库 YanOrigin/ParadigmReboot-Tracker（游戏协议+密钥不入公开库）；插件通过 `core_loader` 动态加载（私有库优先 → 内置副本兜底），未安装时查分指令优雅降级

## 数据维护（maintenance.py）

每周一 04:30 由计划任务 `NympheaMaintenance` 自动执行（`RunLevel=Limited`，无 UAC）：

- **日志轮转**：`logs/*.log` 压缩归档到 `logs/archive/<name>_<YYYY>W<WW>.log.gz`，保留最近 8 份。
  同周重复执行会**追加**到同一归档（gzip 多 member 串接，可正常解压全部内容）。
  轮转需独占日志文件，因此流程为：停 bot → 归档 → 清空 → 启 bot（实测总停机约 7 秒）
- **运势归档**：`data/fortune_data.json` 中早于 **3 个月**的签文移入 `data/archive/fortune_before_<YYYY-MM-DD>.json.gz`，
  按截止日期分文件、同名归档自动合并。保留近期数据以确保「脱凶检测」（依赖昨日记录）正常

手动执行：

```powershell
.venv\Scripts\python.exe maintenance.py             # 全部维护
.venv\Scripts\python.exe maintenance.py --logs      # 只轮转日志
.venv\Scripts\python.exe maintenance.py --fortune   # 只归档运势
.venv\Scripts\python.exe maintenance.py --dry-run   # 预览
```

## 文档

- NoneBot: https://nonebot.dev/
- OneBot V11 适配器: https://onebot.adapters.nonebot.dev/
- NapCat: https://napcat.napneko.icu/

## 别名系统

- 数据文件 `data/paradigm_aliases.json`，格式 `{"<wiki_id>": ["别名", ...]}`（与将来开源库一致）
- 锚点：`wiki_id`（稳定、好输入），`/查歌` 结果里会显示每首歌的 Wiki ID
- 三种添加方式：
  1. 一步式：`/别名添加 <wiki_id|曲名|已有别名> <新别名>`（唯一命中直接入库）
  2. 引导式：`/别名添加 <曲名片段>` → 回复序号选歌 → 发送别名
  3. 带别名引导：`/别名添加 <曲名片段> <新别名>` → 选序号后自动入库
- 防护：同歌重复提示 / 每人每日 20 条 / 会话 2 分钟超时
- 跨歌同名允许（玩梗自由）：/查歌 遇多义别名列出候选，用 Wiki ID 精确直达
- 投票上库：新别名本地即时生效，集满 3 人投票（/别名投票 <别名>）才进入开源库白名单；种子/历史别名视为已通过
- **投票过期重置**：待审核投票 6 小时（`alias_vote_ttl`，0=不过期）未集满自动重置；
  别名本体不受影响（查歌仍可用），重开后 `/别名投票 <别名>` 会自动重建投票（投票者算首票、重新计时）
- 部署提示：维护数据文件请勿用 PowerShell `Set-Content -Encoding utf8` 写入（带 BOM 会导致读取失败），用 Python `json.dump` 或无 BOM 编辑器

## 别名库 GitHub 自动同步（alias_sync）

- 开源仓库：https://github.com/YanOrigin/ParadigmReboot-alias （`aliases.json`，main 分支）
- 数据流：`data/alias_votes.json` 的 approved 白名单 → 导出开源库格式 → GitHub Contents API 提交
- **纯后台自动同步，无任何 QQ 指令**（防止群内恶意刷指令）：别名集满 3 票进入白名单后，
  bot 每 5 分钟（`ALIAS_SYNC_INTERVAL`，0 = 关闭）自动比对并推送，内容无变化不产生空 commit
- 安全机制：
  - 导出内容确定性（键序排序、无时间戳）→ 无变化零 commit
  - 远端若含本地没有的条目（他人改动）→ 拒绝覆盖，仅写日志告警
  - Token 为细粒度 PAT，仅授权此仓库的 Contents 读写，存于 `.env.prod`（不进 git）
- 配置（`.env.prod`）：

  ```ini
  ALIAS_SYNC_GITHUB_REPO=YanOrigin/ParadigmReboot-alias
  ALIAS_SYNC_GITHUB_TOKEN=github_pat_xxx
  ALIAS_SYNC_GITHUB_PATH=aliases.json
  ALIAS_SYNC_GITHUB_BRANCH=main
  ALIAS_SYNC_INTERVAL=300   # 自动同步间隔秒；0 = 关闭
  ```

- 同步状态可查 `data/alias_sync_state.json`（上次推送指纹 / commit / 时间）
- 已知坑：NoneBot driver config 对 .env 额外键做小写化（case_sensitive=False），
  插件 config 用 `validation_alias` 接收时必须写**小写**别名（如 `alias_sync_github_repo`）

## 曲库每日自动更新（song_refresh）

- 游戏版本更新出新歌/改定数后，bot 每日 **04:00** 自动从 PRP 聚合 API
  （`api.prp.icel.site/api/v2/songs`）拉取最新数据，重写 `data/paradigm_songs.json`，
  查歌 / 今日运势 / 别名系统**无需重启**即用上新数据
- 安全机制：
  - 聚合结果与本地逐条比对，**内容无变化不写文件**
  - 原子写入（临时文件 + replace），杜绝写一半被读坏
  - API 失败/返回空 → 保留旧曲库，仅记日志，次日再试
  - 无任何有效难度的脏数据行自动丢弃
- 曲绘不做预下载：查歌/运势本来就是按需下载缓存，新歌首次被抽到时自然落盘
- 配置（`.env.prod`）：

  ```ini
  SONG_REFRESH_TIME=04:00     # 每日刷新时刻（HH:MM）；留空 = 关闭
  SONG_REFRESH_ON_START=false # 启动时立即刷新一次（调试/首次引导用）
  ```

- 验证小技巧：临时把 TIME 置空 + ON_START=true 重启，可在日志看到
  `启动刷新: 434 首 → 434 首`（无变化）或带新增/变动明细的更新记录

## /help 使用指南（bot_help）

- `@bot /help`（群）/ `/help`（私聊）：先回一条**文字速览**（全部指令一览），
  再发一条**合并转发卡片**（逐指令详细用法，点开才展开，不刷屏）
- 转发走 NapCat `send_group/private_forward_msg`；API 失败时自动降级为纯文本详细说明（双层兜底）
- 改帮助内容只需编辑 `plugins/bot_help/__init__.py` 里的 `BRIEF` 与 `CARD_NODES`
- 同样受 to_me 约束：群内无 @ 不响应
