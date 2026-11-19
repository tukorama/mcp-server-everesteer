"""Unit tests for secret scrubbing and untrusted remote text hygiene."""

from mcp_server_everesteer.redaction import clean_label, clean_text, register_secret, scrub


def test_scrubs_api_keys_webhooks_and_registered_secrets() -> None:
    register_secret("super-secret-custom-token")
    text = (
        "X-API-Key: eiq_test_fixture_secret_9999 "
        "webhook=whsec_test_fixture_secret_8888 "
        "Authorization: Bearer bearer_token_xyz "
        "raw=super-secret-custom-token"
    )
    cleaned = scrub(text)
    assert "eiq_test_fixture_secret_9999" not in cleaned
    assert "whsec_test_fixture_secret_8888" not in cleaned
    assert "bearer_token_xyz" not in cleaned
    assert "super-secret-custom-token" not in cleaned
    assert "***" in cleaned


def test_clean_text_and_label_strip_bidi_and_control_chars() -> None:
    nasty = "Model\x00Name\u202eEvil\n\tLabel"
    assert clean_text(nasty) == "ModelNameEvil\n\tLabel"
    assert clean_label(nasty) == "ModelNameEvil Label"
    assert clean_text("abcdefgh", limit=5) == "abcd…"
    assert clean_text(None) is None
