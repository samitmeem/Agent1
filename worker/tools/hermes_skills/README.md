# Hermes Skills (Vendored Tools)

This directory holds **vendored, read-only** Hermes Agent tool modules,
isolated from the rest of the worker.

## Isolation contract

- Tools self-register into `worker.tools.hermes_skills.registry` (the local
  shim), **not** into any global singleton.
- No imports from `worker.tools.base`, `executor`, `github`, or `gmail`.
- Each tool must provide a `check_fn` returning `False` when its deps are
  missing (pip packages, env vars, system binaries).
- Only read-only / non-sensitive tools. No write operations.

## Adding a vendored tool

1. Copy the Hermes tool module here (e.g. `weather.py`).
2. Adjust its `registry.register(...)` call to import the local shim:
   ```python
   from worker.tools.hermes_skills.registry import registry
   ```
3. Ensure `check_fn` gates availability on deps + env vars.
4. The adapter (`worker.tools.hermes_adapter`) auto-discovers it on next
   import — no manual wiring needed.
