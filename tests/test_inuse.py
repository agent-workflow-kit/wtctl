"""In-use detection: matching rules (fake scanner) and the real ``/proc`` scanner."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.removallib import FakeScanner
from wtctl import inuse
from wtctl.gateway import Gateway, ProcessInfo, ScannerUnavailable
from wtctl.procscan import LsofScanner, ProcScanner, parse_lsof

T = "/w/tree"


def gw(*procs: ProcessInfo, fail: bool = False) -> Gateway:
    sc = FakeScanner()
    sc.procs = list(procs)
    sc.fail = fail
    return Gateway(scanner=sc)


def hit(*procs: ProcessInfo, ids: set[tuple[int, int]] | None = None, own: int = 999_999):  # noqa: ANN201
    return inuse.check_in_use(gw(*procs), T, ids or set(), own_pid=own)


# -- matching -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (T, True),
        (T + "/", True),
        (T + "/sub/dir", True),
        (T + "2", False),  # sibling with the target as a string prefix
        (T + "-old/x", False),
        ("/w", False),
        ("/", False),
        (T + " (deleted)", True),  # the kernel's marker on a removed directory
        (T + "/gone.txt (deleted)", True),
        ("/other/tree/x", False),
    ],
)
def test_path_matching_is_by_component(path: str, expected: bool) -> None:
    assert inuse.path_matches(path, T) is expected


def test_cwd_inside_is_reported_with_pid_command_and_path() -> None:
    r = hit(ProcessInfo(10, "bash", T + "/sub"))
    assert (r.status, r.processes) == ("fail", [{"pid": 10, "command": "bash", "path": T + "/sub"}])


def test_open_file_inside_is_reported() -> None:
    r = hit(ProcessInfo(11, "tail", "/elsewhere", (T + "/log.txt",)))
    assert r.processes == [{"pid": 11, "command": "tail", "path": T + "/log.txt"}]


def test_deleted_cwd_and_deleted_file_still_count() -> None:
    r = hit(
        ProcessInfo(12, "sh", T + "/d (deleted)"), ProcessInfo(13, "x", "/", (T + "/f (deleted)",))
    )
    assert {p["pid"] for p in r.processes} == {12, 13}


def test_cwd_matched_by_directory_identity_under_another_path() -> None:
    """Spike E3: a sandboxed process sees the directory bind-mounted at another path."""
    ids = {(7, 70), (7, 71)}
    r = hit(ProcessInfo(14, "claude", "/sandbox/view", (), cwd_id=(7, 71)), ids=ids)
    assert r.processes == [{"pid": 14, "command": "claude", "path": "/sandbox/view"}]
    assert hit(ProcessInfo(15, "x", "/sandbox/view", (), cwd_id=(7, 99)), ids=ids).status == "pass"


def test_open_files_at_another_path_are_not_matched() -> None:
    # open files are matched by path only (the sandbox binds the worktree at the same path)
    r = hit(ProcessInfo(16, "x", "/", ("/sandbox/view/f",), cwd_id=(1, 1)), ids={(7, 70)})
    assert r.status == "pass"


def test_untouching_processes_pass() -> None:
    r = hit(ProcessInfo(17, "x", "/home", ("/home/f",)))
    assert (r.status, r.processes, r.warnings) == ("pass", [], [])


def test_unavailable_scanner() -> None:
    r = inuse.check_in_use(gw(fail=True), T, set())
    assert r.status == "unavailable" and r.processes == [] and r.warnings


def test_uninspected_processes_are_counted_in_a_warning() -> None:
    r = hit(ProcessInfo(20, "other-user", None, inspected=False), ProcessInfo(21, "me", "/home"))
    assert r.status == "pass" and any("1 process" in w for w in r.warnings)


def test_hostile_command_and_path_are_escaped() -> None:
    nasty = T + "/a\nb\x1b[0m\udcff"
    r = hit(ProcessInfo(22, "ev\nil\x00", nasty))
    p = r.processes[0]
    assert "\n" not in p["path"] and "\n" not in p["command"] and "\x1b" not in p["path"]
    assert "\\x0a" in p["path"] and "\\xff" in p["path"]


# -- wtctl's own ancestry is excluded -------------------------------------------------------------


def test_own_process_and_its_children_are_excluded() -> None:
    procs = [
        ProcessInfo(100, "wtctl", T, ppid=50, start="s100"),
        ProcessInfo(101, "git", T, ppid=100, start="s101"),
        ProcessInfo(102, "git-sub", T, ppid=101, start="s102"),
        ProcessInfo(103, "stranger", T, ppid=1, start="s103"),
    ]
    g = gw(*procs)
    g.procs = _Starts({100: "s100", 50: "s50", 1: "s1"})  # type: ignore[assignment]
    r = inuse.check_in_use(g, T, set(), own_pid=100)
    assert [p["pid"] for p in r.processes] == [103]


class _Starts:
    def __init__(self, table: dict[int, str]) -> None:
        self.table = table

    def start_time(self, pid: int) -> str | None:
        return self.table.get(pid)


def test_ancestors_are_excluded_with_a_warning_when_they_sit_in_the_target() -> None:
    procs = [
        ProcessInfo(100, "wtctl", "/home", ppid=50, start="s100"),
        ProcessInfo(50, "bash", T + "/sub", ppid=10, start="s50"),
        ProcessInfo(10, "tmux", T, ppid=1, start="s10"),
        ProcessInfo(1, "init", "/", ppid=0, start="s1"),
        ProcessInfo(60, "sibling-shell", T, ppid=1, start="s60"),
    ]
    g = gw(*procs)
    g.procs = _Starts({100: "s100", 50: "s50", 10: "s10", 1: "s1", 60: "s60"})  # type: ignore[assignment]
    r = inuse.check_in_use(g, T, set(), own_pid=100)
    assert [p["pid"] for p in r.processes] == [60]
    assert sum("ancestor" in w for w in r.warnings) == 2


def test_an_ancestor_whose_pid_was_reused_is_not_excluded() -> None:
    procs = [
        ProcessInfo(100, "wtctl", "/home", ppid=50, start="s100"),
        ProcessInfo(50, "bash", T, ppid=1, start="OLD"),
    ]
    g = gw(*procs)
    g.procs = _Starts({100: "s100", 50: "NEW", 1: "s1"})  # type: ignore[assignment]
    assert [p["pid"] for p in inuse.check_in_use(g, T, set(), own_pid=100).processes] == [50]


def test_an_ancestor_that_cannot_be_verified_is_not_excluded() -> None:
    procs = [
        ProcessInfo(100, "wtctl", "/home", ppid=50, start="s100"),
        ProcessInfo(50, "bash", T, ppid=1, start="s50"),
    ]
    g = gw(*procs)
    g.procs = _Starts({100: "s100"})  # type: ignore[assignment]  # 50 is gone
    assert [p["pid"] for p in inuse.check_in_use(g, T, set(), own_pid=100).processes] == [50]


# -- the real /proc scanner ---------------------------------------------------------------------

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")


class Sleeper:
    """A process this test started, detached from it (wtctl's own children are excluded from the
    in-use check, so a child of the test would never be reported). It writes its own PID into the
    ready file and is killed by exactly that PID, after its command line is checked."""

    def __init__(self, code: str, cwd: Path, ready: Path) -> None:
        self.ready = ready
        full = (
            f"import os, time\n{code}\nopen({str(ready)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n"
        )
        subprocess.run(
            ["sh", "-c", '"$0" -c "$1" &', sys.executable, full],
            cwd=cwd, check=True, timeout=30,
        )  # fmt: skip
        self.pid = self.wait()

    def wait(self) -> int:
        deadline = time.monotonic() + 20
        while True:
            text = self.ready.read_text() if self.ready.exists() else ""
            if text.isdigit():
                return int(text)
            assert time.monotonic() < deadline, "child did not start"
            time.sleep(0.02)

    def stop(self) -> None:
        try:
            cmd = Path(f"/proc/{self.pid}/cmdline").read_bytes()
        except OSError:
            return
        if str(self.ready).encode() in cmd:  # still the process this test started
            os.kill(self.pid, signal.SIGKILL)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = tmp_path / "tree"
    (t / "sub").mkdir(parents=True)
    (t / "f.txt").write_text("data")
    return t


@pytest.fixture
def sleepers() -> Iterator[list[Sleeper]]:
    made: list[Sleeper] = []
    yield made
    for s in made:
        s.stop()


def real_check(tree: Path, ids: set[tuple[int, int]] | None = None) -> inuse.InUse:
    return inuse.check_in_use(Gateway(scanner=ProcScanner()), str(tree), ids or set())


@linux_only
def test_real_process_with_its_cwd_inside(
    tree: Path, sleepers: list[Sleeper], tmp_path: Path
) -> None:
    s = Sleeper("", tree / "sub", tmp_path / "r1")
    sleepers.append(s)
    r = real_check(tree)
    assert [(p["pid"], p["path"]) for p in r.processes] == [(s.pid, str(tree / "sub"))]


@linux_only
def test_real_process_with_an_open_file(
    tree: Path, sleepers: list[Sleeper], tmp_path: Path
) -> None:
    s = Sleeper(f"fh = open({str(tree / 'f.txt')!r})", tmp_path, tmp_path / "r2")
    sleepers.append(s)
    assert [(p["pid"], p["path"]) for p in real_check(tree).processes] == [
        (s.pid, str(tree / "f.txt"))
    ]


@linux_only
def test_real_process_with_a_mapped_file_whose_descriptor_is_closed(
    tree: Path, sleepers: list[Sleeper], tmp_path: Path
) -> None:
    (tree / "big.bin").write_bytes(b"x" * 8192)
    code = (
        "import mmap\n"
        f"fh = open({str(tree / 'big.bin')!r}, 'rb')\n"
        "m = mmap.mmap(fh.fileno(), 0, prot=mmap.PROT_READ)\n"
        "fh.close()\n"
    )
    s = Sleeper(code, tmp_path, tmp_path / "r3")
    sleepers.append(s)
    assert not os.listdir(f"/proc/{s.pid}/fd").count(str(tree))  # no descriptor is left
    assert [p["path"] for p in real_check(tree).processes] == [str(tree / "big.bin")]


@linux_only
def test_real_process_whose_cwd_was_deleted(
    tree: Path, sleepers: list[Sleeper], tmp_path: Path
) -> None:
    gone = tree / "gone"
    gone.mkdir()
    s = Sleeper("", gone, tmp_path / "r4")
    sleepers.append(s)
    gone.rmdir()
    assert os.readlink(f"/proc/{s.pid}/cwd").endswith(" (deleted)")
    assert [p["pid"] for p in real_check(tree).processes] == [s.pid]


@linux_only
def test_real_process_elsewhere_is_not_reported(
    tree: Path, sleepers: list[Sleeper], tmp_path: Path
) -> None:
    sleepers.append(Sleeper("", tmp_path, tmp_path / "r5"))
    assert real_check(tree).status == "pass"


@linux_only
def test_real_scanner_reports_ids_parents_and_start_times() -> None:
    me = next(p for p in ProcScanner().scan() if p.pid == os.getpid())
    st = os.stat(".")
    assert me.cwd_id == (st.st_dev, st.st_ino) and me.ppid == os.getppid() and me.start
    assert me.inspected and me.cwd == os.getcwd()


@linux_only
def test_real_scanner_marks_other_users_uninspected() -> None:
    procs = ProcScanner(uid=os.getuid() + 12345).scan()
    assert procs and not any(p.inspected for p in procs)


@linux_only
def test_a_missing_proc_is_an_unavailable_scanner(tmp_path: Path) -> None:
    with pytest.raises(ScannerUnavailable):
        ProcScanner(proc=str(tmp_path / "nope")).scan()


@linux_only
def test_own_ancestry_is_excluded_for_real(
    tree: Path, tmp_path: Path, sleepers: list[Sleeper]
) -> None:
    """sh (cwd inside) -> python (the wtctl stand-in): the shell is an excluded ancestor, a
    sibling with a cwd inside is still reported."""
    sibling = Sleeper("", tree, tmp_path / "r6")
    sleepers.append(sibling)
    code = (
        "import json, sys;"
        "from wtctl import inuse; from wtctl.gateway import Gateway;"
        "from wtctl.procscan import ProcScanner;"
        "r = inuse.check_in_use(Gateway(scanner=ProcScanner()), sys.argv[1], set());"
        "print(json.dumps([r.status, [p['pid'] for p in r.processes], r.warnings]))"
    )
    out = subprocess.run(
        ["sh", "-c", 'cd "$1" && "$2" -c "$3" "$1"; true', "sh", str(tree), sys.executable, code],
        capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    import json

    status, pids, warnings = json.loads(out.stdout)
    assert (status, pids) == ("fail", [sibling.pid])
    assert any("ancestor" in w for w in warnings)


def _unshare_works(tmp_path: Path) -> bool:
    unshare = shutil.which("unshare")
    if unshare is None or not sys.platform.startswith("linux"):
        return False
    r = subprocess.run([unshare, "-Urm", "true"], capture_output=True, timeout=30, check=False)
    return r.returncode == 0


@linux_only
def test_sandboxed_process_with_a_bind_mount_at_another_path(tree: Path, tmp_path: Path) -> None:
    """Spike E3: the worktree bind-mounted at another path in a mount namespace is found by the
    directory identity of its cwd, though no path of its cwd lies inside the target."""
    if not _unshare_works(tmp_path):
        pytest.skip("unprivileged user and mount namespaces are unavailable here")
    view = tmp_path / "view"
    view.mkdir()
    ready = tmp_path / "r7"
    script = (
        f'mount --bind "{tree}" "{view}" && cd "{view}/sub" '
        f'&& echo $$ > "{ready}" && exec sleep 120'
    )
    # detached from the test, like any other process of the user
    subprocess.run(
        ["sh", "-c", '"$0" -Urm sh -c "$1" &', str(shutil.which("unshare")), script],
        check=True, timeout=30,
    )  # fmt: skip
    deadline = time.monotonic() + 20
    while not (ready.exists() and ready.read_text().strip().isdigit()):
        assert time.monotonic() < deadline, "namespace did not start"
        time.sleep(0.02)
    pid = int(ready.read_text())
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        assert not inuse.inside(cwd, str(tree))  # by path alone it is invisible
        assert real_check(tree).status == "pass"
        st = os.stat(tree / "sub")
        found = real_check(tree, {(st.st_dev, st.st_ino)})
        assert [x["pid"] for x in found.processes] == [pid]
    finally:
        try:
            if b"sleep" in Path(f"/proc/{pid}/cmdline").read_bytes():
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# -- lsof ---------------------------------------------------------------------------------------


def test_parse_lsof() -> None:
    text = "p10\ncbash\nn/w/tree/sub\np11\ncvim\nn/w/tree/f.txt\nnsocket\n"
    procs = {p.pid: p for p in parse_lsof(text)}
    assert procs[10].command == "bash" and procs[10].open_paths == ("/w/tree/sub",)
    assert procs[11].open_paths == ("/w/tree/f.txt",)  # "socket" is not a path


def test_lsof_scanner_combines_working_directories_and_open_files() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        out = "p10\ncbash\nn/w/tree/sub\n" if "cwd" in argv else "p11\ncvim\nn/w/tree/f.txt\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    procs = LsofScanner(runner=runner, uid=1000).scan("/w/tree")
    by = {p.pid: p for p in procs}
    assert by[10].cwd == "/w/tree/sub" and by[11].open_paths == ("/w/tree/f.txt",)
    assert any(c[:2] == ["lsof", "-a"] and "1000" in c for c in calls)
    assert any("+D" in c and "/w/tree" in c for c in calls)
    r = inuse.check_in_use(Gateway(scanner=LsofScanner(runner=runner, uid=1000)), "/w/tree", set())
    assert {p["pid"] for p in r.processes} == {10, 11}


def test_lsof_missing_is_unavailable() -> None:
    def runner(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("lsof")

    with pytest.raises(ScannerUnavailable):
        LsofScanner(runner=runner).scan("/x")
