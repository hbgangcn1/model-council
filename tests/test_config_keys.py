"""Tests for config_loader.api_keys() credential resolution (v15.9).

Priority: MODEL_COUNCIL_CREDENTIALS file > env vars > ~/.model-council/credentials
> ~/.dsh/.credentials.yaml. No real credentials are touched: HOME is redirected
to tmp_path and env is scrubbed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestrator import config_loader


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    for var in list(config_loader._KNOWN_KEY_NAMES) + ["MODEL_COUNCIL_CREDENTIALS"]:
        monkeypatch.delenv(var, raising=False)
    # Redirect Path.home() (Windows: USERPROFILE) to an empty dir.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", "")
    monkeypatch.setenv("HOMEPATH", "")
    return tmp_path


def test_no_source_raises_helpful(clean_env):
    with pytest.raises(FileNotFoundError, match="MODEL_COUNCIL_CREDENTIALS|DEEPSEEK_API_KEY"):
        config_loader.api_keys()


def test_env_only(clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "  dk  ")
    keys = config_loader.api_keys()
    assert keys == {"DEEPSEEK_API_KEY": "dk"}


def test_standard_file(clean_env):
    cred = Path.home() / ".model-council" / "credentials"
    cred.parent.mkdir(parents=True)
    cred.write_text("DEEPSEEK_API_KEY: filekey\nJUNK LINE\n", encoding="utf-8")
    assert config_loader.api_keys() == {"DEEPSEEK_API_KEY": "filekey"}


def test_env_overrides_file(clean_env, monkeypatch):
    cred = Path.home() / ".model-council" / "credentials"
    cred.parent.mkdir(parents=True)
    cred.write_text("DEEPSEEK_API_KEY: filekey\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "envkey")
    assert config_loader.api_keys()["DEEPSEEK_API_KEY"] == "envkey"


def test_explicit_file_wins_and_missing_fails(clean_env, tmp_path, monkeypatch):
    cred = Path.home() / ".model-council" / "credentials"
    cred.parent.mkdir(parents=True)
    cred.write_text("DEEPSEEK_API_KEY: filekey\n", encoding="utf-8")
    other = tmp_path / "other"
    other.write_text("MINIMAX_CN_API_KEY: otherkey\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_COUNCIL_CREDENTIALS", str(other))
    assert config_loader.api_keys() == {"MINIMAX_CN_API_KEY": "otherkey"}
    monkeypatch.setenv("MODEL_COUNCIL_CREDENTIALS", str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError, match="missing file"):
        config_loader.api_keys()


def test_legacy_dsh_path_still_works(clean_env):
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    cred.parent.mkdir(parents=True)
    cred.write_text("MINIMAX_CN_API_KEY: dshkey\n", encoding="utf-8")
    assert config_loader.api_keys() == {"MINIMAX_CN_API_KEY": "dshkey"}
