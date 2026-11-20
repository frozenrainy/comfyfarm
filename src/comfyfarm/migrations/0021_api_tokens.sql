-- 0021: personal API tokens for the command line and programs (docs/design/19, D-57). Only the SHA-256 is kept.
CREATE TABLE api_tokens (
  id            bigserial PRIMARY KEY,
  username      text NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  name          text NOT NULL,
  scope         text NOT NULL CHECK (scope IN ('read', 'user', 'admin')),
  token_hash    text NOT NULL UNIQUE,
  hint          text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  last_used_at  timestamptz,
  revoked_at    timestamptz,
  revoked_by    text
);
CREATE INDEX api_tokens_user ON api_tokens(username) WHERE revoked_at IS NULL;
