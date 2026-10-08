#!/usr/bin/env python3
"""Reuse Brio's installed app-host release guard for shared ingress operations."""
import contextlib
import fcntl
import grp
import os
from pathlib import Path
import re
import stat
import sys
import time

GUARD = Path('/run/lock/brio-release-evidence.guard')
LEASE = Path('/run/lock/brio-release-evidence.lease')


def open_checked(path, uid, gid, mode):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) != (uid, gid, mode):
            raise RuntimeError('Unsafe Brio release guard or lease permissions')
        return os.fdopen(fd, 'r')
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def deployment_guard():
    """Fail before mutation on missing/unsafe authority, contention or active evidence."""
    with open_checked(GUARD, 0, grp.getgrnam('makepad').gr_gid, 0o660) as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lease = open_checked(LEASE, 0, 0, 0o644)
        except FileNotFoundError:
            lease = None
        if lease is not None:
            with lease:
                value = lease.read(256)
                match = re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}\|[a-f0-9]{40}\|([0-9]{10,12})\n', value)
                if not match:
                    raise RuntimeError('Malformed Brio release evidence lease')
                if int(match[1]) > int(time.time()):
                    raise RuntimeError('Brio release evidence is active; ingress deployment is locked')
        yield guard.fileno()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: brio_release_guard.py COMMAND [ARG...]')
    with deployment_guard() as descriptor:
        # Keep the same lock owner across exec; do not leave a child deployment
        # running after a wrapper process exits or receives a signal.
        os.set_inheritable(descriptor, True)
        os.execvp(sys.argv[1], sys.argv[1:])
