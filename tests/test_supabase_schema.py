"""Offline checks of the Supabase schema and RLS policies. No Supabase project and no network needed.

These tests verify what the migration SQL DECLARES (structure, policies, grants, consistency with the app's
models). They cannot prove that a live database enforces it: for that run the opt-in two-user test in
tests/integration/test_supabase_rls.py against a real project (see supabase/README.md).
"""

import base64
import dataclasses
import json
import re
from pathlib import Path

import pytest

from agent.state import AllergyStatus
from db import database
from db.database import PrivilegedKeyError, is_privileged_key
from services import taxonomy
from services.constraints import Constraints
from services.menu_service import SPICE_LEVELS

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
SQL = "\n".join(path.read_text(encoding="utf-8") for path in MIGRATIONS)
# Constraints fields that belong to a single request, not to the user's durable profile.
PER_REQUEST_FIELDS = {"max_price_sar", "categories", "serving_temperature"}
POLICY_COMMANDS = {"select", "insert", "update", "delete"}


def sql_without_comments() -> str:
    return re.sub(r"--[^\n]*", "", SQL)


# ---- static checks (no extra dependency) ----

def test_there_is_exactly_one_migration_creating_exactly_one_table():
    assert len(MIGRATIONS) == 1
    assert re.findall(r"create\s+table\s+([\w.]+)", sql_without_comments(), re.I) == ["public.user_preferences"]


def test_no_transcript_or_extra_tables_exist():
    assert not re.search(r"message|conversation|transcript|chat", sql_without_comments(), re.I)


def test_the_schema_stores_exactly_the_durable_preference_fields():
    durable = {f.name for f in dataclasses.fields(Constraints)} - PER_REQUEST_FIELDS
    expected = durable | {"allergy_status", "user_id", "created_at", "updated_at"}
    block = re.search(r"create\s+table\s+public\.user_preferences\s*\((.*?)\n\);", SQL, re.S | re.I).group(1)
    columns = set(re.findall(r"^\s{2}([a-z_]+)\s+(?:uuid|text|boolean|timestamptz)", block, re.M))
    assert columns == expected, "a Constraints field was added or removed: decide whether it is durable"


def test_spice_levels_in_sql_match_the_application():
    in_sql = re.search(r"min_spice_level in \(([^)]*)\)", SQL).group(1)
    assert tuple(re.findall(r"'(\w+)'", in_sql)) == SPICE_LEVELS
    assert re.search(r"array\['none', 'mild', 'medium', 'hot'\]", SQL)


def test_persisted_allergy_statuses_match_the_application_minus_the_transient_one():
    in_sql = re.search(r"allergy_status in \(([^)]*)\)", SQL).group(1)
    persisted = {s.value for s in AllergyStatus} - {AllergyStatus.AWAITING_ANSWER.value}
    assert set(re.findall(r"'(\w+)'", in_sql)) == persisted


def test_every_taxonomy_id_passes_the_database_id_check_and_bad_ids_do_not():
    pattern = re.compile(re.search(r"!~ '([^']+)'", SQL).group(1))
    t = taxonomy.get_taxonomy()
    ids = [*t.allergen_names, *t.ingredient_names]
    assert ids and all(pattern.search(i) for i in ids)
    for bad in ("Milk", "حليب", "milk ", "1milk", "tree-nuts", "", "a" * 70):
        assert not pattern.search(bad), bad


def test_nothing_in_the_schema_references_a_privileged_role_or_opens_access():
    sql = sql_without_comments()
    assert "service_role" not in sql
    assert not re.search(r"\bto\s+(anon|public)\b", sql, re.I)
    assert not re.search(r"(using|with\s+check)\s*\(\s*true\s*\)", sql, re.I)


def test_the_integrity_rule_that_none_reported_means_no_allergies_exists():
    assert re.search(r"allergy_status\s*<>\s*'none_reported'", SQL)


