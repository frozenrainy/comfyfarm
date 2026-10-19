"""Command line: serve settings, backup/restore round trip (real pg_dump/pg_restore), doctor, logging."""
import asyncio
import json
import logging
import os
import pathlib

import pytest

from comfyfarm import cli, doctor, logs
from comfyfarm.app import Router
from conftest import add_worker, fast_settings

PG_BIN = str(pathlib.Path(pytest.importorskip("pgserver").__file__).parent / "pginstall" / "bin")


def test_settings_from_env():
    s = cli.settings_from_env({"COMFYFARM_DSN": "postgresql://x", "COMFYFARM_INSTANCE": "r1", "COMFYFARM_DEV_ADMINS": "a, b"})
    assert (s.dsn, s.instance, s.dev_admins, s.gateway_secret) == ("postgresql://x", "r1", ("a", "b"), None)
    assert cli.settings_from_env({"COMFYFARM_DSN": "postgresql://x", "COMFYFARM_KEYS_DIR": "/keys"}).keys_dir == "/keys"
    with pytest.raises(SystemExit):
        cli.settings_from_env({})



def test_secrets_from_files(tmp_path):
    from comfyfarm.envfile import env_secret, load_secret_files
    (tmp_path / "gw").write_text("s3cret-value\n")
    (tmp_path / "pw").write_text("db pass/#\n")
    (tmp_path / "empty").write_text("\n")
    env = {"COMFYFARM_DSN": "postgresql://comfyfarm@db/comfyfarm", "COMFYFARM_GATEWAY_SECRET_FILE": str(tmp_path / "gw"),
           "PGPASSWORD_FILE": str(tmp_path / "pw")}
    load_secret_files(env)
    assert env["PGPASSWORD"] == "db pass/#" and "PGPASSWORD_FILE" not in env          # the driver reads PGPASSWORD
    assert cli.settings_from_env(env).gateway_secret == "s3cret-value"
    assert env_secret("X", {"X": "plain"}) == "plain" and env_secret("X", {}) is None
    for bad in ({"X": "a", "X_FILE": str(tmp_path / "gw")},                           # both: which wins would be a guess
                {"X_FILE": str(tmp_path / "missing")}, {"X_FILE": str(tmp_path / "empty")}):
        with pytest.raises(SystemExit):
            env_secret("X", bad)


def test_secrets_write_keeps_existing_files(tmp_path, capsys):
    (tmp_path / "db_password").write_text("chosen-before\n")
    cli.main(["secrets", "--write", str(tmp_path)])
    assert (tmp_path / "db_password").read_text() == "chosen-before\n"             # Postgres was initialised with it
    for name in ("gateway_secret", "session_key"):
        f = tmp_path / name
        assert len(f.read_text().strip()) >= 32 and f.stat().st_mode & 0o777 == 0o444
    first = (tmp_path / "session_key").read_text()
    capsys.readouterr()
    cli.main(["secrets", "--write", str(tmp_path)])                              # a second run writes nothing
    assert (tmp_path / "session_key").read_text() == first and capsys.readouterr().out == ""

async def test_backup_and_restore_round_trip(dsn, tmp_path):
    r = Router(fast_settings(dsn))
    await r.start(loops=False)
    await cli.add_user(dsn, "keeper", "password-1", admin=True)
    user_dir = tmp_path / "user"
    (user_dir / "keeper" / "workflows").mkdir(parents=True)
    (user_dir / "keeper" / "workflows" / "hero.json").write_text("{}")
    art = tmp_path / "art" / "core" / "c1"
    art.mkdir(parents=True)
    (art / "manifest.json").write_text(json.dumps({"name": "c1", "tree_sha256": "x"}))
    (art / "READY").write_text("")
    dest = cli.backup(dsn, str(tmp_path / "bk"), str(user_dir), str(tmp_path / "art"), PG_BIN)
    files = sorted(os.listdir(dest))
    assert files == ["MANIFEST.json", "README.txt", "artifacts.json", "ledger.dump", "userdata.tar.gz"]
    assert json.load(open(os.path.join(dest, "artifacts.json")))["core"]["c1"]["tree_sha256"] == "x"
    # the site loses its users and workflows…
    async with r.db.conn() as c:
        await c.execute("DELETE FROM users")
    (user_dir / "keeper" / "workflows" / "hero.json").unlink()
    await r.stop()                                             # the router must be stopped for a restore
    cli.restore(dsn, dest, str(user_dir), PG_BIN)
    r2 = Router(fast_settings(dsn))
    await r2.start(loops=False)
    async with r2.db.conn() as c:
        assert await c.fetchval("SELECT count(*) FROM users WHERE username = 'keeper' AND 'admin' = ANY(roles)") == 1
    await r2.stop()
    assert (user_dir / "keeper" / "workflows" / "hero.json").read_text() == "{}"


