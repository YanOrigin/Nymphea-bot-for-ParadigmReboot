"""at_reply：群聊回复自动 @ 指令发送者（全局补丁）

原理：所有插件回复最终都经由 Bot.send(event, message) 发出；
包装它——当 event 是群消息时，在消息开头插入 @发送者 段。
- 私聊不受影响
- 合并转发等直接 call_api 的消息不受影响（无需 @）
- 未来新增插件自动获得此行为，无需逐个改
"""
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import GroupMessageEvent
from nonebot.log import logger

_original_send = Bot.send


async def _at_prefixed_send(self: Bot, event, message, **kwargs):
    try:
        if isinstance(event, GroupMessageEvent):
            at_seg = MessageSegment.at(event.user_id)
            if not isinstance(message, Message):
                message = Message(message)
            message = at_seg + message
    except Exception as e:  # 补丁自身绝不能弄丢回复
        logger.warning(f"at_reply 包装异常，回退原样发送: {e!r}")
    return await _original_send(self, event, message, **kwargs)


Bot.send = _at_prefixed_send

logger.info("at_reply 已启用：群聊回复将自动 @ 指令发送者")
