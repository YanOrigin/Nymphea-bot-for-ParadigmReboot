"""查分核心加载器

优先级：
1. TRACKER_CORE_DIR 环境变量 / config.tracker_core_dir 指定的私有库（ParadigmReboot-Tracker）
2. 标准探测路径 D:\\Developed\\ParadigmReboot-Tracker\\core
3. bot 内置副本 plugins/paradigm_score/（prd_api / prp_api / b50_render 与私有库同源）

统一出口：get_core() 返回命名空间对象（prd_api / prp_api / b50_render 属性）。
私有库不可用时回退内置副本并记录日志；两者都不可用返回 None（指令层降级提示）。
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from nonebot.log import logger

from .config import config

_FALLBACK_DIR = Path(__file__).parent
_core_cache: SimpleNamespace | None = None
_core_source: str = ""


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _candidate_dirs() -> list[Path]:
    dirs = []
    env = config.tracker_core_dir or __import__("os").getenv("TRACKER_CORE_DIR", "")
    if env:
        dirs.append(Path(env))
    here = Path(__file__).parent  # plugins/paradigm_score
    develop = here.parent.parent.parent  # D:\Developed
    dirs.append(develop / "ParadigmReboot-Tracker" / "core")
    dirs.append(here.parent.parent / "ParadigmReboot-Tracker" / "core")
    return dirs


def get_core():
    """返回 (core_namespace, source_desc) 或 (None, 原因)"""
    global _core_cache, _core_source
    if _core_cache is not None:
        return _core_cache, _core_source

    for d in _candidate_dirs():
        d = Path(d)
        if all((d / f).exists() for f in ("prd_api.py", "prp_api.py", "b50_render.py")):
            try:
                core = SimpleNamespace(
                    prd_api=_load_module("tracker_core.prd_api", d / "prd_api.py"),
                    prp_api=_load_module("tracker_core.prp_api", d / "prp_api.py"),
                    b50_render=_load_module("tracker_core.b50_render", d / "b50_render.py"),
                )
                _core_cache, _core_source = core, str(d)
                logger.info(f"paradigm_score 查分核心已加载（私有库）: {d}")
                return _core_cache, _core_source
            except Exception as e:
                logger.warning(f"私有核心 {d} 加载失败，尝试内置副本: {e!r}")

    # 内置副本兜底（与私有库同源的运行副本）
    try:
        core = SimpleNamespace(
            prd_api=_load_module("tracker_core.prd_api", _FALLBACK_DIR / "prd_api.py"),
            prp_api=_load_module("tracker_core.prp_api", _FALLBACK_DIR / "prp_api.py"),
            b50_render=_load_module("tracker_core.b50_render", _FALLBACK_DIR / "b50_render.py"),
        )
        _core_cache, _core_source = core, str(_FALLBACK_DIR)
        logger.info(f"paradigm_score 查分核心已加载（内置副本）: {_FALLBACK_DIR}")
        return _core_cache, _core_source
    except Exception as e:
        logger.error(f"查分核心完全不可用: {e!r}")
        return None, f"{e!r}"
