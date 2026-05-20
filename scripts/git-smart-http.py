#!/usr/bin/env python3
"""Minimal smart-HTTP git server for local testing of import.sh + csc.

Wraps `git http-backend` (a CGI program shipped with git) behind a
threading HTTPServer so a local directory of bare repos becomes pushable
and clonable over `http://127.0.0.1:<port>/<repo>.git`.

Usage:
  # Pre-create the bare repos you want to serve:
  mkdir -p /tmp/git-srv && for r in marketplace foo bar; do
    git init --bare --quiet "/tmp/git-srv/$r.git"
    # Allow http push:
    git -C "/tmp/git-srv/$r.git" config http.receivepack true
  done

  # Start the server:
  python3 scripts/git-smart-http.py --root /tmp/git-srv --port 8848

  # In another shell:
  cd /path/to/extracted/bundle
  ./import.sh http://127.0.0.1:8848

NOTE: this server is for local testing only — no auth, no TLS, no logging
sanitisation. Do not expose to a real network.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


GIT_HTTP_BACKEND_CANDIDATES = [
    "/usr/lib/git-core/git-http-backend",
    "/usr/libexec/git-core/git-http-backend",
    "/opt/homebrew/Cellar",  # macOS Homebrew — resolved below
    "/usr/local/Cellar",
]


def find_git_http_backend() -> Path:
    for p in GIT_HTTP_BACKEND_CANDIDATES:
        if p.endswith("Cellar"):
            base = Path(p) / "git"
            if base.is_dir():
                # newest git version dir
                versions = sorted(base.iterdir(), reverse=True)
                for v in versions:
                    cand = v / "libexec" / "git-core" / "git-http-backend"
                    if cand.is_file():
                        return cand
            continue
        path = Path(p)
        if path.is_file():
            return path
    # Last resort: ask git itself.
    res = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, check=True)
    path = Path(res.stdout.strip()) / "git-http-backend"
    if path.is_file():
        return path
    raise SystemExit("ERROR: could not locate git-http-backend on this system")


class Handler(BaseHTTPRequestHandler):
    server_version = "costrict-smart-http/0.1"
    root: Path  # set after server construction
    backend: Path

    def do_GET(self):  # noqa: N802
        self._invoke()

    def do_POST(self):  # noqa: N802
        self._invoke()

    def _read_chunked(self) -> bytes:
        """Decode HTTP/1.1 chunked transfer encoding from rfile."""
        chunks: list[bytes] = []
        while True:
            line = self.rfile.readline()
            if not line:
                break
            # chunk-size is hex, optional ;extension
            size_part = line.split(b";", 1)[0].strip()
            try:
                size = int(size_part, 16)
            except ValueError:
                break
            if size == 0:
                # trailing headers + final CRLF; consume until empty line
                while True:
                    trailer = self.rfile.readline()
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            chunk = b""
            while len(chunk) < size:
                more = self.rfile.read(size - len(chunk))
                if not more:
                    break
                chunk += more
            chunks.append(chunk)
            # consume CRLF after chunk data
            self.rfile.readline()
        return b"".join(chunks)

    def _invoke(self) -> None:
        path = self.path
        # Strip leading slash; e.g. "/foo.git/info/refs?service=git-upload-pack"
        path_info = "/" + path.lstrip("/").split("?", 1)[0]
        query = path.split("?", 1)[1] if "?" in path else ""
        env = os.environ.copy()
        env.update(
            {
                "GIT_PROJECT_ROOT": str(self.root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": path_info,
                "QUERY_STRING": query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", ""),
                "SERVER_PROTOCOL": self.protocol_version,
                "REMOTE_ADDR": self.client_address[0],
                "REMOTE_USER": "",
            }
        )
        body = b""
        if self.command == "POST":
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                body = self._read_chunked()
            else:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
            # git-http-backend reads CONTENT_LENGTH from env; reflect the actual decoded size
            env["CONTENT_LENGTH"] = str(len(body))
        proc = subprocess.Popen(
            [str(self.backend)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        out, err = proc.communicate(body)
        if proc.returncode != 0:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"git-http-backend error:\n" + err)
            return
        # Parse CGI response (headers + blank line + body)
        try:
            head, payload = out.split(b"\r\n\r\n", 1)
        except ValueError:
            head, payload = out.split(b"\n\n", 1)
        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.splitlines():
            line = line.decode("latin-1")
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if k.lower() == "status":
                status = int(v.split()[0])
            else:
                headers.append((k, v))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", required=True, type=Path, help="Directory containing bare repos (*.git).")
    p.add_argument("--port", type=int, default=8848)
    p.add_argument("--host", default="127.0.0.1")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.root.is_dir():
        print(f"ERROR: --root {args.root} is not a directory", file=sys.stderr)
        return 2
    # Auto-enable http.receivepack on any bare repos we discover.
    for repo in args.root.glob("*.git"):
        if (repo / "HEAD").is_file():
            subprocess.run(["git", "-C", str(repo), "config", "http.receivepack", "true"], check=False)
    Handler.root = args.root.resolve()
    Handler.backend = find_git_http_backend()
    print(f"serving {args.root} via {Handler.backend} on http://{args.host}:{args.port}/")
    print("Ctrl-C to stop.")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
