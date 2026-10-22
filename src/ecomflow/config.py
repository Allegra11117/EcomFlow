"""配置加载：把 config.yaml 变成可点路径访问的对象，并统一解析项目内路径。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


class Config:
    """只读配置包装器，支持 cfg.get("validate.thresholds.gmv_wow_pct", 35)。"""

    def __init__(self, data: dict[str, Any], root: Path = ROOT) -> None:
        self._data = data
        self.root = root

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    def path(self, dotted: str, default: str | None = None) -> Path:
        """解析配置中的相对路径为绝对路径（不自动创建目录）。"""
        raw = self.get(dotted, default)
        if not raw:
            raise KeyError(f"配置缺少路径项: {dotted}")
        p = Path(str(raw))
        return p if p.is_absolute() else (self.root / p)

    def env(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)


@lru_cache(maxsize=1)
def get_config(config_path: str | None = None) -> Config:
    path = Path(config_path) if config_path else ROOT / "config" / "config.yaml"
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config(data)


def ensure_dirs(cfg: Config | None = None) -> None:
    """确保运行所需目录存在。"""
    cfg = cfg or get_config()
    for key in ("paths.raw", "paths.archive", "paths.output", "paths.logs"):
        cfg.path(key).mkdir(parents=True, exist_ok=True)
    cfg.path("paths.db", "db/ecom.db").parent.mkdir(parents=True, exist_ok=True)
    (cfg.path("paths.output") / "alerts").mkdir(parents=True, exist_ok=True)
