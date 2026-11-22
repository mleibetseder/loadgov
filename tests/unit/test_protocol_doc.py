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
