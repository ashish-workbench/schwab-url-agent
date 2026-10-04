from orchestrator import codegen, policy, reasoning
from orchestrator.scenarios import V1_SPEC


def _write(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def rules(found):
    return {v.rule for v in found}


def test_clean_generated_code_passes_all_rules(tmp_path):
    codegen.write_files(tmp_path, codegen.render({"expiry": True, "default_ttl_seconds": 60}, codegen.APP_FILES + codegen.TEST_FILES))
    assert policy.scan(tmp_path) == []


def test_detects_secrets_dangerous_calls_pii_and_destructive_sql(tmp_path):
    _write(
        tmp_path,
        {
            "app/settings.py": 'API_KEY = "abcdef123456"\n',
            "app/run.py": "import os\nos.system('ls')\neval('1')\n",
            "app/schema.py": 'S = """\nCREATE TABLE clicks (\n    code TEXT,\n    ip_address TEXT\n)\n"""\n',
            "app/cleanup.py": 'Q = "DROP TABLE links"\n',
        },
    )
    assert rules(policy.scan(tmp_path)) >= {"SEC-001", "SEC-002", "CMP-001", "CHG-002"}


def test_redirect_guard_requires_validation(tmp_path):
    _write(tmp_path, {"app/store.py": "def validate_url(u):\n    return u\n\ndef create_link(conn, url):\n    return url\n"})
    assert "SEC-003" in rules(policy.scan(tmp_path))


def test_change_control_flags_out_of_scope_files(tmp_path):
    _write(tmp_path, {"app/extra.py": "x = 1\n"})
    found = policy.scan(tmp_path, files=["app/extra.py"], allowed=["app/store.py"])
    assert rules(found) == {"CHG-001"}


def test_reasoning_detects_features_and_flows(tmp_path):
    codegen.write_files(tmp_path, codegen.render(V1_SPEC, codegen.APP_FILES + codegen.TEST_FILES))
    index = reasoning.index_codebase(tmp_path)
    assert reasoning.detect_features(index) == {"create", "redirect", "analytics"}
    assert "GET /{code} -> follow -> record_click -> links" in reasoning.data_flows(index)
    impact = reasoning.impact_analysis(index, {"expiry"})
    assert {i["kind"] for i in impact} == {"table", "function", "route", "tests"}


def test_template_engine_rejects_unknown_flags():
    import pytest

    with pytest.raises(KeyError):
        codegen.render_text("# @if nope\nx\n# @endif\n", {"expiry": True}, {})
