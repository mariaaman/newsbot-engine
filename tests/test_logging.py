from newsbot.logging_setup import redact


def test_redact_masks_secrets():
    assert "abc123secret" not in redact("token=abc123secret", ["abc123secret"])
    out = redact("key gsk_abcdefgh12345")
    assert "gsk_" in out and "abcdefgh12345" not in out


def test_redact_masks_bearer_tokens():
    assert "abcdefghijk123" not in redact("Authorization: Bearer abcdefghijk123", [])
