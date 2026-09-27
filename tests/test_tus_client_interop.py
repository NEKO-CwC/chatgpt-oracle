from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_tus_js_client_interoperates_with_upload_protocol(tmp_path: Path) -> None:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    node = shutil.which("node.exe") or shutil.which("node")
    if not npm or not node:
        pytest.skip("Node and npm are required for the optional tus-js-client interoperability test")

    package_dir = tmp_path / "client"
    package_dir.mkdir()
    install = subprocess.run(
        [npm, "install", "--prefix", str(package_dir), "--no-save", "--no-package-lock", "--ignore-scripts", "tus-js-client@4.3.1"],
        cwd=package_dir,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if install.returncode != 0:
        pytest.skip(f"pinned tus-js-client could not be installed: {install.stderr[-500:]}")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server_root = tmp_path / "server"
    environment = os.environ.copy()
    environment.update(
        {
            "CHATGPT_ORACLE_ROOT": str(server_root),
            "CHATGPT_ORACLE_JOBS_DIR": str(server_root / "jobs"),
            "CHATGPT_SESSIONS_DIR": str(server_root / "sessions"),
            "CHATGPT_ORACLE_TRANSPORT": "http",
            "CHATGPT_ORACLE_HOST": "127.0.0.1",
            "CHATGPT_ORACLE_PORT": str(port),
            "CHATGPT_ORACLE_BEARER_TOKEN": "interop-test-token",
            "CHATGPT_BROWSER_CDP_URL": "",
            "CHATGPT_SESSION_TTL_SECONDS": "1800",
            "CHATGPT_FILE_STORE_DIR": str(server_root / "file-store"),
            "CHATGPT_FILE_STORE_MAX_FILE_BYTES": "1048576",
            "CHATGPT_FILE_STORE_MAX_CHUNK_BYTES": "32768",
            "CHATGPT_FILE_STORE_TTL_SECONDS": "1800",
        }
    )
    server_code = (
        "from chatgpt_oracle.server import AuthHealthMiddleware, manager, mcp, settings; "
        "manager.sessions.create('interop-session'); "
        "import uvicorn; "
        "app=AuthHealthMiddleware(mcp.streamable_http_app(), settings.bearer_token, manager.uploads, manager); "
        f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')"
    )
    server = subprocess.Popen(
        [sys.executable, "-c", server_code],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    content = (b"tus-js-client interoperability\x00" * 1900)[:48_000]
    digest = hashlib.sha256(content).hexdigest()
    endpoint = f"http://127.0.0.1:{port}/v2/uploads"
    client_code = r"""
const tus = require('tus-js-client');
const crypto = require('crypto');
const data = Buffer.from('tus-js-client interoperability\0'.repeat(1900)).subarray(0, 48000);
const digest = crypto.createHash('sha256').update(data).digest('hex');
const endpoint = process.argv[1];
const headers = {
  'Authorization': 'Bearer interop-test-token',
  'X-Oracle-Session-ID': 'interop-session'
};
const upload = new tus.Upload(data, {
  endpoint,
  headers,
  uploadSize: data.length,
  chunkSize: 32768,
  retryDelays: [],
  metadata: {
    session_id: 'interop-session',
    filename: 'interop.bin',
    mime_type: 'application/octet-stream',
    size_bytes: String(data.length),
    sha256: digest
  },
  onError(error) {
    process.stderr.write(String(error));
    process.exitCode = 1;
  },
  onSuccess() {
    fetch(upload.url, { method: 'HEAD', headers: { ...headers, 'Tus-Resumable': '1.0.0' } }).then(async response => {
      if (!response.ok) throw new Error(`HEAD returned ${response.status}`);
      process.stdout.write('INTEROP=' + JSON.stringify({
        offset: response.headers.get('upload-offset'),
        length: response.headers.get('upload-length'),
        sha256: response.headers.get('x-content-sha256'),
        file_ref: response.headers.get('x-oracle-file-ref'),
        digest,
        size: data.length
      }));
    }).catch(error => {
      process.stderr.write(String(error));
      process.exitCode = 1;
    });
  }
});
upload.start();
"""
    try:
        health = f"http://127.0.0.1:{port}/healthz"
        import urllib.request

        ready = False
        for _ in range(100):
            if server.poll() is not None:
                pytest.fail(f"interop server exited with code {server.returncode}")
            try:
                with urllib.request.urlopen(health, timeout=1) as response:
                    ready = response.status == 200
                if ready:
                    break
            except OSError:
                time.sleep(0.1)
        assert ready, "interop server did not become ready"
        client = subprocess.run(
            [node, "-e", client_code, endpoint],
            cwd=package_dir,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert client.returncode == 0, client.stderr
        payload_line = next(line for line in client.stdout.splitlines() if line.startswith("INTEROP="))
        receipt = json.loads(payload_line.removeprefix("INTEROP="))
        assert receipt["offset"] == receipt["length"] == str(len(content))
        assert receipt["sha256"] == receipt["digest"] == digest
        assert receipt["file_ref"].startswith("fref_")
        assert receipt["size"] == len(content)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
