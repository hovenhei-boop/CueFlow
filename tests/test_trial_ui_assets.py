from __future__ import annotations

from importlib import resources


def test_trial_static_resources_are_packaged_and_readable() -> None:
    root = resources.files("cueflow").joinpath("static").joinpath("trial")
    for name in ("index.html", "admin.html", "styles.css", "app.js", "admin.js"):
        item = root.joinpath(name)
        assert item.is_file()
        assert len(item.read_bytes()) > 100
