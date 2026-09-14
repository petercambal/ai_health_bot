from app.link_token import generate_link_token, verify_link_token


def test_roundtrip():
    token = generate_link_token(8969058651)
    assert verify_link_token(token) == 8969058651


def test_tampered_payload_rejected():
    token = generate_link_token(123)
    sig = token.split(".", 1)[1]
    forged = generate_link_token(999).split(".", 1)[0]
    assert verify_link_token(f"{forged}.{sig}") is None


def test_tampered_signature_rejected():
    token = generate_link_token(123)
    payload, sig = token.split(".", 1)
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert verify_link_token(f"{payload}.{flipped}") is None


def test_malformed_token_rejected():
    assert verify_link_token("not-a-valid-token") is None
    assert verify_link_token("") is None


def test_expired_token_rejected(monkeypatch):
    import time

    token = generate_link_token(123)
    future = time.time() + 3600
    monkeypatch.setattr(time, "time", lambda: future)
    assert verify_link_token(token) is None
