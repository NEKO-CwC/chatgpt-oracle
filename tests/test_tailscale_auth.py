"""Exercise the HTTP trust boundary and file ingress without browser side effects."""
import asyncio
from pathlib import Path

import httpx
import pytest

from chatgpt_oracle.config import Settings
from chatgpt_oracle.server import AuthHealthMiddleware
from chatgpt_oracle.uploads import UploadStore

TAILSCALE_IP = "100.77.245.1"
TAILSCALE_V6 = "fd7a:115c:a1e0::4501:f503"


async def downstream(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"MCP reached"})


def app_for(tmp_path: Path, *, token="secret", addresses=(TAILSCALE_IP, TAILSCALE_V6)):
    return AuthHealthMiddleware(downstream, token, UploadStore(Settings.from_env(tmp_path)),
                                tailscale_ips=addresses)


@pytest.mark.parametrize("local,peer", [
    (TAILSCALE_IP, "100.117.226.6"),
    (TAILSCALE_IP, "100.79.198.28"),
    (TAILSCALE_V6, "fd7a:115c:a1e0::303b:c61f"),
])
def test_direct_tailscale_mcp_needs_no_bearer(tmp_path, local, peer):
    async def run():
        transport = httpx.ASGITransport(app_for(tmp_path), client=(peer, 12345))
        origin = f"http://[{local}]:8787" if ":" in local else f"http://{local}:8787"
        async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
            response = await client.post("/mcp", json={})
            assert response.status_code == 200
            assert response.text == "MCP reached"
    asyncio.run(run())


@pytest.mark.parametrize("local,peer", [
    ("127.0.0.1", "127.0.0.1"),  # Public FRP and local broker traffic.
    ("192.168.1.2", "100.117.226.6"),
    (TAILSCALE_IP, "192.168.1.3"),
    (TAILSCALE_IP, "203.0.113.10"),
    ("127.0.0.1", "100.117.226.6"),
])
def test_other_connections_still_require_bearer_even_with_spoofed_headers(tmp_path, local, peer):
    async def run():
        transport = httpx.ASGITransport(app_for(tmp_path), client=(peer, 12345))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{local}:8787") as client:
            forged = {"Host": f"{TAILSCALE_IP}:8787", "X-Forwarded-For": "100.117.226.6",
                      "Forwarded": f"for=100.117.226.6;host={TAILSCALE_IP}:8787"}
            response = await client.post("/mcp", headers=forged, json={})
            assert response.status_code == 401
            response = await client.post("/mcp", headers={**forged, "Authorization": "Bearer secret"}, json={})
            assert response.status_code == 200
    asyncio.run(run())


def test_disabled_tailscale_exception_preserves_bearer_auth(tmp_path):
    async def run():
        transport = httpx.ASGITransport(app_for(tmp_path, addresses=()), client=("100.117.226.6", 12345))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{TAILSCALE_IP}:8787") as client:
            assert (await client.post("/mcp", json={})).status_code == 401
    asyncio.run(run())


def test_no_token_configuration_does_not_open_other_interfaces(tmp_path):
    async def run():
        app = app_for(tmp_path, token=None)
        for local, peer, expected in [(TAILSCALE_IP, "100.117.226.6", 200), ("127.0.0.1", "127.0.0.1", 401)]:
            transport = httpx.ASGITransport(app, client=(peer, 12345))
            async with httpx.AsyncClient(transport=transport, base_url=f"http://{local}:8787") as client:
                response = await client.post("/mcp", headers={"Authorization": "Bearer None"}, json={})
                assert response.status_code == expected
    asyncio.run(run())


def test_tailnet_upload_is_accessible_but_internal_forwarding_stays_local(tmp_path):
    async def run():
        app = app_for(tmp_path)
        transport = httpx.ASGITransport(app, client=("100.117.226.6", 12345))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{TAILSCALE_IP}:8787") as client:
            response = await client.post("/uploads", content=b"tailnet input",
                                         headers={"X-Upload-Filename": "context.txt", "Content-Type": "text/plain"})
            assert response.status_code == 201
            assert response.json()["size"] == len(b"tailnet input")
            response = await client.post("/internal/exports/session-status", json={"session_id": "private"})
            assert response.status_code == 404
        # File ingress is also protected on non-Tailscale connections.
        transport = httpx.ASGITransport(app, client=("203.0.113.10", 12345))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{TAILSCALE_IP}:8787") as client:
            response = await client.post("/uploads", content=b"denied", headers={"X-Upload-Filename": "x.txt"})
            assert response.status_code == 401
    asyncio.run(run())


def test_missing_socket_addresses_do_not_bypass_auth(tmp_path):
    async def run():
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        await app_for(tmp_path)({"type": "http", "path": "/mcp", "headers": []}, receive, send)
        assert sent[0]["status"] == 401
    asyncio.run(run())
