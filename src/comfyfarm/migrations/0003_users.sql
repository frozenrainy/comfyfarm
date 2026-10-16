-- 0003: local identity provider (docs/design/08). Password hashes are scrypt; roles e.g. {admin}.
CREATE TABLE users (
  username       text PRIMARY KEY,
  password_hash  text NOT NULL,
  roles          text[] NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now(),
  disabled_at    timestamptz
);
