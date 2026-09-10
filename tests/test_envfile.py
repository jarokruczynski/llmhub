from __future__ import annotations

from pathlib import Path

from llmhub.envfile import parse_env_text, source_env_dir


def test_parse_env_text_handles_quotes_comments_export() -> None:
    text = """
# comment
export ALPHA=one
BETA="two words"
GAMMA='three'
BROKEN LINE
DELTA=
"""
    assert parse_env_text(text) == {
        "ALPHA": "one",
        "BETA": "two words",
        "GAMMA": "three",
        "DELTA": "",
    }


def test_source_env_dir_does_not_override_existing(tmp_path: Path) -> None:
    env_dir = tmp_path / "keys"
    env_dir.mkdir()
    (env_dir / "keys.env").write_text("EXISTING=from_file\nNEW=from_file\n", encoding="utf-8")
    environ = {"EXISTING": "from_shell"}

    loaded = source_env_dir(env_dir, environ)

    assert environ["EXISTING"] == "from_shell"
    assert environ["NEW"] == "from_file"
    assert loaded == ["NEW"]


def test_source_env_dir_reads_all_env_files(tmp_path: Path) -> None:
    env_dir = tmp_path / "keys"
    env_dir.mkdir()
    (env_dir / "a.env").write_text("A=1\n", encoding="utf-8")
    (env_dir / "b.env").write_text("B=2\n", encoding="utf-8")
    (env_dir / "ignored.txt").write_text("C=3\n", encoding="utf-8")
    environ: dict[str, str] = {}

    source_env_dir(env_dir, environ)

    assert environ == {"A": "1", "B": "2"}


def test_missing_env_dir_is_not_an_error(tmp_path: Path) -> None:
    environ: dict[str, str] = {}
    assert source_env_dir(tmp_path / "nope", environ) == []
    assert environ == {}
