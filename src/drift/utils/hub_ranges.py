"""Bounded HTTP ranges for large immutable model artifacts."""

import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

RANGE_BYTES = 8 * 1024**2
RANGE_WORKERS = 4


def download_ranges(url, headers, partial, *, size, offset):
    """Append contiguous bytes only; the caller verifies SHA-256 before promotion.

    A first 200 response safely falls back to a complete sequential download.
    A range-supporting origin is read in bounded batches of four 8 MiB ranges.
    Failed ranges never leave gaps or preallocated unverified tails.
    """
    from drift.model_manifest import ManifestError

    started = time.monotonic()

    def retry_transfer(operation):
        for attempt in range(3):
            try:
                return operation()
            except requests.RequestException as exc:
                status = None if exc.response is None else exc.response.status_code
                if (
                    attempt == 2
                    or time.monotonic() - started > 3600
                    or (status is not None and status < 500 and status != 429)
                ):
                    raise
                time.sleep(0.5 * 2**attempt)

    def open_range(start, end):
        response = requests.get(
            url,
            headers=dict(headers, Range=f"bytes={start}-{end}"),
            stream=True,
            allow_redirects=True,
            timeout=(10, 60),
        )
        try:
            response.raise_for_status()
            if response.status_code == 206:
                expected = f"bytes {start}-{end}/{size}"
                if response.headers.get("Content-Range") != expected:
                    raise ManifestError("Hub returned an inconsistent bounded Content-Range")
            elif response.status_code != 200:
                raise ManifestError("Hub returned an unsupported artifact response")
            return response
        except BaseException:
            response.close()
            raise

    def read_exact(response, length):
        body = bytearray()
        until = time.monotonic() + 120
        for chunk in response.iter_content(chunk_size=min(1024**2, length)):
            if time.monotonic() > until:
                raise requests.Timeout("Bounded artifact range exceeded its transfer deadline")
            if len(body) + len(chunk) > length:
                raise ManifestError("Hub returned more bytes than the declared range")
            body.extend(chunk)
        if len(body) != length:
            raise requests.ConnectionError("Hub ended a bounded artifact range early")
        return body

    def fetch_once(start, end):
        with open_range(start, end) as response:
            if response.status_code != 206:
                raise ManifestError("Hub stopped honoring artifact ranges during transfer")
            return read_exact(response, end - start + 1)

    def fetch(start, end):
        return retry_transfer(lambda: fetch_once(start, end))

    end = min(size, offset + RANGE_BYTES) - 1

    def first_transfer():
        nonlocal offset, end
        # A failed 200 fallback truncates/replaces the old partial. An origin
        # may honor Range again on retry, so bind its next request to the prefix
        # actually present, not the offset from before that truncation.
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            return None  # The caller still verifies the complete hash.
        if offset > size:
            raise ManifestError("Artifact partial exceeds its declared length")
        end = min(size, offset + RANGE_BYTES) - 1
        with open_range(offset, end) as response:
            if response.status_code == 200:
                # Never append a complete response to an existing partial.
                written = 0
                with partial.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024**2):
                        if time.monotonic() - started > 3600:
                            raise requests.Timeout("Artifact transfer exceeded its deadline")
                        if written + len(chunk) > size:
                            raise ManifestError("Hub returned more bytes than the manifested artifact")
                        stream.write(chunk)
                        written += len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if written != size:
                    raise requests.ConnectionError("Hub ended the artifact before its declared length")
                return None
            return read_exact(response, end - offset + 1)

    first = retry_transfer(first_transfer)
    if first is None:
        return

    with partial.open("ab" if offset else "wb") as stream:
        stream.write(first)
        del first
        stream.flush()
        os.fsync(stream.fileno())
        offset = end + 1
        with ThreadPoolExecutor(max_workers=RANGE_WORKERS, thread_name_prefix="drift-artifact-range") as pool:
            while offset < size:
                if time.monotonic() - started > 3600:
                    raise requests.Timeout("Artifact transfer exceeded its deadline")
                ranges = [
                    (start, min(size, start + RANGE_BYTES) - 1)
                    for start in range(offset, min(size, offset + RANGE_WORKERS * RANGE_BYTES), RANGE_BYTES)
                ]
                futures = [pool.submit(fetch, start, end) for start, end in ranges]
                try:
                    for (start, end), future in zip(ranges, futures):
                        body = future.result()
                        assert start == offset
                        stream.write(body)
                        del body
                        stream.flush()
                        os.fsync(stream.fileno())
                        offset = end + 1
                finally:
                    for future in futures:
                        future.cancel()
                    futures.clear()
                    del future
