"""Standalone fixture for parsing host prerequisites; no host commands run."""


def linger_yes(payload: str) -> bool:
    return payload == "Linger=yes\n"


def cgroup2_observed(mountinfo: str) -> bool:
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        if separator and len(before.split()) >= 5 and after.split()[:1] == ["cgroup2"]:
            if before.split()[4] == "/sys/fs/cgroup":
                return True
    return False


def main():
    assert linger_yes("Linger=yes\n")
    assert not linger_yes("Linger=no\n")
    assert not linger_yes("Linger=yes\nIgnored=yes\n")
    assert cgroup2_observed("25 1 0:23 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n")
    assert not cgroup2_observed("25 1 0:23 / /sys/fs/cgroup rw - cgroup cgroup rw\n")
    assert not cgroup2_observed("25 1 0:23 / /other rw - cgroup2 cgroup rw\n")
    print("standalone volunteer host prerequisite parser PASS")


if __name__ == "__main__":
    main()
