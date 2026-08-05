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


def test_pretty_json_nested_secret_is_redacted_structurally() -> None:
    value = """{
  "service": {
    "apiKey":
      "pretty-json-secret"
  }
}"""

    with pytest.raises(SecretPolicyViolation, match="credential-like content"):
        assert_secret_free_bytes(value.encode(), label="pretty JSON")

    redacted = redact_secret_text(value)
    assert "pretty-json-secret" not in redacted
    assert '"apiKey":"[REDACTED]"' in redacted


def test_prefixed_pretty_json_secret_fails_closed() -> None:
    value = """runner: starting test
{
  "service": {
    "password":
      "prefixed-json-secret"
  }
}
"""

    assert redact_secret_text(value) == "[REDACTED]"


def test_prefixed_multiline_toml_secret_fails_closed() -> None:
    value = """runner: starting test
[service]
token = [
  "toml-prefixed-secret",
  "second-value"
]
"""

    assert redact_secret_text(value) == "[REDACTED]"


def test_prefixed_multiline_xml_attribute_secret_fails_closed() -> None:
    value = """runner: starting test
<configuration>
  <service password="xml-prefixed-
secret" />
</configuration>
"""

    assert redact_secret_text(value) == "[REDACTED]"


def test_toml_multiline_secret_fails_closed_for_output_redaction() -> None:
    value = '''[service]
token = """
toml-multiline-secret
"""
'''

    with pytest.raises(SecretPolicyViolation, match="credential-like content"):
        assert_secret_free_bytes(value.encode(), label="multiline TOML")

    assert redact_secret_text(value) == "[REDACTED]"


def test_nested_xml_multiline_attribute_is_redacted_structurally() -> None:
    value = """<configuration>
  <service
    password="xml-multiline-secret"
  >localhost</service>
</configuration>"""

    with pytest.raises(SecretPolicyViolation, match="credential-like content"):
        assert_secret_free_bytes(value.encode(), label="nested XML")

    redacted = redact_secret_text(value)
    assert "xml-multiline-secret" not in redacted
    assert 'password="[REDACTED]"' in redacted
