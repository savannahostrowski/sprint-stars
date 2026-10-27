from pathlib import Path
import runpy
from unittest.mock import Mock

import pytest
import uvicorn

DEV = Path(__file__).parents[1] / "dev.py"


@pytest.mark.parametrize("token", [None, "", " \t "])
def test_missing_token_stops_before_starting_server(monkeypatch, token):
    if token is None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    with pytest.raises(SystemExit, match="Missing GITHUB_TOKEN"):
        runpy.run_path(str(DEV), run_name="__main__")
    run.assert_not_called()


def test_token_allows_startup_without_printing_credentials(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    runpy.run_path(str(DEV), run_name="__main__")
    run.assert_called_once_with("main:app", host="127.0.0.1", port=8774)
    output = capsys.readouterr()
    assert "test-token" not in output.out + output.err
