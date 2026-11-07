"""CLI tests: run the real Typer commands against the fake site."""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novelbot.cli import runner as cli_runner
from novelbot.cli.app import app
from novelbot.core.engine import Engine
from tests.conftest import HOST, FakeSite

cli = CliRunner()


@pytest.fixture
def fake(monkeypatch) -> FakeSite:
    site = FakeSite(chapters=12)
    patched = functools.partial(Engine, transport=site.transport())
    monkeypatch.setattr(cli_runner, "Engine", patched)
    monkeypatch.setattr("novelbot.cli.app.Engine", patched)
    monkeypatch.setenv("NOVELBOT_CONFIG", "")
    return site


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "novelbot.toml"
    path.write_text("[http]\ndelay = 0\njitter = 0\n", "utf-8")
    return path


def test_crawl_then_export_offline(tmp_path: Path, fake: FakeSite) -> None:
    out = tmp_path / "out"
    config = _config(tmp_path)
    result = cli.invoke(app, ["crawl", f"{HOST}/novel/big/", "-f", "epub,txt", "--first", "3",
                              "-o", str(out), "-c", str(config), "-y"])
    assert result.exit_code == 0, result.output
    folder = out / "Big Story"
    assert (folder / "Big Story c1-3.epub").is_file()
    assert (folder / "Big Story c1-3.txt").is_file()

    result = cli.invoke(app, ["export", str(folder), "-f", "json", "-c", str(config)])
    assert result.exit_code == 0, result.output
    assert (folder / "Big Story c1-3.json").is_file()

    result = cli.invoke(app, ["info", f"{HOST}/novel/big/", "-c", str(config)])
    assert result.exit_code == 0 and "12" in result.output


def test_partial_failure_exit_code(tmp_path: Path, fake: FakeSite) -> None:
    fake.fail = {2}
    result = cli.invoke(app, ["crawl", f"{HOST}/novel/big/", "--all", "-o", str(tmp_path),
                              "-c", str(_config(tmp_path))])
    assert result.exit_code == cli_runner.EXIT_PARTIAL, result.output


def test_bad_format_is_rejected(tmp_path: Path, fake: FakeSite) -> None:
    result = cli.invoke(app, ["crawl", f"{HOST}/novel/big/", "-f", "mobi", "--all",
                              "-o", str(tmp_path), "-c", str(_config(tmp_path))])
    assert result.exit_code == 1
    assert "Unknown output format" in result.output
    assert sum(fake.requests.values()) == 0  # rejected before any request


def test_sources_and_config_init(tmp_path: Path) -> None:
    result = cli.invoke(app, ["sources"])
    assert result.exit_code == 0 and "royalroad" in result.output and "[sites]" in result.output
    target = tmp_path / "cfg.toml"
    assert cli.invoke(app, ["config", "init", str(target)]).exit_code == 0
    assert "[download]" in target.read_text("utf-8")
    assert cli.invoke(app, ["config", "init", str(target)]).exit_code == 1  # no overwrite
