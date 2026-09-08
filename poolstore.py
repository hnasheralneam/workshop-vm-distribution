import fcntl
import json
import os
import uuid


def load(path):
    with open(path, "r") as f:
        return json.load(f)


def update(path, fn, factory=list):
    with open(str(path) + ".lock", "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = load(path)
            except FileNotFoundError:
                data = factory()
            data = fn(data)
            tmp = f"{path}.{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp, "w") as f:
                    os.fchmod(f.fileno(), 0o600)
                    json.dump(data, f, indent=2)
                    f.flush()
                os.replace(tmp, str(path))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return data
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
