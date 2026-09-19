"""Parser for the one trusted structured verification report format."""

from __future__ import annotations

from xml.etree import ElementTree

from agent.verification.contracts import (
    REPORT_SCHEMA,
    VerificationCase,
    VerificationCaseStatus,
    VerificationReport,
)


class VerificationReportError(ValueError):
    """The configured JUnit report is missing required deterministic evidence."""


def parse_junit_xml(data: bytes, *, check_id: str) -> VerificationReport:
    """Parse pytest's JUnit XML output without using logs as identity."""
    if not isinstance(data, bytes) or not data:
        raise VerificationReportError("JUnit report is empty.")
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, UnicodeError) as error:
        raise VerificationReportError("JUnit report is malformed.") from error
    if root.tag not in {"testsuite", "testsuites"}:
        raise VerificationReportError("JUnit report has an unsupported root element.")
    cases: list[VerificationCase] = []
    seen: set[str] = set()
    for testcase in root.iter("testcase"):
        classname = testcase.attrib.get("classname", "").strip()
        name = testcase.attrib.get("name", "").strip()
        if not classname or not name:
            raise VerificationReportError(
                "JUnit testcase requires classname and name attributes."
            )
        case_id = f"{classname}::{name}"
        if case_id in seen:
            raise VerificationReportError("JUnit report contains duplicate testcases.")
        seen.add(case_id)
        if testcase.find("error") is not None:
            status = VerificationCaseStatus.ERROR
        elif testcase.find("failure") is not None:
            status = VerificationCaseStatus.FAIL
        elif testcase.find("skipped") is not None:
            status = VerificationCaseStatus.SKIPPED
        else:
            status = VerificationCaseStatus.PASS
        cases.append(
            VerificationCase(
                check_id=check_id,
                case_id=case_id,
                status=status,
                report_schema=REPORT_SCHEMA,
            )
        )
    if not cases:
        raise VerificationReportError("JUnit report contains no testcases.")
    return VerificationReport(
        check_id=check_id,
        report_schema=REPORT_SCHEMA,
        cases=tuple(cases),
    )
