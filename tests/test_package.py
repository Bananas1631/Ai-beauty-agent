from importlib.metadata import version

import beauty_agent


def test_package_is_installed_from_src_layout() -> None:
    assert version("beauty-agent") == beauty_agent.__version__
