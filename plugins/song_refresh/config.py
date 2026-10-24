"""song_refresh 配置项

.env.prod 中以 SONG_REFRESH_ 前缀书写（driver 会将额外键小写化，alias 全用小写）：
    SONG_REFRESH_TIME=04:00          # 每日刷新时刻（本地时间 HH:MM）；空串 = 关闭
    SONG_REFRESH_ON_START=true       # 启动时立即刷新一次（调试/首次引导用）
"""
from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """曲库每日自动更新配置"""

    # 曲库文件（song_query / daily_fortune / alias_manager 共用）
    songs_file: str = Field(default="data/paradigm_songs.json", validation_alias="song_refresh_songs_file")
    # PRP 聚合 API
    api_url: str = Field(default="https://api.prp.icel.site/api/v2/songs", validation_alias="song_refresh_api_url")
    # 每日刷新时刻（本地时间 HH:MM）；留空 = 关闭定时刷新
    refresh_time: str = Field(default="04:00", validation_alias="song_refresh_time")
    # 启动时立即刷新一次（首次引导/测试用）
    refresh_on_start: bool = Field(default=False, validation_alias="song_refresh_on_start")
    # HTTP 超时（秒）
    request_timeout: int = Field(default=30, validation_alias="song_refresh_timeout")


config = get_plugin_config(Config)
