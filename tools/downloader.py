#!/usr/bin/env python3
"""High-performance multi-connection direct HTTP(S) downloader."""
from __future__ import annotations

import functools
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

DEFAULT_MAX_BYTES = 1900 * 1024 * 1024
MAX_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(DEFAULT_MAX_BYTES)))
DOWNLOAD_PROGRESS_CALLBACK = None
CHUNK_SIZE = 8 * 1024 * 1024          # 8 MiB — fewer I/O ops, higher throughput
MIN_PARALLEL_SIZE = 4 * 1024 * 1024
TIMEOUT_SECONDS = 90
RETRIES = 3
MAX_ALLOWED_CONNECTIONS = 32
DNS_CACHE_SIZE = 128
WRITE_BATCH_BYTES = 16 * 1024 * 1024  # Batch in-RAM writes before pwrite
RECV_BUF_SIZE = 512 * 1024            # Large socket receive buffer


class DownloadError(Exception):
    pass


def format_bytes(value: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(max(0, value))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def progress_message(downloaded: int, total: int | None, elapsed: float) -> str:
    speed = downloaded / max(elapsed, 0.001)
    speed_text = f"{format_bytes(speed)}/s"
    if total is None or total <= 0:
        return (
            f"Downloaded: {format_bytes(downloaded)} | Total: unknown | "
            f"Remaining: unknown | Progress: n/a | Speed: {speed_text} | "
            f"Elapsed: {format_duration(elapsed)}"
        )

    percent = min(100.0, max(0.0, downloaded * 100.0 / total))
    remaining = max(0, total - downloaded)
    eta = remaining / speed if speed > 0 else 0
    filled = min(20, int(percent // 5))
    bar = "#" * filled + "-" * (20 - filled)
    return (
        f"[{bar}] {percent:5.1f}% | Downloaded: {format_bytes(downloaded)} / "
        f"{format_bytes(total)} | Remaining: {format_bytes(remaining)} | "
        f"Speed: {speed_text} | ETA: {format_duration(eta)}"
    )


class ProgressReporter:
    """Thread-safe aggregate progress with batched updates."""

    def __init__(self, total: int | None):
        self.total = total
        self.started_at = time.monotonic()
        self.downloaded = 0
        self.last_report_at = 0.0
        self.last_percent = -5.0
        self.lock = threading.Lock()
        self._pending_delta = 0
        self._pending_lock = threading.Lock()

    def add(self, delta: int) -> None:
        with self._pending_lock:
            self._pending_delta += delta

    def flush(self, force: bool = False) -> None:
        with self._pending_lock:
            delta = self._pending_delta
            self._pending_delta = 0
        if delta == 0 and not force:
            return
        with self.lock:
            self.downloaded = max(0, self.downloaded + delta)
            now = time.monotonic()
            elapsed = now - self.started_at
            percent = (
                self.downloaded * 100.0 / self.total
                if self.total and self.total > 0
                else None
            )
            time_due = now - self.last_report_at >= 2.0
            percent_due = percent is not None and percent - self.last_percent >= 5.0
            if force or time_due or percent_due:
                message = progress_message(self.downloaded, self.total, elapsed)
                print("[download-progress] " + message, flush=True)
                self.last_report_at = now
                if percent is not None:
                    self.last_percent = percent
                callback = DOWNLOAD_PROGRESS_CALLBACK
                if callback is not None:
                    try:
                        callback(self.downloaded, self.total, elapsed, message)
                    except Exception as exc:
                        print(
                            "[progress-callback-warning] " + type(exc).__name__,
                            flush=True,
                        )

    def finish(self) -> float:
        self.flush(force=True)
        return time.monotonic() - self.started_at


def plan_ranges(total: int, max_connections: int) -> list[tuple[int, int]]:
    """Return inclusive, contiguous byte ranges covering [0, total-1]."""
    if total <= 0:
        return []
    connections = max(1, min(int(max_connections), MAX_ALLOWED_CONNECTIONS))
    connections = min(connections, max(1, (total + MIN_PARALLEL_SIZE - 1) // MIN_PARALLEL_SIZE))
    base, extra = divmod(total, connections)
    ranges = []
    start = 0
    for index in range(connections):
        length = base + (1 if index < extra else 0)
        end = start + length - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", value.strip(), re.IGNORECASE)
    if not match:
        return None
    start, end, total = map(int, match.groups())
    if start > end or total <= end:
        return None
    return start, end, total


@functools.lru_cache(maxsize=DNS_CACHE_SIZE)
def _resolve_host(host: str, port: int) -> set[str]:
    """Cached DNS resolution to avoid repeated lookups."""
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise DownloadError("The hostname could not be resolved.") from None
    return {
        ipaddress.ip_address(record[4][0].split("%", 1)[0])
        for record in records
    }


def validate_public_url(url: str) -> None:
    """Allow public HTTP(S) destinations only, including redirect destinations."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise DownloadError("Only direct HTTP or HTTPS URLs are supported.")
        if parsed.username is not None or parsed.password is not None:
            raise DownloadError("URLs with embedded credentials are not supported.")

        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise DownloadError("Local network destinations are blocked.")

        try:
            addresses = {ipaddress.ip_address(host)}
        except ValueError:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            addresses = _resolve_host(host, port)

        if not addresses or any(not address.is_global for address in addresses):
            raise DownloadError("Private, local, reserved, and link-local destinations are blocked.")
    except ValueError:
        raise DownloadError("The URL is malformed.") from None


class PublicOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _tune_socket(sock: socket.socket) -> None:
    """Apply TCP/socket optimizations for high-throughput downloads."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUF_SIZE)
    except (OSError, AttributeError):
        pass


def build_ssl_context(skip_tls_verification: bool = False) -> ssl.SSLContext:
    if skip_tls_verification:
        print(
            "WARNING: TLS certificate verification is DISABLED for this run. "
            "The downloaded file could be intercepted or modified."
        )
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def make_opener(skip_tls_verification: bool = False):
    https_handler = HTTPSHandler(
        context=build_ssl_context(skip_tls_verification),
    )
    opener = build_opener(
        PublicOnlyRedirectHandler(),
        https_handler,
    )
    return opener


def make_request(url: str, extra_headers: dict[str, str] | None = None) -> Request:
    headers = {
        "User-Agent": "RM007-Direct-Link-Downloader/3.0",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    if extra_headers:
        headers.update(extra_headers)
    return Request(url, headers=headers, method="GET")


def safe_filename(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\\", "/").split("/")[-1]
    value = "".join(ch for ch in value if ch.isprintable() and ch not in '<>:"|?*')
    value = value.strip(" .")
    if not value or value in {".", ".."}:
        value = "downloaded-file.bin"
    if len(value) > 180:
        suffix = Path(value).suffix[:24]
        value = value[: max(1, 180 - len(suffix))] + suffix
    return value


def choose_filename(headers, final_url: str, requested_name: str) -> str:
    if requested_name.strip():
        return safe_filename(requested_name.strip())

    disposition = headers.get("Content-Disposition", "")
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        candidate = message.get_filename()
        if candidate:
            return safe_filename(candidate)

    segment = urlsplit(final_url).path.rsplit("/", 1)[-1]
    return safe_filename(unquote(segment) if segment else "")


def _open_response(opener, url: str, headers: dict[str, str] | None = None):
    try:
        response = opener.open(make_request(url, headers), timeout=TIMEOUT_SECONDS)
    except HTTPError as exc:
        if exc.code in {400, 405, 416, 501}:
            exc.close()
            return None
        raise DownloadError(f"The file host returned HTTP {exc.code}.") from None
    except URLError as exc:
        reason = getattr(exc, "reason", "network error")
        raise DownloadError(f"Could not reach the file host ({type(reason).__name__}).") from None
    except TimeoutError:
        raise DownloadError("The file host timed out.") from None

    validate_public_url(response.geturl())
    status = getattr(response, "status", 200)
    if status < 200 or status >= 300:
        response.close()
        raise DownloadError(f"The file host returned HTTP {status}.")
    return response


def _single_stream(response, output_dir: Path, filename: str, total_hint: int | None) -> tuple[Path, int, str, float, int]:
    total_size = total_hint
    content_type = response.headers.get("Content-Type", "unknown")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            declared_value = int(declared)
            if declared_value >= 0:
                total_size = declared_value
        except ValueError:
            pass
    if total_size is not None and total_size > MAX_BYTES:
        raise DownloadError("The file exceeds the 1900 MiB limit.")

    final_path = output_dir / filename
    digest = hashlib.sha256()
    reporter = ProgressReporter(total_size)
    fd, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=output_dir)
    try:
        with os.fdopen(fd, "wb") as destination:
            reporter.flush(force=True)
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                if reporter.downloaded + len(chunk) > MAX_BYTES:
                    raise DownloadError("The file exceeded the 1900 MiB limit.")
                destination.write(chunk)
                digest.update(chunk)
                reporter.add(len(chunk))
                reporter.flush()
        if reporter.downloaded == 0:
            raise DownloadError("The server returned an empty file.")
        elapsed = reporter.finish()
        os.replace(temporary_name, final_path)
        return final_path, reporter.downloaded, digest.hexdigest(), elapsed, 1
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    finally:
        response.close()


def _parallel_ranges(
    url: str,
    output_dir: Path,
    filename: str,
    total_size: int,
    requested_connections: int,
    skip_tls_verification: bool,
) -> tuple[Path, int, str, float, int]:
    ranges = plan_ranges(total_size, requested_connections)
    if len(ranges) < 2:
        raise DownloadError("The file is too small for a useful multi-connection transfer.")
    if not hasattr(os, "pwrite"):
        raise DownloadError("This runner does not support parallel positional writes.")

    final_path = output_dir / filename
    reporter = ProgressReporter(total_size)
    fd, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=output_dir)
    os.ftruncate(fd, total_size)
    per_segment_progress = [0] * len(ranges)
    progress_lock = threading.Lock()
    # Pre-create openers to warm TLS handshakes / DNS cache in parallel
    openers = [make_opener(skip_tls_verification) for _ in ranges]

    def worker(index: int, start: int, end: int) -> None:
        expected_length = end - start + 1
        last_error = "unknown range transfer error"
        opener = openers[index]
        for attempt in range(1, RETRIES + 1):
            with progress_lock:
                previous = per_segment_progress[index]
                if previous:
                    reporter.add(-previous)
                    per_segment_progress[index] = 0
            received = 0
            response = None
            try:
                response = _open_response(
                    opener,
                    url,
                    {"Range": f"bytes={start}-{end}"},
                )
                if response is None:
                    raise DownloadError("The server rejected an HTTP Range request.")
                with response:
                    if getattr(response, "status", 200) != 206:
                        raise DownloadError("The server ignored an HTTP Range request.")
                    content_range = parse_content_range(response.headers.get("Content-Range"))
                    if content_range != (start, end, total_size):
                        raise DownloadError("The server returned an unexpected Content-Range.")

                    # Batch writes into a memory buffer before pwrite
                    batch_buffer = bytearray()
                    batch_offset = start

                    while received < expected_length:
                        to_read = min(CHUNK_SIZE, expected_length - received)
                        chunk = response.read(to_read)
                        if not chunk:
                            raise DownloadError("A range ended before all requested bytes arrived.")

                        batch_buffer.extend(chunk)
                        received += len(chunk)

                        # Flush batch when large enough or at end
                        if len(batch_buffer) >= WRITE_BATCH_BYTES or received >= expected_length:
                            view = memoryview(batch_buffer)
                            written = 0
                            while written < len(view):
                                count = os.pwrite(fd, view[written:], batch_offset + written)
                                if count <= 0:
                                    raise OSError("A positional file write returned zero bytes.")
                                written += count
                            batch_offset += len(view)
                            batch_buffer = bytearray()

                        with progress_lock:
                            per_segment_progress[index] = received
                            reporter.add(len(chunk))
                        reporter.flush()

                    if response.read(1):
                        raise DownloadError("The server returned more bytes than the requested range.")
                    return
            except (DownloadError, OSError, URLError, TimeoutError) as exc:
                last_error = str(exc) or type(exc).__name__
                if response is not None:
                    response.close()
                if attempt < RETRIES:
                    print(
                        f"[range-retry] Part {index + 1}/{len(ranges)}, "
                        f"attempt {attempt + 1}/{RETRIES}: {last_error}",
                        flush=True,
                    )
                    time.sleep(min(2 ** (attempt - 1), 4))
            except Exception:
                if response is not None:
                    response.close()
                raise

        raise DownloadError(f"Range {start}-{end} failed after {RETRIES} attempts: {last_error}")

    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=len(ranges), thread_name_prefix="range-download")
    futures = [
        executor.submit(worker, index, start, end)
        for index, (start, end) in enumerate(ranges)
    ]
    try:
        for future in as_completed(futures):
            future.result()
        os.fsync(fd)
    except Exception:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        os.close(fd)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    else:
        executor.shutdown(wait=True)

    os.close(fd)
    elapsed = max(time.monotonic() - reporter.started_at, 0.001)
    reporter.finish()
    digest = hashlib.sha256()
    try:
        with open(temporary_name, "rb") as source:
            while True:
                block = source.read(CHUNK_SIZE)
                if not block:
                    break
                digest.update(block)
        if os.path.getsize(temporary_name) != total_size:
            raise DownloadError("The completed file size does not match the server's advertised size.")
        os.replace(temporary_name, final_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    return final_path, total_size, digest.hexdigest(), elapsed, len(ranges)


def download(
    url: str,
    requested_name: str,
    output_dir: Path,
    skip_tls_verification: bool = False,
    max_connections: int = 16,
) -> dict:
    validate_public_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    connections = max(1, min(int(max_connections), MAX_ALLOWED_CONNECTIONS))
    probe_opener = make_opener(skip_tls_verification)

    # Ask for the first byte. A 206 with Content-Range proves Range support.
    probe = None
    try:
        probe = _open_response(probe_opener, url, {"Range": "bytes=0-0"})
    except DownloadError:
        raise

    if probe is None:
        print("[download-mode] Range requests are not supported; using one connection.", flush=True)
        response = _open_response(make_opener(skip_tls_verification), url)
        if response is None:
            raise DownloadError("The server refused a normal GET request.")
        filename = choose_filename(response.headers, response.geturl(), requested_name)
        final_path, total, sha256, elapsed, used_connections = _single_stream(
            response, output_dir, filename, None
        )
        content_type = response.headers.get("Content-Type", "unknown")
        range_supported = False
    else:
        status = getattr(probe, "status", 200)
        filename = choose_filename(probe.headers, probe.geturl(), requested_name)

        content_type = probe.headers.get("Content-Type", "unknown")
        if status == 206:
            content_range = parse_content_range(probe.headers.get("Content-Range"))
            first_byte = probe.read(1)
            probe.close()
            if content_range and content_range[0] == 0 and content_range[1] >= 0 and len(first_byte) == 1:
                total_size = content_range[2]
                if total_size > MAX_BYTES:
                    raise DownloadError("The file exceeds the 1900 MiB limit.")
                ranges = plan_ranges(total_size, connections)
                if len(ranges) >= 2:
                    print(
                        f"[download-mode] HTTP Range supported; using {len(ranges)} parallel connections.",
                        flush=True,
                    )
                    try:
                        final_path, total, sha256, elapsed, used_connections = _parallel_ranges(
                            url, output_dir, filename, total_size, connections, skip_tls_verification
                        )
                        range_supported = True
                    except DownloadError as exc:
                        print(
                            f"[download-mode] Parallel ranges failed ({exc}); retrying with one connection.",
                            flush=True,
                        )
                        response = _open_response(make_opener(skip_tls_verification), url)
                        if response is None:
                            raise DownloadError("The server refused fallback GET.") from exc
                        final_path, total, sha256, elapsed, used_connections = _single_stream(
                            response, output_dir, filename, total_size
                        )
                        range_supported = False
                else:
                    response = _open_response(make_opener(skip_tls_verification), url)
                    if response is None:
                        raise DownloadError("The server refused a normal GET request.")
                    final_path, total, sha256, elapsed, used_connections = _single_stream(
                        response, output_dir, filename, total_size
                    )
                    range_supported = True
            else:
                print("[download-mode] The Range probe was incomplete; using one connection.", flush=True)
                response = _open_response(make_opener(skip_tls_verification), url)
                if response is None:
                    raise DownloadError("The server refused a normal GET request.")
                final_path, total, sha256, elapsed, used_connections = _single_stream(
                    response, output_dir, filename, None
                )
                range_supported = False
        elif status == 200:
            print("[download-mode] Server ignored Range; using the existing response as a single stream.", flush=True)
            total_hint = None
            declared = probe.headers.get("Content-Length")
            if declared:
                try:
                    total_hint = int(declared)
                except ValueError:
                    pass
            final_path, total, sha256, elapsed, used_connections = _single_stream(
                probe, output_dir, filename, total_hint
            )
            range_supported = False
        else:
            probe.close()
            raise DownloadError(f"Unexpected HTTP status during Range probe: {status}.")

    metadata = {
        "file_name": final_path.name,
        "size_bytes": total,
        "size_mib": round(total / (1024 * 1024), 2),
        "duration_seconds": round(elapsed, 2),
        "average_speed_bytes_per_second": round(total / max(elapsed, 0.001), 2),
        "sha256": sha256,
        "content_type": content_type,
        "source_host": urlsplit(url).hostname,
        "range_supported": range_supported,
        "connections_used": used_connections,
    }
    (output_dir / "download-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> int:
    url = os.environ.get("FILE_URL", "").strip()
    requested_name = os.environ.get("OUTPUT_FILENAME", "").strip()
    output_dir = Path(os.environ.get("OUTPUT_DIR", "release-output"))

    if os.environ.get("CONFIRM_PUBLIC_RIGHTS", "").lower() != "true":
        print("Public publishing was not confirmed. No file was downloaded.")
        return 2
    if not url:
        print("No direct URL was provided.")
        return 2

    try:
        skip_tls = os.environ.get("SKIP_TLS_VERIFICATION", "").lower() == "true"
        max_connections = int(os.environ.get("MAX_CONNECTIONS", "32"))
        metadata = download(
            url, requested_name, output_dir,
            skip_tls_verification=skip_tls,
            max_connections=max_connections,
        )
    except (DownloadError, ValueError) as exc:
        print(f"Download failed: {exc}")
        return 1
    except OSError as exc:
        print(f"Local file error: {type(exc).__name__}.")
        return 1

    output_file = output_dir / metadata["file_name"]
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"file_path={output_file.resolve()}\n")
        output.write(f"file_name={metadata['file_name']}\n")
        output.write(f"size_mib={metadata['size_mib']}\n")
        output.write(f"size_bytes={metadata['size_bytes']}\n")
        output.write(f"duration_seconds={metadata['duration_seconds']}\n")
        output.write(f"average_speed_bytes_per_second={metadata['average_speed_bytes_per_second']}\n")
        output.write(f"connections_used={metadata['connections_used']}\n")
        output.write(f"range_supported={str(metadata['range_supported']).lower()}\n")
        output.write(f"content_type={metadata['content_type']}\n")
        output.write(f"sha256={metadata['sha256']}\n")
        output.write(f"source_host={metadata['source_host']}\n")

    print(f"Downloaded: {metadata['file_name']}")
    print(f"Size: {metadata['size_mib']} MiB")
    print(f"Connections used: {metadata['connections_used']}")
    print(f"Range supported: {metadata['range_supported']}")
    print(f"SHA-256: {metadata['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
