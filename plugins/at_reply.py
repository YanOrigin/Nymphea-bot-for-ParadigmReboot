"""at_reply：群聊回复自动 @ 指令发送者（全局补丁）

原理：所有插件回复最终都经由 Bot.send(event, message) 发出；
包装它——当 event 是群消息时，在消息开头插入 @发送者 段，随后换行。
- 私聊不受影响
- 合并转发等直接 call_api 的消息不受影响（无需 @）
- 未来新增插件自动获得此行为，无需逐个改

@ 后换行的原因：QQ 里 @ 与正文同行会让首行被挤压，视觉上正文顶格贴在 @ 后面。
统一在 @ 后插入一个软换行，所有指令回复的首行都与 @ 分行显示。
"""
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import GroupMessageEvent
from nonebot.log import logger

_original_send = Bot.send
# 热重载保护：若已被本补丁包装过，不能再次包装（否则会 @ 两次 + 两个换行）
_ALREADY_PATCHED = getattr(_original_send, "_at_reply_patched", False)


def _starts_with_break(message: Message) -> bool:
    """消息本身已以换行开头时不重复插入"""
    if not message:
        return False
    first = message[0]
    return first.type == "text" and first.data.get("text", "").startswith("\n")


async def _at_prefixed_send(self: Bot, event, message, **kwargs):
    try:
        if isinstance(event, GroupMessageEvent):
            at_seg = MessageSegment.at(event.user_id)
            if not isinstance(message, Message):
                message = Message(message)
            if _starts_with_break(message):
                message = at_seg + message
            else:
                message = at_seg + MessageSegment.text("\n") + message
    except Exception as e:  # 补丁自身绝不能弄丢回复
        logger.warning(f"at_reply 包装异常，回退原样发送: {e!r}")
    return await _original_send(self, event, message, **kwargs)


if _ALREADY_PATCHED:
    logger.info("at_reply 已是生效状态，跳过重复包装")
else:
    Bot.send = _at_prefixed_send
    Bot.send._at_reply_patched = True
    logger.info("at_reply 已启用：群聊回复将自动 @ 指令发送者（@ 后换行）")
