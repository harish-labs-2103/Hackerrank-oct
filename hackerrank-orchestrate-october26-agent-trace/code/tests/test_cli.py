"""CLI contract tests (deterministic: every invocation uses --no-llm)."""
import csv
import json
import re

from typer.testing import CliRunner

from davestigator.cli import app

runner = CliRunner()
ANSI = re.compile(r"\x1b\[")


def invoke(*args):
    return runner.invoke(app, ["--no-llm", "--no-color", *args], env={"NO_COLOR": "1"})


def test_version_and_welcome():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and "Davestigator" in r.stdout
    r = invoke()
    assert r.exit_code == 0 and "DAVESTIGATOR" in r.stdout and "Read-only" in r.stdout


def test_usage_error_exit_1():
    assert invoke("audit").exit_code == 1  # neither --case nor --all


def test_unknown_case_exit_2():
    r = invoke("audit", "--case", "case_999999")
    assert r.exit_code == 2


def test_apply_refuses_without_dry_run():
    assert invoke("apply", "--case", "case_09").exit_code == 4


def test_audit_case_json_and_masking(dataset_root):
    r = invoke("--format", "json", "audit", "--case", "case_24")
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)
    assert data["deletion_status"] == "INCOMPLETE"
    raw_email = [row for row in csv.DictReader(open(dataset_root / "sample_deletion_requests.csv")) if row["case_id"] == "case_24"][0]["email"]
    assert raw_email not in r.stdout, "requester email must be masked"


def test_plan_matches_output_row():
    r = invoke("--format", "json", "plan", "--case", "case_11")
    steps = json.loads(r.stdout)
    gold = [row for row in csv.DictReader(open(__import__("pathlib").Path(__file__).resolve().parents[2] / "dataset" / "sample_output.csv"))
            if row["case_id"] == "case_11"][0]
    assert [(s["operation"], s["resource_id"]) for s in steps] == [(s["operation"], s["resource_id"]) for s in json.loads(gold["steps_json"])]


def test_no_ansi_with_no_color():
    r = invoke("findings", "--case", "case_32")
    assert r.exit_code == 0 and not ANSI.search(r.stdout)


def test_audit_all_sample_writes_valid_output(tmp_path):
    out = tmp_path / "sample_out.csv"
    r = invoke("-q", "audit", "--all", "--sample", "--output", str(out))
    assert r.exit_code == 0, r.stdout
    rows = list(csv.DictReader(open(out)))
    assert len(rows) == 32 and [x["case_id"] for x in rows] == [f"case_{i:02d}" for i in range(1, 33)]
    v = invoke("validate-output", "--sample", "--file", str(out))
    assert v.exit_code == 0 and "PASSED" in v.stdout