async def test_doctor_reports_in_plain_words(dsn, fakes):
    r = Router(fast_settings(dsn))
    await r.start(loops=False)
    locked, open_ = await fakes(secured=True, ws_fix=True), await fakes(secured=False, ws_fix=True)
    await add_worker(r, locked.url, name="locked")
    await add_worker(r, open_.url, name="open")
    await add_worker(r, "http://127.0.0.1:9", name="dead", group="g2")
    await r.health.tick()
    await r.stop()
    res = await doctor.run(fast_settings(dsn), pg_bin=PG_BIN)
    by = {t: (st, d) for st, t, d in res}
    assert by["ledger"][0] == "OK" and by["accounts"][0] == "WARN"               # no admin yet
    assert by["worker locked"][0] == "OK" and "secured" in by["worker locked"][1]
    assert by["worker open"][0] == "WARN" and "UNSECURED" in by["worker open"][1]
    assert by["worker dead"][0] == "FAIL" and "not answering" in by["worker dead"][1]
    assert by["group g1"][0] == "OK" and "agree" in by["group g1"][1]
    assert by["shared storage"][0] == "WARN" and "--ui" in by["shared storage"][1]
    assert by["backup tooling"][0] == "OK"
    text = doctor.render(res)
    assert "FAIL worker dead" in text


def test_logging_split(tmp_path):
    logs.setup(str(tmp_path), "INFO")
    log = logging.getLogger("comfyfarm.test")
    log.info("routine")
    log.error("real failure")
    logs.teardown()                                             # closes only the handlers it installed
    main, err = (tmp_path / "router.log").read_text(), (tmp_path / "router-error.log").read_text()
    assert "routine" in main and "real failure" in main
    assert "real failure" in err and "routine" not in err


