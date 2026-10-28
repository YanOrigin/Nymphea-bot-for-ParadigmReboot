"""song_random 配置项"""
from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """打什么（随机曲目）插件配置"""

    # 曲目数据库（与 song_query / daily_fortune 共用）
    song_random_songs_file: str = Field(
        default="data/paradigm_songs.json", validation_alias="song_random_songs_file"
    )
    # 曲绘缓存目录（与其他插件共用）
    song_random_cover_dir: str = Field(
        default="data/covers", validation_alias="song_random_cover_dir"
    )
    # 曲绘源 URL 前缀
    song_random_cover_url: str = Field(
        default="https://prp.icel.site/cover/", validation_alias="song_random_cover_url"
    )
    # 只抽有 massive 难度的曲目（默认开启：避免抽到纯新手曲）
    song_random_require_massive: bool = Field(
        default=False, validation_alias="song_random_require_massive"
    )
    # 同一用户连续两次抽到同一首的规避：记住最近 N 首（0 = 不规避）
    song_random_avoid_recent: int = Field(
        default=10, validation_alias="song_random_avoid_recent"
    )


config = get_plugin_config(Config)
