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
    with pytest.raises(SystemExit):
        cli.settings_from_env({})


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
    main, err = (tmp_path / "comfyfarm.log").read_text(), (tmp_path / "error.log").read_text()
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
