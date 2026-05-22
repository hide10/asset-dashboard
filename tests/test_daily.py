import subprocess

import pytest

from src import daily


def test_deploy_cloudflare_pages_raises_when_wrangler_missing(monkeypatch, tmp_path):
    def fake_run(cmd, check):
        raise FileNotFoundError("wrangler")

    monkeypatch.setattr(daily.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="wrangler"):
        daily._deploy_cloudflare_pages(tmp_path / "dist")


def test_deploy_cloudflare_pages_propagates_command_failure(monkeypatch, tmp_path):
    def fake_run(cmd, check):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(daily.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        daily._deploy_cloudflare_pages(tmp_path / "dist")


def test_deploy_cloudflare_pages_passes_project_name(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, check):
        calls.append((cmd, check))

    monkeypatch.setattr(daily.subprocess, "run", fake_run)

    daily._deploy_cloudflare_pages(tmp_path / "dist", "asset-dashboard")

    assert calls == [
        (
            ["wrangler", "pages", "deploy", str(tmp_path / "dist"), "--project-name", "asset-dashboard"],
            True,
        )
    ]
