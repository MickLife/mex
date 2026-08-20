"""配置读取：MEX_HOME / config.yaml / 环境变量。

优先级：环境变量 > config.yaml > 内置默认值（架构文档 §9）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_MEX_HOME = str(Path.home() / ".mex")
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_SECONDS = 2
DEFAULT_PROFILE_MAX_TOKENS = 3000


@dataclass(frozen=True)
class Config:
    """运行时配置快照（一次命令生命周期内不变）。"""

    mex_home: str
    db_path: str
    schema_path: str
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_api_key_env: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    llm_max_retries: int = DEFAULT_MAX_RETRIES
    llm_retry_base_seconds: int = DEFAULT_RETRY_BASE_SECONDS
    profile_max_tokens: int = DEFAULT_PROFILE_MAX_TOKENS

    def api_key(self) -> str | None:
        """API key 解析：``api_key_env`` 指定的环境变量存在时优先；否则用文件里的 ``api_key``。

        双通道设计（用户决策）：日常用文件存储，CI/脚本场景用环境变量覆盖。
        """
        if self.llm_api_key_env:
            env_value = os.environ.get(self.llm_api_key_env)
            if env_value:
                return env_value
        return self.llm_api_key


def get_mex_home() -> str:
    """数据目录：``MEX_HOME`` 环境变量优先，默认 ``~/.mex``。"""
    return os.environ.get("MEX_HOME") or DEFAULT_MEX_HOME


def load_config(mex_home: str) -> Config:
    """读取 config.yaml（缺失或非法时用内置默认值），叠加环境变量覆盖。

    Args:
        mex_home: 数据目录（通常来自 :func:`get_mex_home`）。

    Returns:
        配置快照。config.yaml 中缺失的字段取默认值；环境变量优先于文件。
    """
    home = Path(mex_home)
    raw = _read_config_yaml(home / "config.yaml")

    llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
    profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}

    return Config(
        mex_home=mex_home,
        db_path=str(home / "mex.db"),
        schema_path=str(home / "schema.yaml"),
        llm_base_url=_opt_str(llm.get("base_url")),
        llm_api_key=_opt_str(llm.get("api_key")),
        llm_api_key_env=_opt_str(llm.get("api_key_env")),
        llm_model=_opt_str(llm.get("model")),
        llm_timeout_seconds=int(llm.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        llm_max_retries=int(llm.get("max_retries", DEFAULT_MAX_RETRIES)),
        llm_retry_base_seconds=int(llm.get("retry_base_seconds", DEFAULT_RETRY_BASE_SECONDS)),
        profile_max_tokens=int(profile.get("max_tokens", DEFAULT_PROFILE_MAX_TOKENS)),
    )


def write_llm_config(
    mex_home: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    model: str | None = None,
) -> None:
    """更新 config.yaml 的 ``llm`` 段（保留其他内容），写入后收紧权限为 600。

    参数为 None 的字段保持不变；空字符串会清空该字段。

    Args:
        mex_home: 数据目录。
        base_url / api_key / api_key_env / model: 要写入的 LLM 配置。

    Raises:
        FileNotFoundError: config.yaml 不存在（需先运行 ``mex init``）。
    """
    path = Path(mex_home) / "config.yaml"
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist, run `mex init` first")
    data = _read_config_yaml(path)
    llm = data.setdefault("llm", {})
    if not isinstance(llm, dict):
        data["llm"] = {}
        llm = data["llm"]
    for key, value in (("base_url", base_url), ("api_key", api_key), ("api_key_env", api_key_env), ("model", model)):
        if value is not None:
            llm[key] = value
    _write_config_yaml(path, data)


def _write_config_yaml(path: Path, data: dict) -> None:
    """写回 config.yaml 并收紧权限（600，仅本人可读）。"""
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


def _read_config_yaml(path: Path) -> dict:
    """读取 config.yaml；文件缺失或 YAML 非法时返回空字典（按默认值运行）。"""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _opt_str(value: object) -> str | None:
    """可选字符串：空值归一为 None。"""
    return str(value) if value else None
