"""Control library for the ViewSonic X100-4K (RS-232 / LAN control protocol)."""

__version__ = "2.1.0"

try:  # tools/make_release.py writes this into every release zip
    from ._build import BUILD, BUILD_DATE
except ImportError:  # a source tree that was never packaged
    BUILD, BUILD_DATE = "dev", ""


def version_line() -> str:
    """What ``--version`` prints and the services log at start-up, e.g.
    ``x100-control 2.0.1 (build 3f9c2a1b, 2026-10-12)``. The build is a hash of every
    file in the release, so it changes with any change, however small."""
    date = f", {BUILD_DATE}" if BUILD_DATE else ""
    return f"x100-control {__version__} (build {BUILD}{date})"
