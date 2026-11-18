"""Terminal capability and CLI parser tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pitch_guard import cli
from pitch_guard.service import PitchGuardService
from pitch_guard.terminal import supports_unicode


class DummyStream:
    def __init__(self, encoding: str) -> None:
        self.encoding = encoding


def test_unicode_detection_respects_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PITCH_GUARD_ASCII", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    assert supports_unicode(DummyStream("utf-8")) is True
    assert supports_unicode(DummyStream("ascii")) is False


def test_unicode_detection_respects_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PITCH_GUARD_ASCII", "1")
    assert supports_unicode(DummyStream("utf-8")) is False
    monkeypatch.delenv("PITCH_GUARD_ASCII")
    monkeypatch.setenv("TERM", "dumb")
    assert supports_unicode(DummyStream("utf-8")) is False
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    assert supports_unicode(DummyStream("utf-8")) is False


def test_cli_parser_options() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["--demo", "--mute", "--ascii", "--data-dir", "/tmp/pg"])
    assert args.demo is True
    assert args.mute is True
    assert args.ascii is True
    assert args.data_dir == Path("/tmp/pg")


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "pitch-guard" in capsys.readouterr().out


def test_cli_demo_wires_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeApp:
        def __init__(self, service, *, demo: bool, ascii_only: bool) -> None:
            captured["service"] = service
            captured["demo"] = demo
            captured["ascii"] = ascii_only
            captured["history"] = service.history

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(cli, "PitchGuardApp", FakeApp)
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    code = cli.main(["--demo", "--mute", "--data-dir", str(tmp_path)])
    assert code == 0
    assert captured["demo"] is True
    assert captured["ran"] is True
    service = captured["service"]
    assert isinstance(service, PitchGuardService)
    assert service.history.open_bucket is not None or service.history.snapshot(datetime.now(UTC))
