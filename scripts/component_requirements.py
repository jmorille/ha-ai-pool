"""Print the pip requirements of the Home Assistant components the pools front.

The test harness installs Home Assistant but not its components' own
dependencies, and a component that cannot be imported makes its platform
untestable. Reading them from the installed manifests, rather than pinning them
a second time in a requirements file, keeps this correct across the Home
Assistant versions the CI matrix covers.
"""

from __future__ import annotations

import json
from pathlib import Path

import homeassistant

# The domains the pools front. Their dependencies are followed transitively,
# which is how tts reaches ffmpeg and ai_task reaches camera.
ROOTS = ("ai_task", "conversation", "stt", "tts", "sensor", "homeassistant")


def collect(root: Path, domain: str, seen: set[str], out: list[str]) -> None:
    """Add ``domain``'s requirements, then those of what it depends on."""
    if domain in seen:
        return
    seen.add(domain)

    manifest = root / domain / "manifest.json"
    if not manifest.exists():
        return

    data = json.loads(manifest.read_text(encoding="utf8"))
    for requirement in data.get("requirements", []):
        if requirement not in out:
            out.append(requirement)
    for dependency in data.get("dependencies", []):
        collect(root, dependency, seen, out)


def main() -> None:
    """Print one requirement per line, for ``pip install -r``."""
    root = Path(homeassistant.__file__).parent / "components"
    requirements: list[str] = []
    seen: set[str] = set()
    for domain in ROOTS:
        collect(root, domain, seen, requirements)

    # ai_task imports camera at module level for its image tasks, so the
    # component cannot be imported without it even though it is not a declared
    # dependency of anything here.
    collect(root, "camera", seen, requirements)

    for requirement in requirements:
        print(requirement)


if __name__ == "__main__":
    main()
