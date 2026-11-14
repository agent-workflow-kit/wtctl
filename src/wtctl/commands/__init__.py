"""Command handlers: one module per command, named by its registry key.

A module defines ``run(ctx: Context) -> Result`` and raises ``WtctlError`` for expected
failures. Nothing else needs editing to add one (see ``wtctl.registry.load_handler``).

Handlers still to come:

- TODO(#5): export
- TODO(#7): list, show, label_add, label_remove
- TODO(#8): cron_install, cron_uninstall
- TODO(#11): locate, clone
- TODO(#15): create
- TODO(#16): bootstrap
- TODO(#17): baseline_check, baseline_accept
- TODO(#18): adopt, forget
- TODO(#19): status
- TODO(#20): remove
- TODO(#21): prune
- TODO(#22): extract
- TODO(#23): reconcile
"""
