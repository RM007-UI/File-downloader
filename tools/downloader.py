#!/usr/bin/env python3
"""Fetch a direct public HTTP(S) file as raw bytes for a GitHub Release asset."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import time
import unicodedata
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

MAX_BYTES = 1900 * 1024 * 1024  # Below GitHub's 2 GiB per-release-asset limit.
CHUNK_SIZE = 1024 * 1024
TIMEOUT_SECONDS = 60


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

    percent = min(100.0, downloaded * 100.0 / total)
    remaining = max(0, total - downloaded)
    eta = remaining / speed if speed > 0 else 0
    filled = min(20, int(percent // 5))
    bar = "#" * filled + "-" * (20 - filled)
    return (
        f"[{bar}] {percent:5.1f}% | Downloaded: {format_bytes(downloaded)} / "
        f"{format_bytes(total)} | Remaining: {format_bytes(remaining)} | "
        f"Speed: {speed_text} | ETA: {format_duration(eta)}"
    )


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
            try:
                records = socket.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            except socket.gaierror:
                raise DownloadError("The hostname could not be resolved.") from None
            addresses = {
                ipaddress.ip_address(record[4][0].split("%", 1)[0])
                for record in records
            }

        if not addresses or any(not address.is_global for address in addresses):
            raise DownloadError("Private, local, reserved, and link-local destinations are blocked.")
    except ValueError:
        raise DownloadError("The URL is malformed.") from None


class PublicOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_ssl_context(skip_tls_verification: bool = False) -> ssl.SSLContext:
    """Use normal certificate checks by default; bypass only when explicitly requested."""
    if skip_tls_verification:
        print(
            "WARNING: TLS certificate verification is DISABLED for this run. "
            "The downloaded file could be intercepted or modified."
        )
        return ssl._create_unverified_context()
    return ssl.create_default_context()


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


def download(
    url: str,
    requested_name: str,
    output_dir: Path,
    skip_tls_verification: bool = False,
) -> dict:
    validate_public_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)

    request = Request(
        url,
        headers={"User-Agent": "RM007-Direct-Link-Downloader/1.0", "Accept": "*/*"},
        method="GET",
    )
    ssl_context = build_ssl_context(skip_tls_verification)
    opener = build_opener(
        PublicOnlyRedirectHandler(),
        HTTPSHandler(context=ssl_context),
    )

    try:
        response = opener.open(request, timeout=TIMEOUT_SECONDS)
    except HTTPError as exc:
        raise DownloadError(f"The file host returned HTTP {exc.code}.") from None
    except URLError as exc:
        reason = getattr(exc, "reason", "network error")
        raise DownloadError(f"Could not reach the file host ({type(reason).__name__}).") from None
    except TimeoutError:
        raise DownloadError("The file host timed out.") from None

    with response:
        validate_public_url(response.geturl())
        status = getattr(response, "status", 200)
        if status < 200 or status >= 300:
            raise DownloadError(f"The file host returned HTTP {status}.")

        declared_size = response.headers.get("Content-Length")
        total_size = None
        if declared_size:
            try:
                declared_value = int(declared_size)
            except ValueError:
                declared_value = None
            if declared_value is not None:
                total_size = declared_value
                if declared_value > MAX_BYTES:
                    raise DownloadError("The file exceeds the 1900 MiB limit.")

        filename = choose_filename(response.headers, response.geturl(), requested_name)
        final_path = output_dir / filename
        if final_path.exists():
            raise DownloadError("A file with that output name already exists.")

        digest = hashlib.sha256()
        total = 0
        started_at = time.monotonic()
        last_report_at = 0.0
        last_reported_percent = -5.0

        def report_progress(force: bool = False) -> None:
            nonlocal last_report_at, last_reported_percent
            now = time.monotonic()
            elapsed = now - started_at
            percent = (total * 100.0 / total_size) if total_size else None
            time_due = now - last_report_at >= 2.0
            percent_due = (
                percent is not None
                and percent - last_reported_percent >= 5.0
            )
            if force or time_due or percent_due:
                print(
                    "[download-progress] " + progress_message(total, total_size, elapsed),
                    flush=True,
                )
                last_report_at = now
                if percent is not None:
                    last_reported_percent = percent

        fd, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=output_dir)
        try:
            with os.fdopen(fd, "wb") as destination:
                report_progress(force=True)
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise DownloadError("The file exceeded the 1900 MiB limit.")
                    destination.write(chunk)
                    digest.update(chunk)
                    report_progress()

            if total == 0:
                raise DownloadError("The server returned an empty file.")
            elapsed_seconds = time.monotonic() - started_at
            report_progress(force=True)
            os.replace(temporary_name, final_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

        metadata = {
            "file_name": filename,
            "size_bytes": total,
            "size_mib": round(total / (1024 * 1024), 2),
            "duration_seconds": round(elapsed_seconds, 2),
            "average_speed_bytes_per_second": round(total / max(elapsed_seconds, 0.001), 2),
            "sha256": digest.hexdigest(),
            "content_type": response.headers.get("Content-Type", "unknown"),
            "source_host": urlsplit(response.geturl()).hostname,
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
        metadata = download(url, requested_name, output_dir, skip_tls_verification=skip_tls)
    except DownloadError as exc:
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
        output.write(f"sha256={metadata['sha256']}\n")
        output.write(f"content_type={metadata['content_type']}\n")
        output.write(f"source_host={metadata['source_host']}\n")

    print(f"Downloaded: {metadata['file_name']}")
    print(f"Size: {metadata['size_mib']} MiB")
    print(f"SHA-256: {metadata['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
