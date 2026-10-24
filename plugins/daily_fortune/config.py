"""daily_fortune 配置项"""
from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """今日运势插件配置"""

    # 抽签数据文件（用户每日结果持久化）
    fortune_data_file: str = Field(default="data/fortune_data.json")
    # 曲目数据库文件
    fortune_songs_file: str = Field(default="data/paradigm_songs.json")
    # 曲绘缓存目录
    fortune_cover_dir: str = Field(default="data/covers")
    # 曲绘源 URL 前缀
    fortune_cover_url: str = Field(default="https://prp.icel.site/cover/")
    # 各运势档位权重：上善/中庸/安平/无恙/染秽/穷凶（顺序对应 LUCK_TIERS）
    fortune_weights: list[int] = Field(default=[2, 20, 33, 30, 12, 3])


config = get_plugin_config(Config)
