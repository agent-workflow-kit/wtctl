"""``wtctl label add <id|path> k=v...``."""

from __future__ import annotations

from .. import labels as labelmod
from ..context import Context, Result
from ..label_edit import edit


def run(ctx: Context) -> Result:
    return edit(
        ctx,
        preview=labelmod.merge_labels,
        apply=lambda state, record_id, labels: state.add_labels(record_id, labels),
        verb="added",
    )
