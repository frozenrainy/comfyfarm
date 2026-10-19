"""Worker setup script (docs/design/14) on a fake host: every path under a root prefix, commands recorded."""
import base64
import json
import os

import pytest

from comfyfarm import worker_setup as ws

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHk1c2V0dXBrZXlmb3J0ZXN0aW5nb25seTEyMzQ1Ng comfyfarm-router"


def fake_root(tmp_path, sshd=True, nfs=True):
    root = tmp_path / "root"
    (root / "run" / "systemd" / "system").mkdir(parents=True)
    (root / "etc" / "ssh").mkdir(parents=True)
    (root / "etc" / "ssh" / "ssh_host_ed25519_key.pub").write_text("ssh-ed25519 AAAAhostkey root@gpu-1\n")
    (root / "etc" / "fstab").write_text("UUID=abc / ext4 defaults 0 1\n")
    (root / "usr" / "sbin").mkdir(parents=True)
    if sshd:
        (root / "usr" / "sbin" / "sshd").write_text("")
    if nfs:
        (root / "sbin").mkdir()
        (root / "sbin" / "mount.nfs").write_text("")
    return root


def payload(**spec):
    base = {"instance": "gpu-1", "account": "comfyfarm", "router_url": "http://10.0.0.5:8188", "art": "/srv/art",
            "local": "/var/lib/comfyfarm/local", "gpu": "amd", "port": 8188, "listen": "0.0.0.0",
            "paths": {"models": "/srv/models", "input": "/mnt/farm/input", "output": "/mnt/farm/output",
                      "user": "/mnt/farm/user", "temp": "/var/lib/comfyfarm/temp/gpu-1"},
            "mounts": [{"source": "nas:/export/farm", "target": "/mnt/farm"}], "agent_pubkey": KEY}
    base.update(spec)
    files = {n: base64.b64encode(f"# {n}\n".encode()).decode() for n in ("launcher.py", "agent.py", "hostcheck.py")}
    return {"spec": base, "files": files, "version": "0.9.0", "report_url": "http://10.0.0.5:8188/api/worker_setup/t/report"}


class Recorder:
    def __init__(self, check_json=None):
        self.calls = []
        self.check_json = check_json or [{"level": "OK", "name": "GPU (AMD)", "detail": "1 GPU(s): gfx1201"}]

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "mountpoint":
            return 1, ""                                         # not mounted yet
        if "hostcheck.py" in " ".join(argv):
            return 0, json.dumps(self.check_json)
        return 0, ""

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def test_setup_builds_a_worker_and_reports(tmp_path):
    root, rec = fake_root(tmp_path), Recorder()
    res = ws.setup(payload(), ws.Host(root=str(root), run=rec))
    assert res["ok"], res
    assert [s["step"] for s in res["steps"]] == ["preconditions", "account", "worker tools", "configuration", "mounts",
                                                 "directories", "router access", "user manager", "builds"]
    assert res["host_keys"] == ["ssh-ed25519 AAAAhostkey"]
    # account with a real shell (sshd runs the forced command through it)
    assert rec.ran("useradd")[0][-3:] == ["--shell", "/bin/sh", "comfyfarm"] or "/bin/sh" in rec.ran("useradd")[0]
    # root-owned tools and configuration, nothing secret in /etc
    assert (root / "opt/comfyfarm/worker/launcher.py").read_text() == "# launcher.py\n"
    env = (root / "etc/comfyfarm/gpu-1.env").read_text()
    assert "COMFY_LAUNCH_ROUTER=http://10.0.0.5:8188" in env and "COMFY_LAUNCH_ARTIFACTS=/srv/art" in env
    assert "COMFY_LAUNCH_PREFIXES=/mnt/farm/input,/mnt/farm/output,/mnt/farm/user,/srv/models,/var/lib/comfyfarm/temp/gpu-1" in env
    assert json.loads((root / "etc/comfyfarm/gpu-1.check.json").read_text())["gpu"] == "amd"
    assert "ExecStart=/usr/bin/python3 /opt/comfyfarm/worker/launcher.py" in (root / "etc/systemd/user/comfyfarm@.service").read_text()
    # the forced command, every forwarding off, in a file the account cannot edit
    ak = (root / "var/lib/comfyfarm/.ssh/authorized_keys").read_text()
    assert 'command="/usr/bin/python3 /opt/comfyfarm/worker/agent.py ssh"' in ak and "no-pty" in ak and KEY in ak
    assert oct(os.stat(root / "var/lib/comfyfarm/.ssh/authorized_keys").st_mode & 0o777) == "0o644"
    # NFS: fstab entry, mounted now
    assert "nas:/export/farm /mnt/farm nfs defaults,_netdev,nofail 0 0" in (root / "etc/fstab").read_text()
    assert rec.ran("mount", "/mnt/farm")
    # lingering and the unit enabled, not started (nothing is assigned yet)
    assert rec.ran("loginctl", "enable-linger", "comfyfarm")
    enable = [c for c in rec.calls if "enable" in c and "comfyfarm@gpu-1" in c]
    assert enable and not [c for c in rec.calls if "start" in c and "comfyfarm@gpu-1" in c]
    assert res["check"][0]["name"] == "GPU (AMD)"


