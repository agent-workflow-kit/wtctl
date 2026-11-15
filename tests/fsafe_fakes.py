"""Seams for the mount tests: a ``Fs`` whose device and mount id can be faked per inode."""

from __future__ import annotations

import dataclasses
import os

from wtctl import fsafe
from wtctl.gateway import MountEntry


class FakeFs(fsafe.Fs):
    """Reports another ``st_dev`` / mount id for chosen inodes, emulating a mount point.

    ``dev_by_ino`` emulates a different file system (FUSE, tmpfs); ``mnt_by_ino`` emulates a
    same-device bind mount (only the mount id differs).
    """

    def __init__(
        self,
        dev_by_ino: dict[int, int] | None = None,
        mnt_by_ino: dict[int, int] | None = None,
        table: list[MountEntry] | None = None,
        mnt_default: int | None = 1,
    ) -> None:
        self.dev_by_ino = dev_by_ino or {}
        self.mnt_by_ino = mnt_by_ino or {}
        self.table = table
        self.mnt_default = mnt_default

    def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
        return dataclasses.replace(st, dev=self.dev_by_ino.get(st.ino, st.dev))

    def mount_id(self, fd: int) -> int | None:
        ino = os.fstat(fd).st_ino
        return self.mnt_by_ino.get(ino, self.mnt_default)

    def mount_table(self) -> list[MountEntry] | None:
        return self.table
