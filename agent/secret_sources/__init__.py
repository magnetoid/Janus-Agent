"""External secret source integrations.

A secret source is anything that can supply environment-variable-shaped
credentials at process startup, _after_ ~/.janus/.env has loaded.  By
default sources are non-destructive: they only set values for env vars
that aren't already present, so .env and shell exports continue to win.

Currently shipped:

  - ``bitwarden`` — Bitwarden Secrets Manager (`bws` CLI).  See
    ``agent.secret_sources.bitwarden`` for the integration and
    ``janus_cli.secrets_cli`` for the user-facing setup wizard.

Also here, but not an env-var source:

  - ``logins`` — site logins from 1Password (``op``) or Bitwarden (``bw``)
    for the ``browser_fill_credential`` tool. Looked up on demand, never
    loaded into the environment.
"""
