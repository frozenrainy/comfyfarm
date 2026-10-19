"""Secrets from files: `NAME_FILE=/run/secrets/x` instead of `NAME=value` — the convention of Docker's official images.
A value in the environment shows in `docker inspect` and in every child process; a file mounted as a compose secret
does not."""
from __future__ import annotations

import os

SECRET_NAMES = ("PGPASSWORD", "COMFYFARM_GATEWAY_SECRET", "COMFYFARM_SESSION_KEY")


def env_secret(name: str, env=None) -> str | None:
    """The value of `name`, read from the file `name_FILE` names when that is set. Both set is refused: which one
    wins would be a guess."""
    env = os.environ if env is None else env
    path = env.get(name + "_FILE")
    if not path:
        return env.get(name) or None
    if env.get(name):
        raise SystemExit(f"{name} and {name}_FILE are both set — keep one")
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
    except OSError as e:
        raise SystemExit(f"{name}_FILE: cannot read {path} ({e.strerror})") from None
    if not value:
        raise SystemExit(f"{name}_FILE: {path} is empty")
    return value


def load_secret_files(env=None) -> None:
    """Resolve every `*_FILE` secret into this process's environment once, at start: the database driver and the
    PostgreSQL client tools read PGPASSWORD from there."""
    env = os.environ if env is None else env
    for name in SECRET_NAMES:
        if env.get(name + "_FILE"):
            env[name] = env_secret(name, env)
            del env[name + "_FILE"]
