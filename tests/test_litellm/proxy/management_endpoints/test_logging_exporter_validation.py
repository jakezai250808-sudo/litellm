"""Validation for admin-owned logging-exporter assignment on key/team/org."""

import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath("../../../.."))

import litellm
from litellm.models.credentials import CredentialItem
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.management_endpoints.logging_exporter_validation import (
    validate_logging_exporter_assignment,
)


@pytest.fixture
def _registry():
    original = litellm.credential_list
    litellm.credential_list = [
        CredentialItem(
            credential_name="langfuse-eu",
            credential_values={},
            credential_info={
                "credential_type": "logging",
                "description": "langfuse_otel",
            },
        ),
        CredentialItem(
            credential_name="openai-key",
            credential_values={},
            credential_info={"custom_llm_provider": "openai"},  # a provider credential
        ),
    ]
    try:
        yield
    finally:
        litellm.credential_list = original


def _admin():
    return UserAPIKeyAuth(api_key="k", user_role=LitellmUserRoles.PROXY_ADMIN)


def _member():
    return UserAPIKeyAuth(api_key="k", user_role=LitellmUserRoles.INTERNAL_USER)


def test_admin_with_known_logging_credential_is_allowed(_registry):
    validate_logging_exporter_assignment(
        {"logging_exporters": ["langfuse-eu"]}, _admin()
    )


def test_noop_when_assignment_absent(_registry):
    # an update that does not touch logging_exporters is never gated/validated
    validate_logging_exporter_assignment({"some_other_key": 1}, _member())
    validate_logging_exporter_assignment(None, _member())


def test_non_admin_is_forbidden(_registry):
    with pytest.raises(HTTPException) as exc:
        validate_logging_exporter_assignment(
            {"logging_exporters": ["langfuse-eu"]}, _member()
        )
    assert exc.value.status_code == 403


def test_unknown_credential_is_rejected(_registry):
    with pytest.raises(HTTPException) as exc:
        validate_logging_exporter_assignment(
            {"logging_exporters": ["does-not-exist"]}, _admin()
        )
    assert exc.value.status_code == 400


def test_provider_credential_is_not_a_valid_logging_exporter(_registry):
    # openai-key exists but is a provider credential, not a logging destination
    with pytest.raises(HTTPException) as exc:
        validate_logging_exporter_assignment(
            {"logging_exporters": ["openai-key"]}, _admin()
        )
    assert exc.value.status_code == 400


def test_non_list_is_rejected(_registry):
    with pytest.raises(HTTPException) as exc:
        validate_logging_exporter_assignment(
            {"logging_exporters": "langfuse-eu"}, _admin()
        )
    assert exc.value.status_code == 400
