from __future__ import annotations

import pytest

from security.secret_policy import (
    SecretPolicyViolation,
    assert_secret_free_bytes,
    redact_secret_text,
)


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        (
            'export AWS_SECRET_ACCESS_KEY="aws-secret-value"',
            "aws-secret-value",
        ),
        ('DB_PASS="database-password-value"', "database-password-value"),
        ("SECRET_KEY_BASE=rails-secret-value", "rails-secret-value"),
        (
            'GOOGLE_APPLICATION_CREDENTIALS="/home/user/service-account.json"',
            "/home/user/service-account.json",
        ),
        ('{"dbPass":"camel-password-value"}', "camel-password-value"),
        ("<dbPass>xml-password-value</dbPass>", "xml-password-value"),
        ("oauth-token: yaml-token-value", "yaml-token-value"),
    ],
)
def test_normalized_credential_keys_are_rejected_and_redacted(
    value: str,
    secret: str,
) -> None:
    with pytest.raises(SecretPolicyViolation, match="credential-like content"):
        assert_secret_free_bytes(value.encode(), label="test input")

    redacted = redact_secret_text(value)

    assert secret not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.parametrize(
    "value",
    [
        "mode=development",
        "compass=north",
        "tokenizer=wordpiece",
        "privateMode=false",
        "<server>localhost</server>",
    ],
)
def test_noncredential_configuration_is_preserved(value: str) -> None:
    assert_secret_free_bytes(value.encode(), label="test input")

    assert redact_secret_text(value) == value


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        (
            "<password><![CDATA[cdata-synthetic-value]]></password>",
            "cdata-synthetic-value",
        ),
        ('"db pass" = "toml-synthetic-value"', "toml-synthetic-value"),
        (
            r'{"\u0070assword": "json-synthetic-value"}',
            "json-synthetic-value",
        ),
        ('<server password="xml-attribute-value"/>', "xml-attribute-value"),
    ],
)
def test_structured_key_encodings_are_rejected_and_redacted(
    value: str,
    secret: str,
) -> None:
    with pytest.raises(SecretPolicyViolation, match="credential-like content"):
        assert_secret_free_bytes(value.encode(), label="structured input")

    redacted = redact_secret_text(value)

    assert secret not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.parametrize(
    "value",
    [
        '{"mode":',
        '"mode" = [',
        "<server>",
        '<!DOCTYPE server [<!ENTITY local "synthetic">]><server>&local;</server>',
    ],
)
def test_malformed_or_unsafe_structured_documents_fail_closed(value: str) -> None:
    with pytest.raises(SecretPolicyViolation):
        assert_secret_free_bytes(value.encode(), label="structured input")


@pytest.mark.parametrize(
    "value",
    [
        '{"mode":"development"}',
        '"build mode" = "development"',
        '[server]\nhost = "localhost"',
        "<server><![CDATA[localhost]]></server>",
        '<server host="localhost"/>',
    ],
)
def test_safe_structured_documents_are_preserved(value: str) -> None:
    assert_secret_free_bytes(value.encode(), label="structured input")

    assert redact_secret_text(value) == value
