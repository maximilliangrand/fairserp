"""Command line parsing and the startup guidance it prints."""

import pytest

from fairserp import cli


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "serve", calls.append)
    return calls


def test_serve_is_the_default_command_and_takes_its_flags(served):
    cli.main(["--port", "9001"])
    cli.main([])
    assert [(a.command, a.port) for a in served] == [("serve", 9001), ("serve", 8000)]


def test_exposed_listener_without_keys_warns(monkeypatch, capsys):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    for name in ("FAIRSERP_API_KEYS", "FAIRSERP_AUTH_DISABLED", "PORT"):
        monkeypatch.delenv(name, raising=False)
    cli.main(["serve", "--host", "0.0.0.0"])
    assert "every request will get 401" in capsys.readouterr().err


def test_bad_config_exits_naming_the_variable(monkeypatch, capsys, served):
    from fairserp.config import get_settings

    monkeypatch.setenv("FAIRSERP_SERPAPI_ENGINES", "google")
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2 and not served
    assert "FAIRSERP_SERPAPI_ENGINES" in capsys.readouterr().err