def fake_tool(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


def test_pg_tools_never_see_the_password_on_their_command_line(tmp_path, capsys):
    from comfyfarm.cli import pg_run
    log = tmp_path / "seen"
    ok = fake_tool(tmp_path, "pg_dump", f'echo "$@" > {log}; echo "$PGPASSWORD" >> {log}')
    pg_run(ok, ["--file", "x"], "postgresql://farm:p%40ss-w0rd@[::1]:5433/comfyfarm?sslmode=disable")
    argv, pw = log.read_text().splitlines()
    assert argv == "--file x --dbname postgresql://farm@[::1]:5433/comfyfarm?sslmode=disable" and pw == "p@ss-w0rd"
    bad = fake_tool(tmp_path, "pg_restore", "exit 3")
    with pytest.raises(SystemExit) as e:
        pg_run(bad, [], "postgresql://farm:hunter2@db/comfyfarm")
    assert "exit 3" in str(e.value) and "hunter2" not in str(e.value)


@pytest.mark.parametrize("client,server,verdict", [(16, 16, "OK"), (17, 16, "WARN"), (15, 16, "FAIL")])
def test_doctor_wants_the_client_tools_of_the_server_version(tmp_path, client, server, verdict):
    from comfyfarm.doctor import backup_tooling
    dump = fake_tool(tmp_path, "pg_dump", f'echo "pg_dump (PostgreSQL) {client}.2 (Debian {client}.2-1)"')
    assert backup_tooling(dump, server)[0] == verdict


def test_migrations_ignore_stray_files():
    from types import SimpleNamespace as F
    from comfyfarm.db import migration_files
    names = ["0002_b.sql", "._0001_init.sql", "0001_init.sql", "0003_c.sql~", "README", "0010_j.sql"]
    assert [f.name for f in migration_files(F(name=n) for n in names)] == ["0001_init.sql", "0002_b.sql", "0010_j.sql"]


async def apply_upto(dsn, n):
    import asyncpg
    import importlib.resources
    from comfyfarm.db import migration_files
    c = await asyncpg.connect(dsn)
    await c.execute("CREATE TABLE schema_migrations (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    for f in migration_files(importlib.resources.files("comfyfarm.migrations").iterdir()):
        v = int(f.name.split("_")[0])
        if v <= n:
            await c.execute(f.read_text())
            await c.execute("INSERT INTO schema_migrations(version) VALUES ($1)", v)
    await c.close()


@pytest.mark.parametrize("taken_at", [5, 6])
async def test_restore_a_backup_from_before_an_upgrade(dsn, tmp_path, taken_at):
    await apply_upto(dsn, taken_at)
    import asyncpg
    c = await asyncpg.connect(dsn)
    await c.execute("INSERT INTO users(username, password_hash, roles) VALUES ('old', 'x', '{admin}')")
    await c.close()
    dest = cli.backup(dsn, str(tmp_path), None, None, PG_BIN)
    r = Router(fast_settings(dsn))
    await r.start(loops=False)                                  # the upgrade: all migrations
    async with r.db.conn() as c:
        await c.execute("INSERT INTO users(username, password_hash) VALUES ('after', 'x')")
    await r.stop()
    cli.restore(dsn, dest, None, PG_BIN)
    r2 = Router(fast_settings(dsn))
    await r2.start(loops=False)                                 # newer migrations applied to the restored ledger
    async with r2.db.conn() as c:
        assert await c.fetchval("SELECT string_agg(username, ',' ORDER BY username) FROM users") == "old"
        assert await c.fetchval("SELECT max(version) FROM schema_migrations") >= 8
    await r2.stop()


async def test_failed_restore_leaves_the_live_ledger(dsn, tmp_path):
    r = Router(fast_settings(dsn))
    await r.start(loops=False)
    await cli.add_user(dsn, "keeper", "password-1", admin=True)
    await r.stop()
    broken = tmp_path / "broken"
    broken.mkdir()
    dest = cli.backup(dsn, str(tmp_path), None, None, PG_BIN)
    (broken / "ledger.dump").write_bytes(open(os.path.join(dest, "ledger.dump"), "rb").read()[:3000])
    with pytest.raises(SystemExit):
        cli.restore(dsn, str(broken), None, PG_BIN)
    r2 = Router(fast_settings(dsn))
    await r2.start(loops=False)
    async with r2.db.conn() as c:
        assert await c.fetchval("SELECT count(*) FROM users WHERE username = 'keeper'") == 1
    await r2.stop()


def test_user_dir_restore_replaces_exactly_and_touches_nothing_else(tmp_path):
    import tarfile
    src = tmp_path / "snap" / "user"
    (src / "alice").mkdir(parents=True)
    (src / "alice" / "a.json").write_text("old")
    tar = tmp_path / "userdata.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        t.add(src, arcname="user")
    srv = tmp_path / "srv"
    (srv / "user" / "precious").mkdir(parents=True)              # a neighbour that happens to be called "user"
    (srv / "user" / "precious" / "keep.txt").write_text("mine")
    (srv / "ui-user" / "carol").mkdir(parents=True)
    (srv / "ui-user" / "carol" / "new.json").write_text("after the backup")
    cli.restore_user_dir(str(tar), str(srv / "ui-user"))
    assert (srv / "user" / "precious" / "keep.txt").read_text() == "mine"
    assert sorted(os.listdir(srv / "ui-user")) == ["alice"] and (srv / "ui-user" / "alice" / "a.json").read_text() == "old"
    kept = [n for n in os.listdir(srv) if n.startswith("ui-user.before-restore-")]
    assert len(kept) == 1 and (srv / kept[0] / "carol" / "new.json").exists()
    assert not [n for n in os.listdir(srv) if n.startswith(".comfyfarm-restore-")]


async def test_add_user_checks_the_password_and_keeps_the_role(dsn):
    r = Router(fast_settings(dsn))
    await r.start(loops=False)
    with pytest.raises(SystemExit):
        await cli.add_user(dsn, "root", "", admin=True)
    await cli.add_user(dsn, "root", "password-1", admin=True)
    await cli.add_user(dsn, "root", "password-2", admin=None)     # a password reset
    async with r.db.conn() as c:
        assert await c.fetchval("SELECT roles FROM users WHERE username = 'root'") == ["admin"]
    await cli.add_user(dsn, "root", "password-3", admin=False)
    async with r.db.conn() as c:
        assert await c.fetchval("SELECT roles FROM users WHERE username = 'root'") == []
    await r.stop()


async def test_doctor_on_an_empty_database_says_what_to_do(dsn):
    results = await doctor.run(fast_settings(dsn))
    assert results[0][0] == "FAIL" and "start the router once" in results[0][2]


def test_prepare_makes_the_easy_state_once(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    cli.main(["prepare", str(state)])
    assert {p.name: oct(p.stat().st_mode & 0o777) for p in state.iterdir()} == \
        {"secrets": "0o711", "keys": "0o700", "backups": "0o700"}       # Postgres reads its file by name, lists nothing
    secrets = {p.name: p.read_text() for p in (state / "secrets").iterdir()}
    assert set(secrets) == {"db_password", "gateway_secret", "session_key"}
    capsys.readouterr()
    cli.main(["prepare", str(state)])                                    # every later `docker compose up`
    assert {p.name: p.read_text() for p in (state / "secrets").iterdir()} == secrets
    assert "nothing changed" in capsys.readouterr().out
