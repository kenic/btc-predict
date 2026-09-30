"""Explicit reversible activation, only after closure, backup, tag and frozen config."""
import subprocess
from phase4 import ROOT, load_config

if __name__=='__main__':
    load_config()
    subprocess.run(['git','rev-parse','--verify','phase3-complete'],cwd=ROOT,check=True)
    (ROOT/'phase4.enabled').touch(exist_ok=True)
    print('Phase 4 enabled; next hourly run may predict. Remove phase4.enabled to disable.')
