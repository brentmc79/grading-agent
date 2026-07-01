from app.agent import redact_data, redact_sensitive_info


def test_redact_email() -> None:
    text = "Contact me at test.user+mailbox@example.com for details."
    expected = "Contact me at [REDACTED_EMAIL] for details."
    assert redact_sensitive_info(text) == expected


def test_redact_google_api_key() -> None:
    text = "My key is AIzaSyD-1234567890abcdefghijklmnopqrstuvw"
    expected = "My key is [REDACTED_API_KEY]"
    assert redact_sensitive_info(text) == expected


def test_redact_github_pat() -> None:
    text = "Token: ghp_1234567890abcdefghijklmnopqrstuvwxyz"
    expected = "Token: [REDACTED_GITHUB_TOKEN]"
    assert redact_sensitive_info(text) == expected


def test_redact_data_dict() -> None:
    data = {
        "email": "user@example.com",
        "nested": {"key": "AIzaSyD-1234567890abcdefghijklmnopqrstuvw", "safe": "hello"},
        "list": ["ghp_1234567890abcdefghijklmnopqrstuvwxyz", "safe"],
    }
    expected = {
        "email": "[REDACTED_EMAIL]",
        "nested": {"key": "[REDACTED_API_KEY]", "safe": "hello"},
        "list": ["[REDACTED_GITHUB_TOKEN]", "safe"],
    }
    assert redact_data(data) == expected
