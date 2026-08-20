"""``mex clear`` 命令测试：三层确认、备份、清空范围与保留项（02 开发约定 §数据目录）。

数据目录一律走 MEX_HOME → pytest tmp_path，绝不触碰真实 ~/.mex。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mex.cli import app
from mex.cli import clear_cmd  # noqa: F401 - 导入即注册 clear 命令
from mex.store.connection import connect

runner = CliRunner()


@pytest.fixture()
def mex_home(tmp_path, monkeypatch) -> str:
    """MEX_HOME 指向临时目录。"""
    monkeypatch.setenv("MEX_HOME", str(tmp_path))
    return str(tmp_path)


def _invoke(*args: str, **kwargs):
    return runner.invoke(app, list(args), **kwargs)


def _init() -> None:
    assert os.environ.get("MEX_HOME"), "测试必须在 MEX_HOME 隔离下运行"
    assert _invoke("init").exit_code == 0


def _add(**opts) -> dict:
    """执行 add --json 并返回解析后的记忆字典。"""
    args = ["add", "--json"]
    for key, value in opts.items():
        args.extend([f"--{key.replace('_', '-')}", str(value)])
    result = _invoke(*args)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def _query(mex_home: str, table: str) -> int:
    """统计指定表行数。"""
    conn = connect(Path(mex_home) / "mex.db")
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608 - 测试内部受控字面量表名
        return int(row["n"])
    finally:
        conn.close()


def _seed_two() -> None:
    """init 后 add 两条记忆，制造可清空内容。"""
    _init()
    _add(topic="basic_info", sub_topic="name", content="张三")
    _add(topic="basic_info", sub_topic="location", content="北京")


class TestClearDirect:
    """快速路径（--yes）与默认备份行为。"""

    def test_yes_clears_memory_tables(self, mex_home):
        _seed_two()
        assert _query(mex_home, "memories") == 2
        assert _query(mex_home, "history") == 2

        result = _invoke("clear", "--yes", "--no-backup")
        assert result.exit_code == 0, result.stderr

        assert _query(mex_home, "memories") == 0
        assert _query(mex_home, "history") == 0
        assert _query(mex_home, "memories_fts") == 0

    def test_yes_default_backs_up(self, mex_home):
        _seed_two()
        result = _invoke("clear", "--yes")
        assert result.exit_code == 0, result.stderr
        backup_file = Path(mex_home) / "backups" / "mex-export-before-clear.json"
        assert backup_file.exists()
        payload = json.loads(backup_file.read_text(encoding="utf-8"))
        assert len(payload["memories"]) == 2

    def test_no_backup_creates_nothing(self, mex_home):
        _seed_two()
        _invoke("clear", "--yes", "--no-backup")
        assert not (Path(mex_home) / "backups").exists()

    def test_empty_database_skips_flow(self, mex_home):
        """空库时 clear 直接提示无需清空，不进入任何确认、不生成备份。"""
        _init()
        # 无输入流（交互会读到 EOF）：空库应立即短路，退出码 0
        result = _invoke("clear", input="")
        assert result.exit_code == 0, result.stderr
        assert "No data to clear." in result.output
        assert not (Path(mex_home) / "backups").exists()

    def test_empty_database_with_yes(self, mex_home):
        """空库时 --yes 同样直接提示，不执行多余动作。"""
        _init()
        result = _invoke("clear", "--yes")
        assert result.exit_code == 0, result.stderr
        assert "No data to clear." in result.output

    def test_empty_database_with_json(self, mex_home):
        """空库时 --json 输出明确的空结果结构。"""
        _init()
        result = _invoke("clear", "--yes", "--json")
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["cleared_memories"] == 0
        assert data["cleared_history"] == 0


class TestClearConfirmation:
    """三层确认的交互与取消保护。"""

    def test_three_step_confirmation_succeeds(self, mex_home):
        _seed_two()
        # 三步确认依次输入 y（数量 / 备份 / 最终二次确认）
        result = _invoke("clear", input="y\ny\ny\n")
        assert result.exit_code == 0, result.stderr
        assert _query(mex_home, "memories") == 0
        # 修复点：备份在最终确认前执行，并打印了落盘路径
        assert "[backup done] memory snapshot created:" in result.output

    def test_cancel_at_quantity_keeps_data(self, mex_home):
        _seed_two()
        result = _invoke("clear", input="n\n")
        # 第 1 步取消 -> UserError 退出码 1
        assert result.exit_code == 1
        assert _query(mex_home, "memories") == 2

    def test_cancel_at_backup_keeps_data(self, mex_home):
        _seed_two()
        result = _invoke("clear", input="y\nn\n")
        # 备份确认答 n -> 不备份；但第 3 步还没输，直接 Ctrl-D/EOF 取消
        assert result.exit_code == 1
        assert _query(mex_home, "memories") == 2

    def test_final_refusal_keeps_data(self, mex_home):
        _seed_two()
        # 第 1 步 y、备份 y、最终 n -> 取消，数据保留
        result = _invoke("clear", input="y\ny\nn\n")
        assert result.exit_code == 1
        assert _query(mex_home, "memories") == 2


class TestClearPreserve:
    """清空只影响记忆/索引/审计，其余一概保留。"""

    def test_preserves_config_and_schema_files(self, mex_home):
        _seed_two()
        _invoke("clear", "--yes", "--no-backup")
        assert (Path(mex_home) / "config.yaml").exists()
        assert (Path(mex_home) / "schema.yaml").exists()

    def test_preserves_usage_and_extraction_tables(self, mex_home):
        _seed_two()
        conn = connect(Path(mex_home) / "mex.db")
        try:
            conn.execute(
                "INSERT INTO llm_usage (id, purpose, model, prompt_tokens, completion_tokens, created_at) "
                "VALUES ('u1', 'test', 'none', 1, 1, '2024-01-01T00:00:00Z')",
            )
            conn.commit()
        finally:
            conn.close()
        _invoke("clear", "--yes", "--no-backup")
        assert _query(mex_home, "llm_usage") == 1


class TestClearOutput:
    """输出格式。"""

    def test_json_output_shape(self, mex_home):
        _seed_two()
        result = _invoke("clear", "--yes", "--no-backup", "--json")
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["cleared_memories"] == 2
        assert data["cleared_history"] == 2
        assert data["backup"] is None

    def test_backup_path_printed_once_and_restore_hint(self, mex_home):
        _seed_two()
        result = _invoke("clear", "--yes")
        assert result.exit_code == 0, result.stderr
        backup_path = str(Path(mex_home) / "backups" / "mex-export-before-clear.json")
        # 备份路径仅在首次生成时作为「备份完成」提示输出一次
        assert result.output.count(f"[backup done] memory snapshot created: {backup_path}") == 1
        # 清空结果行不再重复路径
        assert ";备份：" not in result.output
        # 额外提示了恢复记忆的命令（含路径）
        assert f"To restore memories, run: mex import {backup_path}" in result.output
