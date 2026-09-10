"""Pure text operations used by workspace transactions."""

import difflib
import hashlib

from agent.editing.models import EditErrorCode, EditProposal


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def apply_unique_replacement(
    working_content: str, proposal: EditProposal
) -> str | EditErrorCode:
    if not proposal.old_text:
        return EditErrorCode.EMPTY_OLD_TEXT
    line_ending = _line_ending(working_content)
    if line_ending is None:
        searchable = working_content
        old_text = proposal.old_text
        new_text = proposal.new_text
    else:
        searchable = _normalize_line_endings(working_content)
        old_text = _normalize_line_endings(proposal.old_text)
        new_text = _normalize_line_endings(proposal.new_text)
    occurrences = searchable.count(old_text)
    if occurrences == 0:
        return EditErrorCode.MATCH_NOT_FOUND
    if occurrences > 1:
        return EditErrorCode.MATCH_AMBIGUOUS
    updated = searchable.replace(old_text, new_text, 1)
    if line_ending not in (None, "\n"):
        updated = updated.replace("\n", line_ending)
    return updated


def make_diff(path: str, before: str, after: str, *, existed: bool) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True) if existed else [],
            after.splitlines(keepends=True),
            fromfile=path if existed else "/dev/null",
            tofile=path,
        )
    )


def _line_ending(text: str) -> str | None:
    without_crlf = text.replace("\r\n", "")
    has_crlf = "\r\n" in text
    has_lf = "\n" in without_crlf
    has_cr = "\r" in without_crlf
    if sum((has_crlf, has_lf, has_cr)) > 1:
        return None
    if has_crlf:
        return "\r\n"
    if has_cr:
        return "\r"
    return "\n"


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
