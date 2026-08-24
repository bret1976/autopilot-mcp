from __future__ import annotations

import os
from subprocess import CompletedProcess
from pathlib import Path

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-media-tests")

from app.media import (
    MediaError,
    download_and_cut,
    impersonate_available,
    is_source_block,
    source_block_message,
    _pull_strategies,
)


def test_bot_check_is_source_block_not_publish() -> None:
    assert is_source_block("ERROR: [youtube] abc: Sign in to confirm you’re not a bot.")
    assert is_source_block("HTTP Error 429: Too Many Requests")
    assert is_source_block("Video unavailable")
    assert not is_source_block("ffmpeg: encoder not found")
    msg = source_block_message(
        "https://www.youtube.com/watch?v=tedx",
        "Sign in to confirm you’re not a bot",
    )
    assert "source clip" in msg
    assert "not your YouTube channel" in msg


def test_youtube_strategies_try_chrome_impersonate_first() -> None:
    monkeypatch_available = impersonate_available()
    youtube = _pull_strategies("https://www.youtube.com/watch?v=abc")
    tiktok = _pull_strategies("https://www.tiktok.com/@x/video/1")
    assert youtube
    assert tiktok
    joined = " ".join(youtube[0])
    if monkeypatch_available:
        assert "--impersonate" in joined
        assert "chrome" in joined
        assert "--impersonate" in " ".join(tiktok[0])
    assert any("android,ios" in " ".join(item) for item in youtube)


def test_download_retries_next_yt_dlp_client(monkeypatch, tmp_path: Path) -> None:
    os.environ["DATA_DIR"] = str(tmp_path)
    monkeypatch.setattr("app.media.data_dir", lambda: tmp_path)
    monkeypatch.setattr("app.media._which", lambda name: name)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "yt-dlp":
            if any("impersonate" in part for part in cmd):
                return CompletedProcess(cmd, 1, "", "ERROR: Sign in to confirm you’re not a bot")
            out = cmd[cmd.index("-o") + 1]
            dest = Path(str(out).replace("%(ext)s", "mp4"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"RAW")
            return CompletedProcess(cmd, 0, "", "")
        dest = Path(cmd[-1])
        dest.write_bytes(b"CUT")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("app.media._run", fake_run)
    result = download_and_cut("retry-studio", "https://www.youtube.com/watch?v=abc")
    assert result["vertical"].endswith("9x16.mp4")
    assert len([cmd for cmd in calls if cmd[0] == "yt-dlp"]) >= 1


def test_all_strategies_raise_source_bot_check(monkeypatch, tmp_path: Path) -> None:
    os.environ["DATA_DIR"] = str(tmp_path)
    monkeypatch.setattr("app.media.data_dir", lambda: tmp_path)
    monkeypatch.setattr("app.media._which", lambda name: name)

    def fake_run(cmd: list[str]) -> CompletedProcess[str]:
        if cmd[0] == "yt-dlp":
            return CompletedProcess(cmd, 1, "", "Sign in to confirm you’re not a bot")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("app.media._run", fake_run)
    try:
        download_and_cut("blocked-studio", "https://youtu.be/tedx")
        raise AssertionError("expected MediaError")
    except MediaError as exc:
        assert exc.code == "source_bot_check"
        assert "not your YouTube channel" in str(exc)