def test_no_key_or_token_is_committed_anywhere_in_the_project():
    skip_dirs = {".venv", ".git", "__pycache__", ".claude", "node_modules", ".pytest_cache"}
    suffixes = {".py", ".md", ".toml", ".sql", ".txt", ".json", ".example", ".ini", ".yaml", ".yml"}
    patterns = [re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\."), re.compile(r"sb_secret_[A-Za-z0-9_]{10,}")]
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or skip_dirs & set(path.parts) or path.name == ".env" or path.suffix not in suffixes:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(p.search(text) for p in patterns):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_env_example_has_empty_supabase_values_and_warns_about_privileged_keys():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^SUPABASE_URL=$", text, re.M) and re.search(r"^SUPABASE_KEY=$", text, re.M)
    assert "service_role" in text and "never" in text.lower()


# ---- structural checks of the parsed SQL (needs: pip install -r requirements-dev.txt) ----

@pytest.fixture(scope="module")
def nodes():
    pglast = pytest.importorskip("pglast")
    return [raw.stmt for raw in pglast.parse_sql(SQL)]


@pytest.fixture(scope="module")
def ast():
    return pytest.importorskip("pglast.ast")


def test_the_migration_is_valid_postgresql(nodes):
    assert len(nodes) >= 10


def test_one_row_per_user_primary_key_referencing_auth_users_with_cascade(nodes, ast):
    create = next(n for n in nodes if isinstance(n, ast.CreateStmt))
    assert (create.relation.schemaname, create.relation.relname) == ("public", "user_preferences")
    constraints = [e for e in create.tableElts if isinstance(e, ast.Constraint)]
    primary = next(c for c in constraints if c.contype.name == "CONSTR_PRIMARY")
    assert [k.sval for k in primary.keys] == ["user_id"]
    user_id = next(e for e in create.tableElts if isinstance(e, ast.ColumnDef) and e.colname == "user_id")
    foreign = next(c for c in user_id.constraints if c.contype.name == "CONSTR_FOREIGN")
    assert (foreign.pktable.schemaname, foreign.pktable.relname) == ("auth", "users")
    assert foreign.fk_del_action == "c"


def test_row_level_security_is_enabled_and_forced(nodes, ast):
    actions = {
        cmd.subtype.name
        for n in nodes if isinstance(n, ast.AlterTableStmt) and n.relation.relname == "user_preferences"
        for cmd in n.cmds
    }
    assert {"AT_EnableRowSecurity", "AT_ForceRowSecurity"} <= actions


def test_exactly_one_owner_only_policy_per_operation(nodes, ast):
    from pglast.stream import RawStream

    policies = [n for n in nodes if isinstance(n, ast.CreatePolicyStmt)]
    assert {p.cmd_name for p in policies} == POLICY_COMMANDS and len(policies) == 4
    expected = {"select": (True, False), "insert": (False, True), "update": (True, True), "delete": (True, False)}
    for policy in policies:
        assert [r.rolename for r in policy.roles] == ["authenticated"] and policy.permissive
        assert (policy.qual is not None, policy.with_check is not None) == expected[policy.cmd_name], policy.cmd_name
        for expression in (policy.qual, policy.with_check):
            if expression is not None:
                assert RawStream()(expression) == "(SELECT auth.uid()) = user_id"


def test_grants_are_reset_then_limited_to_the_four_operations_for_authenticated(nodes, ast):
    grants = [n for n in nodes if isinstance(n, ast.GrantStmt)]
    revoked = {r.rolename or "public" for g in grants if not g.is_grant for r in g.grantees}
    assert {"anon", "authenticated", "public"} <= revoked
    granted = [g for g in grants if g.is_grant]
    assert len(granted) == 1
    assert [r.rolename for r in granted[0].grantees] == ["authenticated"]
    assert {p.priv_name for p in granted[0].privileges} == POLICY_COMMANDS


# ---- application guard: never run with a privileged key ----

def fake_jwt(role: str) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part({'role': role})}.signature"


def test_privileged_keys_are_recognised_without_being_logged():
    assert is_privileged_key(fake_jwt("service_role"))
    assert is_privileged_key("sb_secret_" + "x" * 20)
    assert not is_privileged_key(fake_jwt("anon"))
    assert not is_privileged_key("sb_publishable_" + "x" * 20)
    assert not is_privileged_key("") and not is_privileged_key("not.a.jwt") and not is_privileged_key("a.b")


def test_the_client_refuses_a_privileged_key_and_never_creates_it(monkeypatch):
    created = []
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", fake_jwt("service_role"))
    monkeypatch.setattr("supabase.create_client", lambda url, key: created.append(key))
    with pytest.raises(PrivilegedKeyError) as error:
        database.create_supabase_client()
    assert created == [] and fake_jwt("service_role") not in str(error.value)


def test_the_client_accepts_an_anon_key(monkeypatch):
    anon = fake_jwt("anon")
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", anon)
    monkeypatch.setattr("supabase.create_client", lambda url, key: ("client", url, key))
    assert database.create_supabase_client() == ("client", "https://example.invalid", anon)


def test_every_call_creates_a_new_client_so_user_sessions_are_never_shared(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", fake_jwt("anon"))
    monkeypatch.setattr("supabase.create_client", lambda url, key: object())
    assert database.create_supabase_client() is not database.create_supabase_client()
    assert not hasattr(database, "_client")


def test_without_configuration_there_is_no_client(monkeypatch):
    monkeypatch.setattr(database, "is_supabase_configured", lambda: False)
    assert database.create_supabase_client() is None
