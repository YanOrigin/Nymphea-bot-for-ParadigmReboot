"""song_query 配置项"""
from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """查歌插件配置"""

    # 曲目数据库（与 daily_fortune 共用）
    song_query_songs_file: str = Field(default="data/paradigm_songs.json")
    # 曲别名库（wiki_id -> [别名...]，与 alias_manager 共用；将来开源库替换此文件）
    song_query_aliases_file: str = Field(default="data/paradigm_aliases.json")
    # 曲绘缓存目录（与 daily_fortune 共用）
    song_query_cover_dir: str = Field(default="data/covers")
    # 曲绘源 URL 前缀
    song_query_cover_url: str = Field(default="https://prp.icel.site/cover/")
    # 模糊匹配最多返回候选数
    song_query_max_candidates: int = Field(default=8)


config = get_plugin_config(Config)
