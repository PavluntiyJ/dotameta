from __future__ import annotations

import os

import pytest

import dotameta.config as config


@pytest.fixture
def environment(monkeypatch) -> dict[str, str]:
    clean: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", clean)
    return clean


def test_dotenv_only_applies_allowlisted_keys(tmp_path, environment):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENDOTA_API_KEY=allowed\n"
        "HTTPS_PROXY=https://attacker.example\n"
        "REQUESTS_CA_BUNDLE=/tmp/attacker.pem\n"
        "UNRELATED=value\n",
        encoding="utf-8",
    )

    applied = config.load_dotenv(dotenv)

    assert applied == {"OPENDOTA_API_KEY": "allowed"}
    assert environment["OPENDOTA_API_KEY"] == "allowed"
    assert "HTTPS_PROXY" not in environment
    assert "REQUESTS_CA_BUNDLE" not in environment
    assert "UNRELATED" not in environment


def test_real_environment_takes_precedence_over_dotenv(tmp_path, environment):
    environment["OPENDOTA_API_KEY"] = "from-environment"
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENDOTA_API_KEY=from-file\n", encoding="utf-8")

    assert config.load_dotenv(dotenv) == {}
    assert environment["OPENDOTA_API_KEY"] == "from-environment"


def test_dotenv_handles_bom_and_quoted_values(tmp_path, environment):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\ufeffOPENDOTA_API_KEY=\"double quoted\"\nSTRATZ_API_TOKEN='single quoted'\n",
        encoding="utf-8",
    )

    assert config.load_dotenv(dotenv) == {
        "OPENDOTA_API_KEY": "double quoted",
        "STRATZ_API_TOKEN": "single quoted",
    }


def test_settings_reports_malformed_account_id(tmp_path, monkeypatch, environment):
    dotenv = tmp_path / ".env"
    dotenv.write_text("DOTAMETA_ACCOUNT_ID=not-an-id\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", str(dotenv))

    settings = config.Settings.from_env()

    assert settings.account_id is None
    assert settings.account_id_error is not None
    assert "DOTAMETA_ACCOUNT_ID is not usable" in settings.account_id_error
