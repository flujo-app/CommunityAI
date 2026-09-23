"""Package the shared lightweight anchor without copying runtime dependencies."""

from pathlib import Path

from setuptools import find_packages, setup
from setuptools.command.sdist import sdist

PROJECT = Path(__file__).resolve().parent
# A repository checkout has one authoritative source. A built source archive
# contains that exact source under its own src directory and needs no sibling.
SHARED = Path("../src/communityai_anchor")
if not (PROJECT / SHARED).is_dir():
    SHARED = Path("src/communityai_anchor")
if not (PROJECT / SHARED / "__init__.py").is_file():
    raise RuntimeError("Shared CommunityAI anchor package is missing")


class SharedSourceArchive(sdist):
    def make_release_tree(self, base_dir, files):
        # Setuptools otherwise copies ../src outside the archive root. Never
        # publish traversal entries or silently omit the shared implementation.
        safe = [name for name in files if not Path(name).is_absolute() and ".." not in Path(name).parts]
        super().make_release_tree(base_dir, safe)
        target = Path(base_dir) / "src" / "communityai_anchor"
        self.mkpath(str(target))
        for source in sorted((PROJECT / SHARED).glob("*.py")):
            self.copy_file(str(source), str(target / source.name))


setup(
    packages=sorted(
        set(find_packages("src", include=["communityai_desktop*", "communityai_anchor*"]) + ["communityai_anchor"])
    ),
    package_dir={"": "src", "communityai_anchor": str(SHARED)},
    cmdclass={"sdist": SharedSourceArchive},
)
