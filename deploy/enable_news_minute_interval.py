"""Narrow upgrade for the legacy standalone 8pre collector; never modifies 8d inputs."""
import argparse,hashlib,json,shutil
from pathlib import Path

def upgrade(source):
    old="    if config.get('interval_seconds') not in range(600, 901):\n        raise ValueError('interval_must_be_600_to_900')"
    new="    interval = config.get('interval_seconds')\n    if type(interval) is not int or not 60 <= interval <= 900:\n        raise ValueError('interval_must_be_integer_60_to_900')"
    if new in source:return source
    if source.count(old)!=1:raise ValueError('unexpected_collector_version')
    return source.replace(old,new)

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--root',required=True);args=a.parse_args();root=Path(args.root);p=root/'news/feeds.py';old=p.read_text();new=upgrade(old)
    backup=root/'news/feeds.before-phase8d-minute.py'
    if not backup.exists():shutil.copy2(p,backup)
    compile(new,str(p),'exec');p.write_text(new)
    (root/'phase8d-minute-upgrade.json').write_text(json.dumps({'before_sha256':hashlib.sha256(old.encode()).hexdigest(),'after_sha256':hashlib.sha256(new.encode()).hexdigest(),'backup':str(backup)},indent=2)+'\n')
    print('{"collector_interval_minimum_seconds":60}')
