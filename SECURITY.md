# Security

Ops Evidence Synthesis is designed around a local-first data boundary.

- Raw logs, raw source files, credential files, and raw environment values must not be uploaded.
- Sanitization and verification run locally before any cloud, model, storage, or review workflow receives data.
- Public samples use reserved example domains, documentation IP ranges, and non-secret placeholder values.
- Runtime credentials must be provided through local environment variables or Google Cloud IAM. Do not commit `.env`, service account JSON, tokens, SQLite databases, logs, or generated workspaces.

If you find a secret or sensitive operational detail in this repository, rotate
the affected credential if applicable and remove it from Git history before
publishing.
