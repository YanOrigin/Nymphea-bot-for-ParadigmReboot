"""bot_help：/help 指令 — 使用指南（文字速览 + 合并转发详细卡片）

设计要点：
- rule=to_me()：与其他指令一致，群聊需 @bot
- A（文字速览）：一眼看到全部指令；B（合并转发卡片）：逐指令详细用法，点开才展开，不刷屏
- 转发走 NapCat 的 send_group/private_forward_msg；失败时自动降级为纯文本详细说明（兜底）
"""
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.log import logger
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

__plugin_meta__ = PluginMetadata(
    name="bot_help",
    description="/help 使用指南（文字速览 + 合并转发详细卡片）",
    usage="/help（群聊需 @bot）",
    type="application",
    homepage=None,
    config=None,
    supported_adapters={"OneBot V11"},
)

help_cmd = on_command("help", rule=to_me(), priority=5, block=True)

# ---------- A. 文字速览 ----------

BRIEF = """📖 Nymphea 使用指南

📁 基础
/查歌 <曲名|别名|wiki_id> —— 曲目信息与四难度定数
/今日运势 —— 每日一签（六档运势 + 今日曲目）
/help —— 打开本指南

🎵 别名系统（社区共建）
/别名添加 <锚点> [新别名] —— 贡献曲目别名
/别名投票 <别名> —— 为新别名投票（集满 3 票进开源库）
/别名查询 <锚点> —— 查某曲的别名与审核进度

📊 查分（B50）
/范式绑定 —— 快爆账号授权绑定（点链接登录）
/范式绑定ios <手机号> <密码> —— iOS/外服账号绑定
/更新成绩 —— 拉取成绩并自动传分（6h 冷却）
/绑定prp <用户名> <上传token> —— 绑定 PRP 账号
/b50 [模式] —— B50 报告图（standard/overflow/global，前缀 f=拟合定数）
/范式解绑 —— 解除游戏账号绑定

💡 群聊请先 @我；各指令详细用法见下方卡片 👇"""

# ---------- B. 合并转发卡片内容 ----------

CARD_NODES: list[str] = [
    " welcome\n【Nymphea · 使用说明】\n\n群聊中使用请先 @我（或回复我的消息）\n私聊直接发指令即可\n指令前的 / 可省略",
    "【/查歌】\n用法：/查歌 <曲名|别名|wiki_id>\n\n· 支持曲名精确、别名、wiki_id 直达\n· 别名对应多首歌时会列出候选，用 wiki_id 精确查询\n· 返回曲目信息 + 曲绘 + 四难度定数\n\n例：/查歌 encore\n例：/查歌 帝王\n例：/查歌 abyssgazer",
    "【/今日运势】\n用法：/今日运势（可简写 抽签 / 运势）\n\n· 每人每天一签，重复抽会收到今天的签\n· 六档运势：上善 / 中庸 / 安平 / 无恙 / 染秽 / 穷凶\n· 附带桃花运 / 打歌运 / 事业运与今日推荐曲目\n· 前一天抽到凶签、今天脱凶有专属祝福",
    "【/别名添加】\n用法：/别名添加 <锚点> [新别名]\n\n· 锚点可以是曲名 / wiki_id / 已有别名\n· 一步式：/别名添加 encore 夜曲安可\n· 引导式：/别名添加 安可 → 按提示选歌、发别名\n· 同曲重复会提示；每人每天最多 20 条\n· 别名本地即时生效，进开源库需投票",
    "【/别名投票】\n用法：/别名投票 <别名>\n\n· 为待审核的别名投上一票（提交者算首票）\n· 集满 3 人通过后进入开源库白名单\n· 投票进度可用 /别名查询 <锚点> 查看\n\n例：/别名投票 夜曲安可",
    "【/别名查询】\n用法：/别名查询 <曲名|wiki_id|别名>\n\n· 列出某曲目已有别名\n· 待审核别名会显示当前票数（开源审核 n/3）",
    "【/别名取消】\n用法：/别名取消\n\n放弃进行中的别名添加会话（卡住时可用）",
    "【查分 · 第一步 绑定游戏账号】\n\n"
    "· 快爆号（国服安卓）：/范式绑定\n"
    "  → bot 回复一个授权链接，手机浏览器打开，\n"
    "    用手机号+短信验证码登录即可（全程不碰密码）\n"
    "  · iOS/外服账号：/范式绑定ios <手机号> <密码>\n\n"
    "· 绑定后 Session 自动续期，无需重复授权\n"
    "· /范式解绑 可解除绑定",
    "【查分 · 第二步 传分】\n\n"
    "· 注册 prp.icel.site → 个人设置 → 刷新上传令牌\n"
    "· /绑定prp <PRP用户名> <上传token>\n"
    "· /更新成绩 —— 拉取游戏成绩并自动传分到 PRP\n"
    "  （游戏侧 6 小时冷却，防频繁登录风控）\n"
    "  （冷却内会提示下次可拉取时间）\n"
    "· /传分 —— 手动补传（不受冷却限制）\n"
    "· PRP 云端即时可查：prp.icel.site",
    "【查分 · 第三步 B50 报告】\n\n"
    "· /b50 —— 标准 B50（B35 旧曲 + B15 新曲）\n"
    "· /b50 overflow —— 每池多显示 5 条候补（看差一点进榜的）\n"
    "· /b50 global —— 全部成绩按 Rating 取前 50（不分池）\n"
    "· /fb50 / foverflow / fglobal —— 使用\n"
    "  拟合定数重算（无拟合定数的谱面标 * 回退官方）\n"
    "· 报告含总 Rating、各池均分与每谱面 Rating\n"
    "· Rating 由 PRP 服务器计算，与游戏内一致",
    "【· ω ·】\n祝游玩愉快～ 有问题请联系 bot 主人",
]


def _text_node(bot: Bot, text: str) -> dict:
    return {
        "type": "node",
        "data": {
            "user_id": int(bot.self_id),
            "uin": bot.self_id,
            "nickname": "Nymphea",
            "content": [{"type": "text", "data": {"text": text}}],
        },
    }


def _detail_text() -> str:
    """转发失败时的纯文本兜底（与卡片同内容）"""
    return "\n\n".join(n.split("\n", 1)[1].strip() for n in CARD_NODES)


@help_cmd.handle()
async def handle_help(bot: Bot, event: MessageEvent):
    # A. 文字速览
    await help_cmd.send(BRIEF)

    # B. 合并转发详细卡片；失败降级为纯文本
    nodes = [_text_node(bot, n) for n in CARD_NODES]
    try:
        if event.message_type == "group":
            await bot.call_api(
                "send_group_forward_msg",
                group_id=event.group_id,
                messages=nodes,
            )
        else:
            await bot.call_api(
                "send_private_forward_msg",
                user_id=event.user_id,
                messages=nodes,
            )
    except Exception as e:
        logger.warning(f"bot_help 转发卡片发送失败，降级为纯文本: {e!r}")
        await help_cmd.finish(_detail_text())
