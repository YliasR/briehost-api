"""Malware scanning for uploaded zips. Uses clamd over TCP."""
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


def clamd_scan(zip_path: Path, host: str, port: int, timeout: float = 60.0) -> None:
    """
    Stream `zip_path` to clamd via INSTREAM and raise on detection.

    Wire format: send `zINSTREAM\\0`, then for each chunk a 4-byte big-endian length
    followed by the bytes, terminated by a zero-length chunk.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"zINSTREAM\0")
            with zip_path.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
            sock.sendall((0).to_bytes(4, "big"))
            response = _recv_all(sock).decode("utf-8", errors="replace").strip("\x00 \r\n")
    except OSError as exc:
        raise ScanError(f"clamd unreachable at {host}:{port}: {exc}") from exc

    # Responses: "stream: OK" or "stream: <SIG> FOUND" or "... ERROR"
    if response.endswith("OK"):
        return
    if response.endswith("FOUND"):
        signature = response.split(":", 1)[-1].strip().removesuffix(" FOUND").strip()
        raise MalwareDetected(signature or "unknown signature")
    raise ScanError(f"clamd error response: {response!r}")
