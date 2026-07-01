"""Shared fixtures for tests."""
from __future__ import annotations
import json
from pathlib import Path
import pytest


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    return tmp_path / "providers.jsonc"


@pytest.fixture
def sample_config_dict() -> dict:
    return {
        "settings": {"rotateEvery": 3, "disableTtlHours": 1},
        "default": {"agent": "claude", "provider": "zhipu", "key": "main"},
        "providers": [
            {
                "id": "zhipu",
                "type": "symmetric",
                "displayName": "智谱",
                "defaultKey": "main",
                "baseURLs": {
                    "anthropic": "https://zhipu/anthropic",
                    "openai": "https://zhipu/openai",
                },
                "keys": [
                    {"id": "main", "key": "sk-zhipu-main"},
                    {"id": "backup", "key": "sk-zhipu-backup"},
                ],
                "models": [
                    {"id": "glm-5.2", "displayName": "GLM-5.2", "context": 1000000, "output": 131072},
                    {"id": "glm-5-turbo", "displayName": "GLM-5 Turbo", "context": 128000, "output": 16384},
                ],
                "errorHandling": {"1308": "disable,rotate", "_default": "disable,rotate,downgrade"},
            },
            {
                "id": "bailian",
                "type": "asymmetric",
                "displayName": "百炼",
                "defaultKey": "account-a",
                "baseURLs": {"openai": "https://bailian/openai"},
                "keys": [
                    {"id": "account-a", "key": "sk-bailian-a",
                     "models": [{"id": "glm-5.2", "displayName": "GLM-5.2", "context": 1000000, "output": 131072}]},
                    {"id": "account-b", "key": "sk-bailian-b",
                     "models": [{"id": "glm-4.6", "displayName": "GLM-4.6", "context": 128000, "output": 16384}]},
                ],
                "errorHandling": {"_default": "disable,rotate"},
            },
        ],
    }


@pytest.fixture
def sample_config_file(tmp_config_path: Path, sample_config_dict: dict) -> Path:
    tmp_config_path.write_text(json.dumps(sample_config_dict), encoding="utf-8")
    return tmp_config_path
