"""alias_manager 配置项"""
from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """别名管理插件配置"""

    # 别名库文件（wiki_id -> [别名...]）
    alias_db_file: str = Field(default="data/paradigm_aliases.json")
    # 投票元数据文件（开源库同步审核用，不进开源库）
    alias_votes_file: str = Field(default="data/alias_votes.json")
    # 曲目库（复用查歌的曲库，用于解析 wiki_id / 曲名）
    alias_songs_file: str = Field(default="data/paradigm_songs.json")
    # 每用户每日可添加别名数（防滥用）
    alias_daily_limit: int = Field(default=20)
    # 别名进入开源库所需投票人数（本地即时生效不受此限制）
    alias_vote_threshold: int = Field(default=3)
    # 引导式列表最多展示候选数
    alias_max_candidates: int = Field(default=8)
    # 引导会话有效期（秒）
    alias_session_timeout: int = Field(default=120)
    # 待审核投票有效期（秒），超时未集满则重置投票（别名本地仍有效）；0 = 不过期
    alias_vote_ttl: int = Field(default=6 * 3600)


config = get_plugin_config(Config)
