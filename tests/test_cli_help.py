"""Grouped command help stays complete and usable in narrow terminals."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sovite import cli


ROOT = Path(__file__).resolve().parents[1]


def root_help(monkeypatch, capsys, columns="80"):
    monkeypatch.setenv("COLUMNS", columns)
    monkeypatch.setattr(sys, "argv", ["sovite", "--help"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    return capsys.readouterr().out


def test_root_help_shows_each_registered_command_once(monkeypatch, capsys):
    output = root_help(monkeypatch, capsys)
    parser = cli._build_parser()
    registered = cli._subparser_action(parser).choices
    for name in registered:
        assert output.count(f"    {name} ") == 1, name


def test_root_help_lines_fit_80_columns(monkeypatch, capsys):
    output = root_help(monkeypatch, capsys)
    assert max(map(len, output.splitlines())) <= 80


def test_unlisted_registered_command_appears_under_other(monkeypatch, capsys):
    parser = cli._build_parser()
    subparsers = cli._subparser_action(parser)
    subparsers.add_parser("unlisted-test-command", help="Visible fallback command")
    output = parser.format_help()
    assert "Other:" in output
    assert output.count("unlisted-test-command") == 1


def test_bare_invocation_prints_grouped_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sovite"])
    cli.main()
    output = capsys.readouterr().out
    assert "Start here:" in output
    assert "Examples:" in output


def test_misspelled_command_suggests_registered_name(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sovite", "workr"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    captured = capsys.readouterr()
    assert result.value.code == 2
    assert "did you mean worker?" in captured.err


@pytest.mark.parametrize("arguments", [["--help"], []])
def test_python_module_help_paths(arguments):
    environment = dict(os.environ, COLUMNS="80", PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, "-m", "sovite", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Start here:" in result.stdout
    assert "Examples:" in result.stdout
