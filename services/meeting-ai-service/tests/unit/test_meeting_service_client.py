"""HTTP and token classification tests for canonical meeting-service delivery."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import SecretStr

from app.core.config import Settings
from app.services import meeting_service_client as client_module
from app.services.durable_outbox import ClaimedMessage
from app.services.meeting_service_client import (
    DeliveryAttempt,
    DeliveryDisposition,
    MeetingServiceClient,
    ReloadingHttpClient,
)

KEY = base64.b64encode(b"K" * 32).decode()


class FakeCapabilityProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def capability_for(self, message: ClaimedMessage):  # type: ignore[no-untyped-def]
        self.calls += 1
        return SecretStr(f"capability-{self.calls}"), None

    async def aclose(self) -> None:
        return None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        ingestion_enabled=True,
        meeting_service_base_url="https://meeting.test",
        meeting_service_token_url="https://auth.test/token",
        meeting_service_client_id="meeting-ai",
        meeting_service_client_secret=SecretStr("secret"),
        ingestion_store_path=tmp_path / "outbox.sqlite3",
        ingestion_active_key_id="v1",
        ingestion_encryption_keys_json=SecretStr(json.dumps({"v1": KEY})),
        ingestion_timeout_sec=1.0,
        ingestion_lease_sec=3.0,
    )


def _message() -> ClaimedMessage:
    return ClaimedMessage(
        analysis_run_id="22222222-2222-4222-8222-222222222222",
        meeting_id="11111111-1111-4111-8111-111111111111",
        payload={
            "summary": "A",
            "generated_at": "2026-07-18T01:02:03Z",
            "decisions": [],
            "actions": [],
            "supersedes_analysis_run_id": None,
        },
        attempt_count=1,
        created_at=1.0,
    )


def _acknowledgment(
    message: ClaimedMessage | None = None,
    *,
    replay: bool = False,
    **overrides: object,
) -> dict[str, object]:
    claimed = message or _message()
    body: dict[str, object] = {
        "analysis_run_id": claimed.analysis_run_id,
        "meeting_id": claimed.meeting_id,
        "persisted": True,
        "storage_mode": "persisted",
        "idempotent_replay": replay,
        "decision_count": len(claimed.payload.get("decisions", [])),  # type: ignore[arg-type]
        "action_count": len(claimed.payload.get("actions", [])),  # type: ignore[arg-type]
        "supersedes_analysis_run_id": claimed.payload.get("supersedes_analysis_run_id"),
        "generated_at": claimed.payload["generated_at"],
    }
    body.update(overrides)
    return body


def test_token_is_cached_and_idempotency_header_is_stable(tmp_path: Path) -> None:
    async def scenario() -> None:
        token_calls = 0
        ingestion_headers: list[httpx.Headers] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.host == "auth.test":
                token_calls += 1
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            ingestion_headers.append(request.headers)
            return httpx.Response(201, json=_acknowledgment())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(_settings(tmp_path), client)
            assert (await service.deliver(_message())).disposition is DeliveryDisposition.DELIVERED
            assert (await service.deliver(_message())).disposition is DeliveryDisposition.DELIVERED
        assert token_calls == 1
        assert all(h["Idempotency-Key"] == _message().analysis_run_id for h in ingestion_headers)
        assert all(h["Authorization"] == "Bearer token" for h in ingestion_headers)

    asyncio.run(scenario())


def test_success_acknowledgment_is_exact_or_retried_with_stable_run(tmp_path: Path) -> None:
    async def scenario(status: int, body: object) -> DeliveryAttempt:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            return httpx.Response(status, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await MeetingServiceClient(_settings(tmp_path), client).deliver(_message())

    replay_body = _acknowledgment(replay=True)
    replay_body.pop("supersedes_analysis_run_id")
    replay = asyncio.run(scenario(200, replay_body))
    wrong_run = asyncio.run(
        scenario(
            201,
            _acknowledgment(analysis_run_id="99999999-9999-4999-8999-999999999999"),
        )
    )
    wrong_count = asyncio.run(scenario(201, _acknowledgment(action_count=1)))
    mismatched_status = asyncio.run(scenario(201, _acknowledgment(replay=True)))
    malformed = asyncio.run(scenario(201, {"persisted": True}))

    assert replay.disposition is DeliveryDisposition.REPLAYED
    for invalid in (wrong_run, wrong_count, mismatched_status, malformed):
        assert invalid.disposition is DeliveryDisposition.RETRY
        assert invalid.error_code == "ingestion_invalid_acknowledgment"


def test_each_post_uses_fresh_capability_and_strips_internal_tenant_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[httpx.Headers], list[dict[str, object]], int]:
        headers: list[httpx.Headers] = []
        bodies: list[dict[str, object]] = []
        capabilities = FakeCapabilityProvider()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            headers.append(request.headers)
            bodies.append(json.loads(request.content))
            return httpx.Response(201, json=_acknowledgment(message))

        message = _message()
        message.payload["_canonical_tenant_id"] = "33333333-3333-4333-8333-333333333333"
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(
                _settings(tmp_path),
                client,
                capability_provider=capabilities,
            )
            await service.deliver(message)
            await service.deliver(message)
        return headers, bodies, capabilities.calls

    headers, bodies, calls = asyncio.run(scenario())
    assert calls == 2
    assert [item["X-Analysis-Job-Capability"] for item in headers] == [
        "capability-1",
        "capability-2",
    ]
    assert all("_canonical_tenant_id" not in body for body in bodies)


def test_401_invalidates_token_and_is_retryable(tmp_path: Path) -> None:
    async def scenario() -> None:
        token_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.host == "auth.test":
                token_calls += 1
                return httpx.Response(200, json={"access_token": f"token-{token_calls}"})
            return httpx.Response(401)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(_settings(tmp_path), client)
            first = await service.deliver(_message())
            second = await service.deliver(_message())
        assert first.disposition is DeliveryDisposition.RETRY
        assert second.disposition is DeliveryDisposition.RETRY
        assert token_calls == 2

    asyncio.run(scenario())


def test_short_lived_token_is_not_reused_and_invalid_token_is_terminal(tmp_path: Path) -> None:
    async def short_lived_scenario() -> int:
        token_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.host == "auth.test":
                token_calls += 1
                return httpx.Response(
                    200,
                    json={"access_token": f"token-{token_calls}", "expires_in": 5},
                )
            return httpx.Response(201, json=_acknowledgment())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(_settings(tmp_path), client)
            await service.deliver(_message())
            await service.deliver(_message())
        return token_calls

    async def invalid_scenario() -> DeliveryDisposition:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": None, "expires_in": 60})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await MeetingServiceClient(_settings(tmp_path), client).deliver(_message())
        return result.disposition

    assert asyncio.run(short_lived_scenario()) == 2
    assert asyncio.run(invalid_scenario()) is DeliveryDisposition.TERMINAL


def test_429_honors_retry_after_and_400_is_terminal(tmp_path: Path) -> None:
    async def scenario(status: int, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(status, headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await MeetingServiceClient(_settings(tmp_path), client).deliver(_message())

    throttled = asyncio.run(scenario(429, {"Retry-After": "17"}))
    rejected = asyncio.run(scenario(400))
    assert throttled.disposition is DeliveryDisposition.RETRY
    assert throttled.retry_after_sec == 17.0
    assert rejected.disposition is DeliveryDisposition.TERMINAL


def test_token_request_body_matches_auth_service_contract(tmp_path: Path) -> None:
    """auth-service ServiceTokenController requires `audience` (400 invalid_audience
    otherwise) and reads `permissions` (repeated form param); it ignores `scope`.
    Regression: #248 sent only `scope`, so a token was never mintable against the
    live auth-service — the mock harness hid it. Pin the exact form body."""

    async def scenario() -> None:
        captured: dict[str, list[str]] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                # httpx encodes form data; parse it back to the multidict the backend sees.
                from urllib.parse import parse_qs

                parsed = parse_qs(request.content.decode(), keep_blank_values=True)
                captured.update(parsed)
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            return httpx.Response(201, json=_acknowledgment())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(_settings(tmp_path), client)
            await service.deliver(_message())

        assert captured.get("grant_type") == ["client_credentials"]
        # audience is required by auth-service (400 invalid_audience otherwise)
        assert captured.get("audience") == ["meeting-service"]
        assert captured.get("permissions") == ["meeting:analysis-result:write"]
        assert "scope" not in captured, "auth-service ignores scope; sending it is misleading"

    asyncio.run(scenario())


def test_multiple_permissions_emit_repeated_form_fields(tmp_path: Path) -> None:
    """auth-service binds `permissions` as a repeated form param. httpx encodes a list
    value into repeated fields; a scalar would silently drop a second permission."""

    async def scenario() -> None:
        captured: dict[str, list[str]] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                from urllib.parse import parse_qs

                captured.update(parse_qs(request.content.decode(), keep_blank_values=True))
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            return httpx.Response(201, json=_acknowledgment())

        settings = _settings(tmp_path)
        settings.meeting_service_scope = (
            "meeting:analysis-result:write, meeting:analysis-result:read"
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(settings, client)
            await service.deliver(_message())

        assert captured.get("permissions") == [
            "meeting:analysis-result:write",
            "meeting:analysis-result:read",
        ]

    asyncio.run(scenario())


def test_mtls_context_is_pinned_and_reloaded_when_key_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "client.pem"
    key_path = tmp_path / "client.key"
    for path, content in (
        (ca_path, "ca"),
        (cert_path, "cert"),
        (key_path, "key-v1"),
    ):
        path.write_text(content, encoding="utf-8")
    settings = _settings(tmp_path)
    settings.meeting_service_tls_mode = "mutual"
    settings.meeting_service_tls_ca_path = ca_path
    settings.meeting_service_tls_client_cert_path = cert_path
    settings.meeting_service_tls_client_key_path = key_path
    settings.meeting_service_tls_reload_interval_sec = 1.0

    contexts: list[object] = []
    loaded: list[tuple[str, str]] = []

    class FakeContext:
        minimum_version: object | None = None
        check_hostname = False
        verify_mode: object | None = None

        def load_cert_chain(self, *, certfile: str, keyfile: str) -> None:
            loaded.append((certfile, keyfile))

    def create_default_context(*, cafile: str | None = None) -> FakeContext:
        assert cafile == str(ca_path)
        context = FakeContext()
        contexts.append(context)
        return context

    clients: list[httpx.AsyncClient] = []

    def client_factory(context: object) -> httpx.AsyncClient:
        assert context in contexts
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(204))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(client_module.ssl, "create_default_context", create_default_context)

    async def scenario() -> None:
        transport = ReloadingHttpClient(settings, client_factory=client_factory)  # type: ignore[arg-type]
        first = await transport._get_client()
        key_path.write_text("key-v2-rotated", encoding="utf-8")
        transport._next_check = 0.0
        second = await transport._get_client()
        assert first is not second
        assert first.is_closed
        await transport.aclose()
        assert second.is_closed

    asyncio.run(scenario())
    assert len(contexts) == 2
    assert loaded == [(str(cert_path), str(key_path)), (str(cert_path), str(key_path))]


def test_missing_rotated_tls_material_is_retryable_without_secret_detail(tmp_path: Path) -> None:
    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "client.pem"
    key_path = tmp_path / "client.key"
    for path in (ca_path, cert_path, key_path):
        path.write_text("test-material", encoding="utf-8")
    settings = _settings(tmp_path)
    settings.meeting_service_tls_mode = "mutual"
    settings.meeting_service_tls_ca_path = ca_path
    settings.meeting_service_tls_client_cert_path = cert_path
    settings.meeting_service_tls_client_key_path = key_path
    key_path.unlink()

    result = asyncio.run(MeetingServiceClient(settings).deliver(_message()))
    assert result.disposition is DeliveryDisposition.RETRY
    assert result.error_code == "token_network_MeetingServiceTlsError"
    assert str(key_path) not in result.error_code


def test_rotated_pool_waits_for_inflight_request_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "client.pem"
    key_path = tmp_path / "client.key"
    for path in (ca_path, cert_path, key_path):
        path.write_text("test-material-v1", encoding="utf-8")
    settings = _settings(tmp_path)
    settings.meeting_service_tls_mode = "mutual"
    settings.meeting_service_tls_ca_path = ca_path
    settings.meeting_service_tls_client_cert_path = cert_path
    settings.meeting_service_tls_client_key_path = key_path
    settings.meeting_service_tls_reload_interval_sec = 1.0

    class FakeContext:
        minimum_version: object | None = None
        check_hostname = False
        verify_mode: object | None = None

        def load_cert_chain(self, *, certfile: str, keyfile: str) -> None:
            assert certfile == str(cert_path)
            assert keyfile == str(key_path)

    monkeypatch.setattr(
        client_module.ssl,
        "create_default_context",
        lambda **kwargs: FakeContext(),
    )

    request_started = asyncio.Event()
    release_request = asyncio.Event()
    clients: list[httpx.AsyncClient] = []

    def client_factory(context: object) -> httpx.AsyncClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            request_started.set()
            await release_request.wait()
            return httpx.Response(204)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    async def scenario() -> None:
        transport = ReloadingHttpClient(settings, client_factory=client_factory)  # type: ignore[arg-type]
        pending = asyncio.create_task(transport.post("https://meeting.test/slow"))
        await request_started.wait()

        key_path.write_text("test-material-v2-rotated", encoding="utf-8")
        transport._next_check = 0.0
        replacement = await transport._get_client()

        assert len(clients) == 2
        assert replacement is clients[1]
        assert not clients[0].is_closed

        release_request.set()
        await pending
        assert clients[0].is_closed
        await transport.aclose()
        assert clients[1].is_closed

    asyncio.run(scenario())


def test_real_mtls_handshake_requires_trusted_client_certificate(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "meeting-ai test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def issue_leaf(
        common_name: str,
        usage: x509.ObjectIdentifier,
        *,
        server_name: str | None = None,
    ) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=True)
        )
        if server_name is not None:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName(server_name),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
        return key, builder.sign(ca_key, hashes.SHA256())

    server_key, server_cert = issue_leaf(
        "localhost",
        ExtendedKeyUsageOID.SERVER_AUTH,
        server_name="localhost",
    )
    client_key, client_cert = issue_leaf(
        "meeting-ai",
        ExtendedKeyUsageOID.CLIENT_AUTH,
    )

    ca_path = tmp_path / "ca.pem"
    server_cert_path = tmp_path / "server.pem"
    server_key_path = tmp_path / "server.key"
    client_cert_path = tmp_path / "client.pem"
    client_key_path = tmp_path / "client.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    for path, certificate in (
        (server_cert_path, server_cert),
        (client_cert_path, client_cert),
    ):
        path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    for path, key in ((server_key_path, server_key), (client_key_path, client_key)):
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(str(server_cert_path), str(server_key_path))
    server_context.load_verify_locations(cafile=str(ca_path))
    server_context.verify_mode = ssl.CERT_REQUIRED
    accepted_paths: list[str] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header_bytes = await reader.readuntil(b"\r\n\r\n")
            header_lines = header_bytes.decode("ascii").split("\r\n")
            path = header_lines[0].split(" ")[1]
            content_length = next(
                (
                    int(line.split(":", 1)[1].strip())
                    for line in header_lines[1:]
                    if line.lower().startswith("content-length:")
                ),
                0,
            )
            if content_length:
                await reader.readexactly(content_length)
            assert writer.get_extra_info("peercert") is not None
            accepted_paths.append(path)
            if path == "/oauth2/token":
                status = "200 OK"
                body = b'{"access_token":"ephemeral-test-token","expires_in":60}'
            else:
                status = "201 Created"
                body = json.dumps(_acknowledgment()).encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def scenario() -> tuple[DeliveryAttempt, DeliveryAttempt]:
        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        base_url = f"https://127.0.0.1:{port}"
        mutual = _settings(tmp_path)
        mutual.meeting_service_base_url = base_url
        mutual.meeting_service_token_url = f"{base_url}/oauth2/token"
        mutual.meeting_service_tls_mode = "mutual"
        mutual.meeting_service_tls_ca_path = ca_path
        mutual.meeting_service_tls_client_cert_path = client_cert_path
        mutual.meeting_service_tls_client_key_path = client_key_path
        mutual_client = MeetingServiceClient(mutual)

        no_certificate = _settings(tmp_path)
        no_certificate.meeting_service_base_url = base_url
        no_certificate.meeting_service_token_url = f"{base_url}/oauth2/token"
        no_certificate.meeting_service_tls_ca_path = ca_path
        no_certificate_client = MeetingServiceClient(no_certificate)
        try:
            accepted = await mutual_client.deliver(_message())
            rejected = await no_certificate_client.deliver(_message())
            return accepted, rejected
        finally:
            await mutual_client.aclose()
            await no_certificate_client.aclose()
            server.close()
            await server.wait_closed()

    accepted, rejected = asyncio.run(scenario())
    assert accepted.disposition is DeliveryDisposition.DELIVERED, accepted.error_code
    assert rejected.disposition is DeliveryDisposition.RETRY, rejected.error_code
    assert accepted_paths == [
        "/oauth2/token",
        "/api/v1/internal/meetings/11111111-1111-4111-8111-111111111111/analysis-results",
    ]
