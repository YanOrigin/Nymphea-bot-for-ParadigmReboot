"""alias_sync 配置项

.env.prod 中以 ALIAS_SYNC_ 前缀书写（NoneBot driver 会将额外键小写化，
validation_alias 因此全部用小写）：
    ALIAS_SYNC_GITHUB_REPO=YanOrigin/ParadigmReboot-alias
    ALIAS_SYNC_GITHUB_TOKEN=github_pat_xxx
    ALIAS_SYNC_GITHUB_PATH=aliases.json
    ALIAS_SYNC_GITHUB_BRANCH=main
    ALIAS_SYNC_INTERVAL=300        # 自动同步间隔秒数；0 = 关闭
"""
from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """别名库 GitHub 自动同步配置（无 QQ 指令，纯后台任务）"""

    # 本地投票元数据文件（approved 白名单是同步唯一来源）
    alias_votes_file: str = Field(default="data/alias_votes.json")
    # GitHub 仓库（owner/repo）
    github_repo: str = Field(default="", validation_alias="alias_sync_github_repo")
    # GitHub 细粒度 PAT（Contents: Read and write）
    github_token: str = Field(default="", validation_alias="alias_sync_github_token")
    # 仓库内文件路径
    github_path: str = Field(default="aliases.json", validation_alias="alias_sync_github_path")
    # 分支
    github_branch: str = Field(default="main", validation_alias="alias_sync_github_branch")
    # API 超时（秒）
    github_timeout: int = Field(default=30, validation_alias="alias_sync_timeout")
    # 自动同步间隔（秒）；0 = 关闭自动同步。建议 ≥300，GitHub API 限额足够
    auto_sync_interval: int = Field(default=300, validation_alias="alias_sync_interval")


config = get_plugin_config(Config)
