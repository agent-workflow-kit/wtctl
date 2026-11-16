from pathlib import Path
import subprocess, sys
def P(*a):
    sys.__stderr__.write(' '.join(str(x) for x in a)+'\n')
from tests.createlib import CW, REPO

def test_smoke(tmp_path: Path, private_home: Path, capsys):
    w = CW(tmp_path, private_home).build()
    for extra in (["--source","remote"], ["--source","remote","--depth","1"], ["--source","remote","--depth","0"], ["--source","remote","--depth","1"]):
        r = w.create(capsys, "--throwaway", "--ref", w.shas["c2"], *extra)
        P(extra, r.code, r.err[-300:])
        if r.code == 0:
            p = r.json()["record"]["path"]
            P(r.json()["record"]["source"], r.json()["record"]["depth"], r.json()["record"]["fetch_url"])
            P(subprocess.run(["ls", "-a", p, p + "/.git"], capture_output=True, text=True).stdout)
            P(subprocess.run(["git","--git-dir",p+"/.git","rev-list","--count","HEAD"], capture_output=True, text=True).stdout)
        else:
            P(r.out)
    r = w.create(capsys, "--throwaway", "--ref", "1"*40, "--source", "remote")
    P(r.code, r.out)
    P(w.rows("select id,state from records"), list((w.root/"throwaway").iterdir()))
