"""SSH local port-forward helpers for remote DB access."""
from __future__ import annotations

import logging
import socket
import subprocess
import time
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

from .paths import expand_path

LOG = logging.getLogger(__name__)

class SSHTunnel:
    """Local port-forward via OpenSSH: localhost:local → remote_bind via ssh_host."""

    def __init__(
        self,
        ssh_host: str,
        ssh_user: str,
        remote_bind_host: str,
        remote_bind_port: int,
        *,
        ssh_port: int = 22,
        identity_file: Optional[str] = None,
        local_bind_host: str = "127.0.0.1",
        local_bind_port: int = 0,
        extra_args: Optional[list[str]] = None,
        ready_timeout: float = 30.0,
    ):
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.identity_file = expand_path(identity_file)
        self.remote_bind_host = remote_bind_host
        self.remote_bind_port = int(remote_bind_port)
        self.local_bind_host = local_bind_host
        self.local_bind_port = int(local_bind_port)
        self.extra_args = list(extra_args or [])
        self.ready_timeout = ready_timeout
        self._proc: Optional[subprocess.Popen[bytes]] = None

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _port_open(self) -> bool:
        try:
            with socket.create_connection(
                (self.local_bind_host, self.local_bind_port), timeout=0.5
            ):
                return True
        except OSError:
            return False

    def start(self) -> int:
        if self.local_bind_port <= 0:
            self.local_bind_port = self._free_port()

        forward = (
            f"{self.local_bind_host}:{self.local_bind_port}:"
            f"{self.remote_bind_host}:{self.remote_bind_port}"
        )
        cmd = [
            "ssh",
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            forward,
            "-p",
            str(self.ssh_port),
        ]
        if self.identity_file:
            cmd.extend(["-i", self.identity_file])
        cmd.extend(self.extra_args)
        cmd.append(f"{self.ssh_user}@{self.ssh_host}")

        LOG.info(
            "Starting SSH tunnel %s → %s:%s via %s@%s",
            forward,
            self.remote_bind_host,
            self.remote_bind_port,
            self.ssh_user,
            self.ssh_host,
        )
        LOG.debug("SSH command: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                err = b""
                if self._proc.stderr:
                    err = self._proc.stderr.read() or b""
                raise RuntimeError(
                    f"SSH tunnel exited early (code {self._proc.returncode}): "
                    f"{err.decode(errors='replace').strip()}"
                )
            if self._port_open():
                LOG.info(
                    "SSH tunnel ready on %s:%s",
                    self.local_bind_host,
                    self.local_bind_port,
                )
                return self.local_bind_port
            time.sleep(0.1)

        self.stop()
        raise TimeoutError(
            f"SSH tunnel did not become ready within {self.ready_timeout}s"
        )

    def stop(self) -> None:
        if not self._proc:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        self._proc = None
        LOG.info("SSH tunnel closed")

    def __enter__(self) -> "SSHTunnel":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()


def rewrite_dsn_host_port(dsn: str, host: str, port: int) -> str:
    """Return DSN with host/port replaced (keeps user/pass/db/query)."""
    parsed = urlparse(dsn)
    userinfo = ""
    if parsed.username is not None:
        userinfo = quote(unquote(parsed.username), safe="")
        if parsed.password is not None:
            userinfo += ":" + quote(unquote(parsed.password), safe="")
        userinfo += "@"
    netloc = f"{userinfo}{host}:{port}"
    return parsed._replace(netloc=netloc).geturl()
