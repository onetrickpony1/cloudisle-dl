"""Shared archive and Potree hierarchy helpers."""

import collections, hashlib, os, re, struct
from pathlib import Path

BASE = "https://www.um.edu.mt/projects/cloudisle/DATA1/cloudisleII/webpublish/"


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def hierarchy_path(name, step):
    if not re.fullmatch(r"r[0-7]*", name) or step < 1:
        raise ValueError("Invalid node or step")
    digits = name[1:]
    return "/".join(
        ["r"]
        + [digits[i : i + step] for i in range(0, len(digits) // step * step, step)]
    )


def decode_hierarchy(data, root, step):
    if not data or len(data) % 5:
        raise ValueError("Truncated or invalid hierarchy")
    queue = collections.deque([root])
    rows = []
    for offset in range(0, len(data), 5):
        if not queue:
            raise ValueError("Extra hierarchy records")
        name = queue.popleft()
        mask, points = struct.unpack_from("<BI", data, offset)
        if len(name) - len(root) > step:
            raise ValueError("Hierarchy exceeds expected depth")
        rows.append((name, mask, points))
        queue.extend(name + str(i) for i in range(8) if mask & (1 << i))
    # Unread children are permitted only beyond the boundary of this hierarchy file.
    if any(len(n) - len(root) <= step for n in queue):
        raise ValueError("Missing hierarchy records before boundary")
    return rows
