"""The hosted client's TLS fallback: the pinned root is used only for the default host, and only after the system store refuses.

The live handshake and the fingerprint are checked by hand (see the PR). These tests check the decision logic with a
fake _open_exchange, so they need no network.
"""

from __future__ import annotations

import hashlib
import ssl
import urllib.request

import pytest

from meridian_desktop import hosted_client as h

DEFAULT_URL = "https://usemeridian.us/projects"
CUSTOM_URL = "https://hosted.example.com/projects"
PINNED_SHA1 = "CABD2A79A1076A31F21D253635CB039D4329A5E8"  # ISRG Root X1, the Let's Encrypt root


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url)


def _fake_exchange(monkeypatch, *, refuse_system: bool):
    """Record each call's context. Refuse the system context if asked, and answer 200 for the pinned context."""
    calls: list[ssl.SSLContext | None] = []

    def fake(request, timeout, deadline, context):
        calls.append(context)
        if context is None and refuse_system:
            raise h._CertificateRefused()
        return h.Reply(200, b"[]")

    monkeypatch.setattr(h, "_open_exchange", fake)
    return calls


def test_default_host_retries_with_the_pinned_root_after_the_system_store_refuses(monkeypatch):
    calls = _fake_exchange(monkeypatch, refuse_system=True)
    reply = h._urllib_transport(_request(DEFAULT_URL), 5)
    assert reply.status == 200
    assert calls[0] is None
    assert isinstance(calls[1], ssl.SSLContext)
    assert len(calls) == 2


def test_default_host_uses_the_system_store_when_it_accepts(monkeypatch):
    calls = _fake_exchange(monkeypatch, refuse_system=False)
    h._urllib_transport(_request(DEFAULT_URL), 5)
    assert calls == [None]


def test_custom_host_never_gets_the_pinned_root(monkeypatch):
    calls = _fake_exchange(monkeypatch, refuse_system=True)
    with pytest.raises(h._CertificateRefused):
        h._urllib_transport(_request(CUSTOM_URL), 5)
    assert calls == [None]


def test_refused_certificate_maps_to_unreachable_not_a_crash():
    def transport(request, timeout):
        raise h._CertificateRefused()

    with pytest.raises(h.HostedUnreachable):
        h._send(transport, _request(CUSTOM_URL), 5)


def test_pinned_context_verifies_and_trusts_only_isrg_root_x1():
    context = h._pinned_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    loaded = context.get_ca_certs()
    assert len(loaded) == 1
    subject = dict(item[0] for item in loaded[0]["subject"])
    assert subject["commonName"] == "ISRG Root X1"


def test_pinned_pem_is_the_isrg_root_x1_certificate():
    der = ssl.PEM_cert_to_DER_cert(h.PINNED_ROOT_PEM)
    assert hashlib.sha1(der).hexdigest().upper() == PINNED_SHA1


def test_default_host_constant_matches_the_default_hosted_url():
    assert h.DEFAULT_HOSTED_HOST == "usemeridian.us"
