"""M3 CLI：``mex gc`` 命令端到端测试（TTL 过期清理）。

数据目录一律走 MEX_HOME → pytest tmp_path，绝不触碰真实 ~/.mex。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import mex.cli.gc as gc_mod  # noqa: F401 - 导入即注册 gc 命令
from mex.cli import app
from mex.cli import write  # noqa: F401 - 导入即注册 M3 命令
from mex.domain.memory import Confidence, Memory
from mex.store import history, memories
from mex.store.connection import connect

runner = CliRunner()


@pytest.fixture()
def mex_home(tmp_path, monkeypatch) -> str:
    """MEX_HOME 指向临时目录。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    return str(tmp_path)


def _init() -> None:
    assert os.environ.get("MEX_HOME"), "测试必须在 MEX_HOME 隔离下运行"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stderr


def _add(**opts) -> dict:
    args = ["add", "--json"]
    for key, value in opts.items():
        args.extend([f"--{key.replace('_', '-')}", str(value)])
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def _insert_expired(conn, mid: str, content: str, expires_at: str) -> None:
    """直接插入一条已过期记录（立即提交，供后续命令读取）。"""
    now = "2026-01-01T00:00:00Z"
    movies = Memory(
        id=mid, topic="finance", sub_topic=None, content=content,
        is_ai_inferred=False, confidence=Confidence.CONFIRMED, evidence=None,
        created_at=now, updated_at=now, expires_at=expires_at,
    )
    memories.insert(conn, movies)
    conn.commit()


def test_gc_soft_deletes_expired(mex_home):
    """mex gc 软删已过期记录（reason=TTL 过期，可 restore）。"""
    _init()
    _add(topic="finance", content="未过期", expires="2099-01-01")
    conn = connect(Path(mex_home) / "mex.db")
    try:
        _insert_expired(conn, "exp-1", "已过期旧记", "2000-01-01T00:00:00Z")
    finally:
        conn.close()
    result = runner.invoke(app, ["gc"])
    assert result.exit_code == 0
    assert "Soft-deleted 1 expired records" in result.stdout
    conn = connect(Path(mex_home) / "mex.db")
    try:
        row = conn.execute(
            "SELECT forgotten_at, forgotten_reason FROM memories WHERE id='exp-1'",
        ).fetchone()
        assert row["forgotten_at"] is not None
        assert row["forgotten_reason"] == "TTL expired"
        row = conn.execute(
            "SELECT forgotten_at FROM memories WHERE content='未过期'",
        ).fetchone()
        assert row["forgotten_at"] is None
        # 软删可 restore
    finally:
        conn.close()
    assert runner.invoke(app, ["restore", "exp-1"]).exit_code == 0


def test_gc_dry_run_no_effect(mex_home):
    """--dry-run 只预览不执行（forgotten_at 不写）。"""
    _init()
    conn = connect(Path(mex_home) / "mex.db")
    try:
        _insert_expired(conn, "exp-2", "将过期", "2000-01-01T00:00:00Z")
    finally:
        conn.close()
    result = runner.invoke(app, ["gc", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run]" in result.stdout and "exp-2" in result.stdout
    conn = connect(Path(mex_home) / "mex.db")
    try:
        row = conn.execute("SELECT forgotten_at FROM memories WHERE id='exp-2'").fetchone()
        assert row["forgotten_at"] is None
    finally:
        conn.close()


def test_gc_no_expired(mex_home):  # noqa: ARG001 - fixture 副作用设置 MEX_HOME
    """无过期记录时提示，退出码 0。"""
    _init()
    _add(topic="finance", content="未过期", expires="2099-01-01")
    result = runner.invoke(app, ["gc"])
    assert result.exit_code == 0
    assert "No expired records." in result.stdout


def test_gc_write_history(mex_home):
    """gc 软删后写 forget 事件（可审计）。"""
    _init()
    conn = connect(Path(mex_home) / "mex.db")
    try:
        _insert_expired(conn, "exp-3", "旧记", "2000-01-01T00:00:00Z")
    finally:
        conn.close()
    runner.invoke(app, ["gc"])
    conn = connect(Path(mex_home) / "mex.db")
    try:
        events = history.list_for_memory(conn, "exp-3")
        assert events[-1]["event"] == "forget"
        assert events[-1]["actor"] == "user"
    finally:
        conn.close()


def test_expired_restore_still_invisible(mex_home):
    """过期记录 restore 后（仍过期）对查询仍不可见——需 --clear-expires 才恢复可见。"""
    _init()
    conn = connect(Path(mex_home) / "mex.db")
    try:
        _insert_expired(conn, "exp-4", "尴尬", "2000-01-01T00:00:00Z")
    finally:
        conn.close()
    runner.invoke(app, ["gc"])
    runner.invoke(app, ["restore", "exp-4"])
    result = runner.invoke(app, ["search", "--keyword", "尴尬"])
    assert "尴尬" not in result.stdout
    assert runner.invoke(app, ["list", "--include-forgotten"]).exit_code == 0
