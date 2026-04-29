"""Malware scanning for uploaded zips. Uses clamd over a Unix socket or TCP."""
import socket
from pathlib import Path


class ScanError(RuntimeError):
    """Raised when the scanner cannot reach a verdict (daemon down, network, etc.)."""


class MalwareDetected(RuntimeError):
    """Raised when clamd reports a positive detection. Message contains the signature."""


def _recv_all(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _stream_scan(sock: socket.socket, zip_path: Path) -> str:
    sock.sendall(b"zINSTREAM\0")
    with zip_path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
    sock.sendall((0).to_bytes(4, "big"))
    return _recv_all(sock).decode("utf-8", errors="replace").strip("\x00 \r\n")


def _scan_via_unix_socket(zip_path: Path, socket_path: str, timeout: float) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        return _stream_scan(sock, zip_path)


def _scan_via_tcp(zip_path: Path, host: str, port: int, timeout: float) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        return _stream_scan(sock, zip_path)


def clamd_scan(zip_path: Path, host: str, port: int, socket_path: str | None = None, timeout: float = 60.0) -> None:
    """
    Stream `zip_path` to clamd via INSTREAM and raise on detection.

    Wire format: send `zINSTREAM\\0`, then for each chunk a 4-byte big-endian length
    followed by the bytes, terminated by a zero-length chunk.
    """
    try:
        if socket_path:
            socket_file = Path(socket_path)
            if socket_file.exists():
                response = _scan_via_unix_socket(zip_path, socket_path, timeout)
            else:
                response = _scan_via_tcp(zip_path, host, port, timeout)
        else:
            response = _scan_via_tcp(zip_path, host, port, timeout)
    except OSError as exc:
        target = socket_path if socket_path else f"{host}:{port}"
        raise ScanError(f"clamd unreachable at {target}: {exc}") from exc

    # Responses: "stream: OK" or "stream: <SIG> FOUND" or "... ERROR"
    if response.endswith("OK"):
        return
    if response.endswith("FOUND"):
        signature = response.split(":", 1)[-1].strip().removesuffix(" FOUND").strip()
        raise MalwareDetected(signature or "unknown signature")
    raise ScanError(f"clamd error response: {response!r}")
