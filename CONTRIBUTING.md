# Contributing

Use Python 3.12/3.13 and a project virtual environment. Install `.[dev,datasets]`, run pytest, Ruff lint and formatting checks before opening a pull request. Tests do not need model credentials.

For a bug report include the version/commit, environment, minimal reproduction, expected/actual result and the relevant redacted trace. Never attach `.env`, an Authorization header or unrelated telemetry.

Keep read-tool adapters separate from repair execution. A new repair requires a fixed allowlist entry, explicit approval binding, documented idempotency/reconciliation behavior and negative tests. Do not add arbitrary shell execution to the investigation agents.

Use development cases for prompt changes. Freeze the protocol before using held-out test cases. Changes inspired by a test failure need a new evaluation iteration and an explicit record; do not silently rerun and publish only the successful attempt.

Public artifacts must distinguish offline scripted engineering checks from online model measurements and identify any automated approval policy.
