from nonebot import on_command
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

__plugin_meta__ = PluginMetadata(
    name="ping",
    description="链路测试插件：/ping -> pong",
    usage="/ping（群聊需 @bot）",
    type="application",
    homepage=None,
    config=None,
    supported_adapters=None,
)

ping = on_command("ping", rule=to_me(), priority=5, block=True)


@ping.handle()
async def handle_ping():
    await ping.finish("pong")
