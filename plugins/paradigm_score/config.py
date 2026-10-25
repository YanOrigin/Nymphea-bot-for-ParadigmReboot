"""paradigm_score 配置项。

.env.prod 中以 PARADIGM_ / PRP_ 前缀书写（driver 会将额外键小写化，alias 全用小写）：
    PARADIGM_TOOL_DATA=D:\\Developed\\paradigm-reboot-data\\data   # 登录工具数据目录（可选覆盖）
    PRP_TABLES_DIR=...      # chart 映射静态表目录（可选；默认用插件内置/仓库表）
    TRACKER_CORE_DIR=...    # 私有查分核心库 core 目录（可选；默认自动探测）
"""
from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """范式：起源查分插件配置"""

    # 登录工具数据目录覆盖（默认自动探测 D:\Developed\paradigm-reboot-data\data）
    paradigm_tool_data: str = Field(default="", validation_alias="paradigm_tool_data")
    # 网络请求超时（秒）
    paradigm_timeout: int = Field(default=30, validation_alias="paradigm_timeout")
    # 游戏凭据存储文件
    paradigm_token_file: str = Field(default="data/paradigm_tokens.json", validation_alias="paradigm_token_file")
    # 成绩缓存目录
    paradigm_scores_dir: str = Field(default="data/paradigm_scores", validation_alias="paradigm_scores_dir")
    # PRP 账号信息存储文件（username + upload_token，独立于游戏凭据）
    prp_accounts_file: str = Field(default="data/prp_accounts.json", validation_alias="prp_accounts_file")
    # chart 映射缓存文件
    prp_chart_cache: str = Field(default="data/prp_chart_map_cache.json", validation_alias="prp_chart_cache")
    # PRP 静态映射表目录（prp_chart_id_map.json / slug_wiki_manual_map.json 所在；空 = 不用静态表）
    prp_tables_dir: str = Field(default="", validation_alias="prp_tables_dir")
    # 私有查分核心库 core 目录（可选；默认自动探测 D:\Developed\ParadigmReboot-Tracker\core）
    tracker_core_dir: str = Field(default="", validation_alias="tracker_core_dir")
    # 游戏成绩拉取冷却（秒）；0 = 不冷却。PRP 查询不受此限制
    fetch_cooldown: int = Field(default=4 * 3600, validation_alias="fetch_cooldown")
    # 快爆网页授权中转（Cloudflare Worker）地址
    auth_relay_base: str = Field(default="", validation_alias="auth_relay_base")
    # 中转服务的 AUTH_SECRET
    auth_relay_secret: str = Field(default="", validation_alias="auth_relay_secret")


config = get_plugin_config(Config)
