#!/usr/bin/env python3
"""Fetch a direct public HTTP(S) file as raw bytes for a GitHub Release asset."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import tempfile
import unicodedata
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 1900 * 1024 * 1024  # Below GitHub's 2 GiB per-release-asset limit.
CHUNK_SIZE = 1024 * 1024
TIMEOUT_SECONDS = 60


class DownloadError(Exception):
    pass


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


def download(url: str, requested_name: str, output_dir: Path) -> dict:
    validate_public_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)

    request = Request(
        url,
        headers={"User-Agent": "RM007-Direct-Link-Downloader/1.0", "Accept": "*/*"},
        method="GET",
    )
    opener = build_opener(PublicOnlyRedirectHandler())

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
        if declared_size:
            try:
                declared_value = int(declared_size)
            except ValueError:
                declared_value = None
            if declared_value is not None and declared_value > MAX_BYTES:
                raise DownloadError("The file exceeds the 1900 MiB limit.")

        filename = choose_filename(response.headers, response.geturl(), requested_name)
        final_path = output_dir / filename
        if final_path.exists():
            raise DownloadError("A file with that output name already exists.")

        digest = hashlib.sha256()
        total = 0
        fd, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=output_dir)
        try:
            with os.fdopen(fd, "wb") as destination:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise DownloadError("The file exceeded the 1900 MiB limit.")
                    destination.write(chunk)
                    digest.update(chunk)

            if total == 0:
                raise DownloadError("The server returned an empty file.")
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
        metadata = download(url, requested_name, output_dir)
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
        output.write(f"sha256={metadata['sha256']}\n")
        output.write(f"content_type={metadata['content_type']}\n")
        output.write(f"source_host={metadata['source_host']}\n")

    print(f"Downloaded: {metadata['file_name']}")
    print(f"Size: {metadata['size_mib']} MiB")
    print(f"SHA-256: {metadata['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
