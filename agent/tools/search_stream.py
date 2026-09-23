"""Bounded streaming helpers for workspace search continuation."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ChunkedFilePage:
    scanned_bytes: int
    next_offset: int
    next_line_number: int
    line_open: bool
    line_reported: bool
    complete: bool
    content_hash: str


def _decode_utf8_prefix(
    raw: bytes, *, at_eof: bool, strip_bom: bool
) -> tuple[bytes, str]:
    encoding = "utf-8-sig" if strip_bom else "utf-8"
    try:
        return raw, raw.decode(encoding)
    except UnicodeDecodeError as error:
        if (
            not at_eof
            and error.reason == "unexpected end of data"
            and error.end == len(raw)
        ):
            valid = raw[: error.start]
            return valid, valid.decode(encoding)
        raise


def search_chunked_file_page(
    path: str,
    file_path: Path,
    search_term: str,
    *,
    file_size: int,
    file_offset: int,
    line_number: int,
    line_open: bool,
    line_reported: bool,
    byte_budget: int,
    on_match: Callable[[dict[str, object], str], bool],
) -> ChunkedFilePage:
    """Scan one bounded byte window without storing source text in the cursor."""
    scanned_bytes = 0
    digest = hashlib.sha256()
    overlap_chars = max(0, len(search_term.casefold()) - 1)
    line_tail = ""

    with file_path.open("rb") as source:
        if line_open and overlap_chars and file_offset:
            overlap_bytes = min(
                file_offset,
                overlap_chars * 4 + 4,
                byte_budget,
            )
            source.seek(file_offset - overlap_bytes)
            prefix = source.read(overlap_bytes)
            scanned_bytes += len(prefix)
            prefix_text = prefix.decode("utf-8", errors="ignore")
            line_tail = prefix_text.rsplit("\n", 1)[-1][-overlap_chars:]
        source.seek(file_offset)

        while scanned_bytes < byte_budget:
            before_offset = source.tell()
            before_line_number = line_number
            before_line_open = line_open
            before_line_reported = line_reported
            raw = source.readline(byte_budget - scanned_bytes)
            if not raw:
                return ChunkedFilePage(
                    scanned_bytes,
                    source.tell(),
                    line_number,
                    line_open,
                    line_reported,
                    True,
                    digest.hexdigest(),
                )

            at_eof = source.tell() >= file_size
            valid, text = _decode_utf8_prefix(
                raw, at_eof=at_eof, strip_bom=before_offset == 0
            )
            scanned_bytes += len(raw)
            if not valid:
                # The budget ended inside a UTF-8 code point; retry its bytes next page.
                source.seek(before_offset)
                return ChunkedFilePage(
                    scanned_bytes,
                    before_offset,
                    line_number,
                    line_open,
                    line_reported,
                    False,
                    digest.hexdigest(),
                )
            if b"\x00" in valid:
                raise ValueError("binary")
            digest.update(valid)
            combined_line = line_tail + text
            complete_line = valid.endswith(b"\n")
            match_text = combined_line.rstrip("\r\n")
            matched = search_term.casefold() in match_text.casefold()

            if matched and not line_reported:
                segment_hash = hashlib.sha256(valid).hexdigest()
                metadata: dict[str, object] = {
                    "path": path,
                    "range": {
                        "start_line": line_number,
                        "end_line": line_number,
                    },
                    "match_line": line_number,
                    "content_hash": segment_hash,
                    "content_hash_scope": "scanned_segment",
                    "snippet_truncated": not complete_line,
                }
                preview = combined_line.rstrip("\r\n")
                display = (
                    f"File: {path}\nMatch found at line: {line_number}\n"
                    f"{preview}\n"
                    + ("-" * 50)
                )
                if not on_match(metadata, display):
                    return ChunkedFilePage(
                        scanned_bytes,
                        before_offset,
                        before_line_number,
                        before_line_open,
                        before_line_reported,
                        False,
                        digest.hexdigest(),
                    )
                line_reported = True

            if complete_line:
                line_number += 1
                line_open = False
                line_reported = False
                line_tail = ""
            else:
                line_open = True
                line_reported = line_reported or matched
                line_tail = combined_line[-overlap_chars:] if overlap_chars else ""

            file_offset = before_offset + len(valid)
            if len(valid) != len(raw):
                # Preserve a split code point for the next page rather than dropping it.
                return ChunkedFilePage(
                    scanned_bytes,
                    file_offset,
                    line_number,
                    line_open,
                    line_reported,
                    False,
                    digest.hexdigest(),
                )
            if file_offset >= file_size:
                return ChunkedFilePage(
                    scanned_bytes,
                    file_offset,
                    line_number,
                    line_open,
                    line_reported,
                    True,
                    digest.hexdigest(),
                )

    return ChunkedFilePage(
        scanned_bytes,
        file_offset,
        line_number,
        line_open,
        line_reported,
        file_offset >= file_size,
        digest.hexdigest(),
    )
