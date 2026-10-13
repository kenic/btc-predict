"""Apply the approved invalid-response policy and process remaining 4R slots."""
import fcntl
from dotenv import load_dotenv
import next_stages as stages


def main():
    load_dotenv(stages.ROOT / '.env')
    with open(stages.ROOT / '.next-stages.lock', 'a') as lock:
        print('Waiting for any running worker...', flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        stages.migrate()
        with stages.database() as c:
            if c.execute("SELECT 1 FROM experiment_transitions WHERE stage='5'").fetchone():
                print('Phase 5 already enabled; no calls or policy changes made.', flush=True)
                return
            if not c.execute("SELECT 1 FROM experiment_transitions WHERE stage='4r'").fetchone():
                raise RuntimeError('Phase 4R has not started; refusing policy change')
        stages.enable_invalid_policy()
        if stages.advance() == '4r':
            stages.run_repeats(budget=100000)
        print('Planned 4R slots processed. Next stage:', stages.advance(), flush=True)


if __name__ == '__main__':
    main()
