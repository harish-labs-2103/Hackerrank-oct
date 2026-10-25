"""Connector layer: discovery, mapping approval gate, live audit, coverage gating, read-only,
search connector over a mock transport. All data is fictional."""
import json
import sqlite3

import httpx
import pytest

from davestigator.config import ConnectorCfg, LiveCfg, Settings
from davestigator.connectors import CredentialsMissing, open_connector
from davestigator.connectors.discovery import analyze, draft_collection
from davestigator.connectors.live import LiveAuditor, LiveRun, live_request, load_rules
from davestigator.connectors.mapping import ConnectorMapping, MappingFile, approval_problems
from davestigator.connectors.nosql import SearchConnector
from davestigator.connectors.sql import SQLConnector


@pytest.fixture()
def shop_db(tmp_path):
    p = tmp_path / "shop.db"
    c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT, phone TEXT, full_name TEXT, created_at TEXT);
    CREATE TABLE email_history(id INTEGER PRIMARY KEY, user_id INTEGER, old_email TEXT, changed_at TEXT);
    CREATE TABLE search_documents(id TEXT PRIMARY KEY, email TEXT, display_name TEXT);
    CREATE TABLE support_tickets(id INTEGER PRIMARY KEY, customer_id INTEGER, body TEXT);
    CREATE TABLE integration_connectors(id INTEGER PRIMARY KEY, subject_selector TEXT, enabled INTEGER);
    CREATE TABLE security_audit_events(id INTEGER PRIMARY KEY, subject_reference INTEGER, event_code TEXT);
    """)
    c.executemany("INSERT INTO users VALUES(?,?,?,?,?)", [(1, "ana@example.org", "+49 151 1111 2222", "Ana B", "2024-01-01"),
                                                         (2, "ana.jr@example.org", "+49 151 3333 4444", "Ana C", "2024-02-01")])
    c.execute("INSERT INTO email_history VALUES(1, 1, 'ana.old@example.net', '2025-01-01')")
    c.execute("INSERT INTO search_documents VALUES('d1', 'ANA.OLD@EXAMPLE.NET', 'Ana B')")
    c.execute("INSERT INTO search_documents VALUES('d2', 'ana.jr@example.org', 'Ana C')")
    c.execute("INSERT INTO support_tickets VALUES(10, 1, 'Reply to ana@example.org about order 7')")
    c.execute("INSERT INTO integration_connectors VALUES(5, 'ana@example.org', 0)")
    c.execute("INSERT INTO security_audit_events VALUES(3, 1, 'ACCOUNT_DELETION_RECORDED')")
    c.commit()
    c.close()
    return p


def _approved_mapping(conn):
    findings = [analyze(conn, i) for i in conn.discover()]
    cols = [c for c in (draft_collection(f) for f in findings) if c]
    for c in cols:
        c.review = "approved"
    return MappingFile(connectors={"shop": ConnectorMapping(collections=cols)})


def _settings(monkeypatch, db):
    monkeypatch.setenv("SHOP_URL", f"sqlite:///{db}")
    s = Settings()
    s.connectors = [ConnectorCfg(name="shop", kind="sql", url_env="SHOP_URL")]
    s.live = LiveCfg()
    return s


def test_sql_connector_is_read_only(shop_db):
    conn = SQLConnector("shop", f"sqlite:///{shop_db}")
    assert conn.test().ok
    with pytest.raises(Exception):
        with conn.engine.connect() as c:
            c.exec_driver_sql("DELETE FROM users")
    conn.close()
    assert sqlite3.connect(shop_db).execute("SELECT count(*) FROM users").fetchone()[0] == 2


def test_discovery_detects_pii_and_ignores_dates(shop_db):
    conn = SQLConnector("shop", f"sqlite:///{shop_db}")
    fs = {f.info.source: f for f in (analyze(conn, i) for i in conn.discover())}
    users = {c.column: c.kind for c in fs["users"].columns}
    assert users.get("email") == "email" and users.get("phone") == "phone" and "created_at" not in users
    assert "body" in fs["support_tickets"].text_fields
    d = draft_collection(fs["users"])
    assert d.role == "identity" and d.identifiers["user_ref"] == "id" and d.review == "pending"
    conn.close()


def test_approval_gate(shop_db):
    conn = SQLConnector("shop", f"sqlite:///{shop_db}")
    m = _approved_mapping(conn)
    m.connectors["shop"].collections[0].review = "pending"
    probs = approval_problems(m, "shop", {"USER_PROFILE", "SEARCH_INDEX_ENTRY", "SUPPORT_MESSAGE", "SECURITY_AUDIT_LOG", "DATA_FLOW"})
    assert any("pending" in p for p in probs)
    conn.close()


def test_credentials_only_from_env(monkeypatch):
    monkeypatch.delenv("NOPE_URL", raising=False)
    with pytest.raises(CredentialsMissing):
        open_connector(ConnectorCfg(name="x", kind="sql", url_env="NOPE_URL"))


def test_live_audit_end_to_end(shop_db, monkeypatch, dataset_root):
    s = _settings(monkeypatch, shop_db)
    m = _approved_mapping(SQLConnector("shop", f"sqlite:///{shop_db}"))
    aud = LiveAuditor(s, m, "sha", load_rules(dataset_root / "deletion_rules.csv"))
    run = LiveRun("t", None)
    r, cov = aud.audit(live_request("ana@example.org", "Please delete my account."), run)
    aud.close()
    found = {(x.operation, x.resource_id) for x in r.steps}
    assert r.deletion_status == "INCOMPLETE"
    assert ("REPORT_REMAINING_RECORD", "shop:users:1") in found
    assert ("REPORT_REMAINING_RECORD", "shop:search_documents:d1") in found  # via old email (history), case-insensitive
    assert ("REPORT_EXPOSED_PII", "shop:support_tickets:10") in found  # email inside free text
    assert ("VERIFY_DATA_FLOW_DISABLED", "shop:integration_connectors:5") in found
    assert not any("d2" in rid or "users:2" in rid for _, rid in found), "look-alike subject must not be linked"
    assert all(c.status == "FULL" for c in cov)
    from davestigator.normalize import EMAIL_RE

    assert not any(EMAIL_RE.search(json.dumps(j)) for j in run.journal), "journal holds no identifier values"


def test_coverage_gap_blocks_complete(shop_db, monkeypatch, dataset_root):
    s = _settings(monkeypatch, shop_db)
    s.connectors[0].options = {"row_limit": 0}
    m = _approved_mapping(SQLConnector("shop", f"sqlite:///{shop_db}"))
    aud = LiveAuditor(s, m, "sha", load_rules(dataset_root / "deletion_rules.csv"))
    r, cov = aud.audit(live_request("ana@example.org", "Please delete my account."), LiveRun("t", None))
    aud.close()
    assert any(c.status == "PARTIAL" for c in cov)
    assert r.deletion_status == "INCOMPLETE" and any("PARTIAL" in g for g in r.gaps)


def test_search_connector_exact_terms_only():
    seen = {}

    def handler(request: httpx.Request):
        if request.url.path.endswith("/_search"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"hits": {"hits": [{"_id": "x1", "_source": {"email": "ana@example.org"}}]}})
        return httpx.Response(200, json={"version": {"number": "8.14.0"}})

    from davestigator.connectors.mapping import CollectionMap

    sc = SearchConnector("es", "http://es.local", transport=httpx.MockTransport(handler))
    cm = CollectionMap(source="customers", system="search_service", category="SEARCH_INDEX_ENTRY", id="_id", identifiers={"email": "email"})
    recs, cov = sc.find(cm, {"email": {"ana@example.org"}})
    assert cov.status == "FULL" and recs[0].record_id == "x1"
    queries = seen["body"]["query"]["bool"]["should"]
    assert all("term" in q for q in queries), "only exact term queries, never full-text match"


def test_cli_refuses_url_on_command_line(tmp_path):
    from typer.testing import CliRunner

    from davestigator.cli import app

    cfg = tmp_path / "c.yaml"
    r = CliRunner().invoke(app, ["--no-color", "--config", str(cfg), "connect", "add", "--name", "x", "--kind", "sql",
                                 "--url-env", "X", "--url", "postgres://u:p@h/db"])
    assert r.exit_code == 1 and not cfg.exists()


def test_snapshot_roundtrip_offline_matches_live(shop_db, monkeypatch, dataset_root, tmp_path):
    from davestigator.app import open_session, requests_for
    from davestigator.connectors.live import export_snapshot

    s = _settings(monkeypatch, shop_db)
    m = _approved_mapping(SQLConnector("shop", f"sqlite:///{shop_db}"))
    aud = LiveAuditor(s, m, "sha", load_rules(dataset_root / "deletion_rules.csv"))
    q = live_request("ana@example.org", "Please delete my account.", case_id="c1")
    live_r, _ = aud.audit(q, LiveRun("t", None))
    export_snapshot(aud, [q], tmp_path / "snap", LiveRun("t2", None))
    aud.close()
    off = Settings()
    off.dataset.root = str(tmp_path / "snap")
    sess = open_session(off, use_llm=False)
    off_r = sess.investigator.investigate(requests_for(sess)[0])
    key = lambda r: sorted((x.operation, x.resource_id) for x in r.steps)
    assert off_r.deletion_status == live_r.deletion_status == "INCOMPLETE"
    assert key(off_r) == key(live_r)
