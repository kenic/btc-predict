"""Operator-run recovery of the one explicitly approved Jev validation failure."""
import fcntl
from dotenv import load_dotenv
import next_stages as stages


def main():
    load_dotenv(stages.ROOT / '.env')
    with open(stages.ROOT / '.next-stages.lock', 'a') as lock:
        print('Waiting for any running stage worker...', flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        stages.migrate()
        if stages.advance() != '4r':
            print('Phase 4R is not active; no calls made.', flush=True)
            return
        granted = stages.authorize_approved_retry()
        print('One retry authorized.' if granted else 'Existing authorization retained; no extra retry granted.', flush=True)
        stages.run_repeats(budget=100000)
        print('Phase 4R complete. Next stage:', stages.advance(), flush=True)


if __name__ == '__main__':
    main()
