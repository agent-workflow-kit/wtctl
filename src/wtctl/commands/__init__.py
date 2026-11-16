"""Command handlers: one module per command, named by its registry key.

A module defines ``run(ctx: Context) -> Result`` and raises ``WtctlError`` for expected
failures. Nothing else needs editing to add one (see ``wtctl.registry.load_handler``).

Handlers still to come:

- TODO(#11): locate, clone
- TODO(#19): status
"""