def test_setup_runs_again_without_changes_and_adds_instances(tmp_path):
    root = fake_root(tmp_path)
    ws.setup(payload(), ws.Host(root=str(root), run=Recorder()))
    again = ws.Host(root=str(root), run=Recorder())
    ws.setup(payload(), again)
    assert not [m for m in again.done if m.startswith(("wrote", "created"))], again.done
    assert (root / "etc/fstab").read_text().count("nas:/export/farm") == 1
    ws.setup(payload(instance="gpu-2", port=8189), ws.Host(root=str(root), run=Recorder()))
    assert "COMFYFARM_AGENT_INSTANCES=gpu-1,gpu-2" in (root / "etc/comfyfarm/agent.env").read_text()


def test_setup_says_what_is_missing(tmp_path):
    res = ws.setup(payload(), ws.Host(root=str(fake_root(tmp_path, sshd=False)), run=Recorder()))
    assert not res["ok"] and res["steps"][-1]["step"] == "preconditions" and "openssh-server" in res["error"]
    res = ws.setup(payload(), ws.Host(root=str(fake_root(tmp_path / "b", nfs=False)), run=Recorder()))
    assert not res["ok"] and res["steps"][-1]["step"] == "mounts" and "nfs-common" in res["error"]
    res = ws.setup(payload(agent_pubkey="ssh-rsa AAAA x"), ws.Host(root=str(fake_root(tmp_path / "c")), run=Recorder()))
    assert not res["ok"] and "ed25519" in res["error"]
    res = ws.setup(payload(account="root"), ws.Host(root=str(fake_root(tmp_path / "d")), run=Recorder()))
    assert not res["ok"] and res["steps"][-1]["step"] == "parameters"
    with pytest.raises(ws.Failed):
        ws.validate(payload(art="relative/art")["spec"])


def test_a_failing_host_check_is_reported_not_hidden(tmp_path):
    rec = Recorder([{"level": "FAIL", "name": "GPU", "detail": "setup expects an AMD GPU and none is usable here"}])
    res = ws.setup(payload(), ws.Host(root=str(fake_root(tmp_path)), run=rec))
    assert not res["ok"] and res["check"][0]["level"] == "FAIL" and "error" not in res


def test_dry_run_changes_nothing(tmp_path):
    root = fake_root(tmp_path)
    before = sorted(str(p) for p in root.rglob("*"))
    h = ws.Host(root=str(root), dry=True)
    ws.setup(payload(), h)
    assert sorted(str(p) for p in root.rglob("*")) == before
    assert any(m.startswith("would write /opt/comfyfarm/worker/launcher.py") for m in h.done)
    assert any("would run: loginctl enable-linger comfyfarm" in m for m in h.done)


def test_without_parameters_it_refuses(capsys):
    assert ws.main([]) == 2 and "download it from the dashboard" in capsys.readouterr().err


def test_new_groups_restart_the_user_manager(tmp_path, monkeypatch):
    """usermod -aG does not reach a running user manager: it must restart to open /dev/kfd."""
    import grp
    monkeypatch.setattr(grp, "getgrnam", lambda g: grp.struct_group((g, "x", 991, [])))
    rec = Recorder()
    ws.setup(payload(), ws.Host(root=str(fake_root(tmp_path)), run=rec))
    assert rec.ran("usermod", "-aG", "render", "comfyfarm") and rec.ran("systemctl", "restart", "user@0.service")


def test_directories_come_after_the_mounts_and_the_uid_is_fixed(tmp_path):
    """Rehearsal: the instance directories were made before NFS was mounted (and hidden under it); and shared storage
    needs one uid on every host."""
    h = ws.Host(root=str(fake_root(tmp_path)), run=Recorder())
    ws.setup(payload(uid=47800), h)
    order = [m for m in h.done if m in ("mounted nas:/export/farm on /mnt/farm", "created /mnt/farm/input")]
    assert order == ["mounted nas:/export/farm on /mnt/farm", "created /mnt/farm/input"]
    rec = Recorder()
    ws.setup(payload(uid=47800), ws.Host(root=str(fake_root(tmp_path / "b")), run=rec))
    assert rec.ran("useradd")[0][-2:] == ["--uid", "47800"]


def test_an_existing_account_with_another_uid_is_refused(tmp_path, monkeypatch):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", 998, 998, "", "/var/lib/comfyfarm", "/bin/sh")))
    res = ws.setup(payload(uid=47800), ws.Host(root=str(fake_root(tmp_path)), run=Recorder()))
    assert not res["ok"] and "uid 998" in res["error"] and "47800" in res["error"]


