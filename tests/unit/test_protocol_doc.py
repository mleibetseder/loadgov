import re
from pathlib import Path

from loadgov.protocol import STATUS_SCHEMA

DOC = Path(__file__).resolve().parents[2] / "docs" / "protocol.md"


def test_protocol_doc_lists_exactly_the_schema_fields():
    text = DOC.read_text()
    section = text.split("## Status object", 1)[1]
    documented = set(re.findall(r"^\| `([^`]+)` \|", section, flags=re.M))
    assert documented == set(STATUS_SCHEMA)


def test_protocol_doc_describes_the_late_and_unregistered_overrun_report():
    row = next(ln for ln in DOC.read_text().splitlines() if ln.startswith("| `job_end` |"))
    for word in (
        "`project`",
        "`label`",
        "`scope empty`",
        "once per unit",
        "`job_start` failed",
        "in memory only",
        "`owner_denied`",
        "trailing `-<pid>`",
    ):
        assert word in row


def test_protocol_doc_lists_every_job_ending_cause_and_the_new_view():
    from loadgov.engines.endings import CAUSES

    text = DOC.read_text()
    section = text.split("### Job endings", 1)[1].split("### Job usage", 1)[0]
    listed = {
        c
        for ln in section.splitlines()
        for c in re.findall(r"`([a-z_]+)`", ln.split("|")[1] if ln.startswith("|") else "")
    }
    assert set(CAUSES) <= listed
    for word in (
        "`exit_code`",
        "`signal`",
        "`job_endings`",
        "loadctl jobs --ended",
        "precedence",
        "`request.unit`",
    ):
        assert word in section
    assert any(ln.startswith("| `job_endings` |") for ln in text.splitlines())
