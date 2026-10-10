import shlex
from pathlib import Path

from mcp.server.fastmcp import FastMCP


_ROOT = Path(__file__).resolve().parents[1]
_MCP_SDK_REQUIREMENT = "mcp==1.28.0"


def _requirements(path: str) -> list[str]:
    requirements = []
    for raw_line in (_ROOT / path).read_text(encoding="utf-8").splitlines():
        requirement = raw_line.split("#", 1)[0].strip()
        if requirement:
            requirements.append(requirement)
    return requirements


def _windows_boot_requirements() -> list[str]:
    marker = '"%PYDIR%\\python.exe" -m pip install '
    for raw_line in (_ROOT / "scripts" / "start.bat").read_text(
        encoding="utf-8"
    ).splitlines():
        if not raw_line.startswith(marker):
            continue

        command = raw_line.removeprefix(marker)
        requirements, separator, _redirect = command.partition(' > "%PIP_TMP%"')
        assert separator, "base pip install command must retain its log redirect"
        return shlex.split(requirements)

    raise AssertionError("base pip install command not found in scripts/start.bat")


def test_windows_boot_dependencies_match_api_requirements():
    assert _windows_boot_requirements() == _requirements("requirements-api.txt")


def _first_install_requirements() -> list[str]:
    marker = '"%PYDIR%\\python.exe" -m pip install "MetaTrader5'
    for raw_line in (_ROOT / "scripts" / "install.bat").read_text(
        encoding="utf-8"
    ).splitlines():
        if not raw_line.startswith(marker):
            continue
        command = raw_line.removeprefix('"%PYDIR%\\python.exe" -m pip install ')
        requirements, separator, _redirect = command.partition(' >> "%INSTALL_LOG%"')
        assert separator, "first-install pip command must retain its log redirect"
        return shlex.split(requirements)

    raise AssertionError("MetaTrader5 pip install command not found in scripts/install.bat")


def test_first_install_uses_the_same_pins_as_the_api():
    """install.bat installs the SDK before start.bat's full set; a different
    version there would be replaced on the next boot, or worse, kept."""
    api = set(_requirements("requirements-api.txt"))

    assert set(_first_install_requirements()) <= api


def test_host_test_requirements_use_the_api_versions():
    """requirements-test.txt carries part of the API's runtime to import
    mt5api.server on the host; a different version there tests other code."""
    api = {req.split("==")[0].lower(): req for req in _requirements("requirements-api.txt")}
    for requirement in _requirements("requirements-test.txt"):
        name = requirement.split("==")[0].lower()
        if name in api:
            assert requirement == api[name], f"{requirement} != {api[name]}"


def test_every_dependency_is_pinned_exactly():
    """SEC14 in .agents/rules/security.md."""
    for path in ("requirements-api.txt", "requirements-mcpunifier.txt", "requirements-test.txt"):
        for requirement in _requirements(path):
            assert "==" in requirement, f"{path}: {requirement} is not pinned exactly"
            assert not any(op in requirement for op in ("<", ">", "~=", "!=")), requirement


def test_mcp_consumers_pin_compatible_sdk():
    for path in ("requirements-api.txt", "requirements-mcpunifier.txt"):
        mcp_requirements = [
            requirement
            for requirement in _requirements(path)
            if requirement.lower().startswith("mcp")
        ]
        assert mcp_requirements == [_MCP_SDK_REQUIREMENT]


def test_fastmcp_v1_api_is_importable():
    assert FastMCP.__name__ == "FastMCP"