def test_a_mount_point_the_site_already_mounted_is_left_alone(tmp_path):
    """Doc-only rehearsal: a mount line carried over from another host's setup went into the fstab of a host whose site
    had mounted the share there already — after a reboot two mounts would stack."""
    class Mounted(Recorder):
        def __call__(self, argv):
            self.calls.append(argv)
            if argv[0] == "mountpoint":
                return 0, ""                                     # something is mounted there
            return super().__call__(argv) if "hostcheck.py" in " ".join(argv) else (0, "")
    root, rec = fake_root(tmp_path), Mounted()
    h = ws.Host(root=str(root), run=rec)
    res = ws.setup(payload(), h)
    assert res["ok"] and "nas:/export/farm" not in (root / "etc/fstab").read_text()
    assert not rec.ran("mount", "/mnt/farm") and any("already mounted" in m for m in h.done)


def test_a_persons_login_account_is_refused_before_anything_changes(tmp_path, monkeypatch):
    """Field report: the operator's own login account typed as the service account. Setup would have replaced that
    account's authorized_keys with the router's forced-command key (locking the person out) and given the router a
    sudo-capable identity. Refused — by home, and by sudo group — before the first change."""
    import grp
    import pwd
    root = fake_root(tmp_path)
    (root / "etc" / "shadow").write_text("root:*:1::::::\nalice:$6$salt$hash:19000:0:99999:7:::\ncomfyfarm:!:19000::::::\n")
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", 1000, 1000, "", "/home/alice", "/bin/bash")))
    rec = Recorder()
    res = ws.setup(payload(account="alice", uid=None), ws.Host(root=str(root), run=rec))
    assert not res["ok"] and "password" in res["error"] and "comfyfarm" in res["error"]
    assert not rec.ran("useradd") and not rec.ran("usermod") and not (root / "home").exists() and not (root / "var").exists()
    # a person without a password but with keys of their own: refused before anything changes, too
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", 1001, 1001, "", "/home/bob", "/bin/bash")))
    keys = root / "home" / "bob" / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True)
    keys.write_text("ssh-ed25519 AAAAbobsown bob@laptop\n")
    rec = Recorder()
    res = ws.setup(payload(account="bob", uid=None), ws.Host(root=str(root), run=rec))
    assert not res["ok"] and "did not write" in res["error"] and not rec.ran("usermod")
    assert keys.read_text() == "ssh-ed25519 AAAAbobsown bob@laptop\n" and not (root / "home" / "bob" / "logs").exists()

    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", 47800, 47800, "", "/var/lib/ops", "/bin/sh")))
    real_getgrnam = grp.getgrnam
    monkeypatch.setattr(grp, "getgrnam", lambda g: grp.struct_group(("sudo", "x", 27, ["ops"])) if g == "sudo" else real_getgrnam(g))
    res = ws.setup(payload(account="ops", uid=47800), ws.Host(root=str(fake_root(tmp_path / "b")), run=Recorder()))
    assert not res["ok"] and "sudo" in res["error"]


def test_a_service_account_made_by_hand_under_home_is_adopted(tmp_path, monkeypatch):
    """`useradd -m comfyfarm` or a manual install (rollouts.md §2) puts the account under /home with a locked
    password: that is a service account, wherever its home is."""
    import pwd
    root = fake_root(tmp_path)
    (root / "etc" / "shadow").write_text("comfyfarm:!:19000::::::\n")
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", 47800, 47800, "", "/home/comfyfarm", "/bin/sh")))
    res = ws.setup(payload(uid=47800), ws.Host(root=str(root), run=Recorder()))
    assert res["ok"], res
    assert (root / "home" / "comfyfarm" / ".ssh" / "authorized_keys").read_text().startswith(ws.MARK)


def test_an_existing_service_account_keeps_its_own_home(tmp_path, monkeypatch):
    import pwd
    root = fake_root(tmp_path)
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", 47800, 47800, "", "/srv/cf", "/bin/sh")))
    res = ws.setup(payload(uid=47800), ws.Host(root=str(root), run=Recorder()))
    assert res["ok"], res
    assert (root / "srv" / "cf" / ".ssh" / "authorized_keys").read_text().startswith(ws.MARK)   # where sshd looks
    assert not (root / "var" / "lib" / "comfyfarm" / ".ssh").exists()


def test_keys_setup_did_not_write_are_never_replaced(tmp_path):
    root = fake_root(tmp_path)
    keys = root / "var" / "lib" / "comfyfarm" / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True)
    keys.write_text("ssh-ed25519 AAAAsomeonesownkey me@laptop\n")
    res = ws.setup(payload(), ws.Host(root=str(root), run=Recorder()))
    assert not res["ok"] and "did not write" in res["error"]
    assert keys.read_text() == "ssh-ed25519 AAAAsomeonesownkey me@laptop\n"


def test_a_unit_in_the_accounts_home_that_would_shadow_setups_is_named(tmp_path):
    """Field report: an older manual install's ~/.config/systemd/user/comfyfarm@.service won over /etc/systemd/user."""
    root = fake_root(tmp_path)
    own = root / "var" / "lib" / "comfyfarm" / ".config" / "systemd" / "user" / "comfyfarm@.service"
    own.parent.mkdir(parents=True)
    own.write_text("[Service]\n")
    res = ws.setup(payload(), ws.Host(root=str(root), run=Recorder()))
    assert not res["ok"] and "take the place" in res["error"]
