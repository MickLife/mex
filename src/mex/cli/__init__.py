"""meX 命令行入口（Typer app 组装）。

各子命令模块（write/query/extract/review/backup/integrate）通过
``from mex.cli import app, run`` 注册命令；本文件底部集中 import 各模块
以触发注册（并行开发完成后由主 agent 统一补齐）。
"""

from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar

import typer

from mex import __version__
from mex.cli.common import ExternalError, UserError

app = typer.Typer(help="meX: local-first personal memory system", no_args_is_help=True)

F = TypeVar("F", bound=Callable)


def run[T: Callable](fn: T) -> T:
    """命令包装器：统一捕获 UserError（退出码 1）与 ExternalError（退出码 2）。

    用法：``@app.command()`` 与 ``@run`` 同时装饰命令函数。
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return fn(*args, **kwargs)
        except UserError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except ExternalError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(2) from exc

    return wrapper  # type: ignore[return-value]


@app.callback(invoke_without_command=True)
def _main(
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
) -> None:
    """meX 全局回调：处理 --version；无命令时由 no_args_is_help 显示帮助。"""
    if version:
        typer.echo(f"mex {__version__}")
        raise typer.Exit()


# 子命令注册（import 副作用即注册命令）：
from mex.cli import backup as backup  # noqa: E402, F401
from mex.cli import clear_cmd as clear_cmd  # noqa: E402, F401
from mex.cli import config_cmd as config_cmd  # noqa: E402, F401
from mex.cli import extract as extract  # noqa: E402, F401
from mex.cli import gc as gc  # noqa: E402, F401
from mex.cli import integrate as integrate  # noqa: E402, F401
from mex.cli import query as query  # noqa: E402, F401
from mex.cli import review as review  # noqa: E402, F401
from mex.cli import write as write  # noqa: E402, F401
