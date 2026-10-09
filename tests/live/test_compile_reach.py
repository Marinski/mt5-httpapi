"""What caller-supplied MQL5 source can reach, against the target's real
MetaEditor. Skipped when the target terminal does not serve /compile.

Every escape names C:\\Windows\\win.ini, which exists on every Windows host and
holds nothing secret, so a failure shows a real read rather than a wrong path,
and its assertion message cannot print anything sensitive. MetaEditor does not
stop a `..` walk at the drive root, so the walks need the depth below the
drive root of the per-request temp directory and of the include tree:
MT5_LIVE_COMPILE_WORK_DEPTH (default 7, for
C:\\Users\\Docker\\Desktop\\Shared\\logs\\compile-work\\compile-xxxx) and
MT5_LIVE_COMPILE_INCLUDE_DEPTH (default 9, for
...\\Shared\\terminals\\metaquotes\\base\\MQL5\\Include).

Each escape must come back as a 400 from the handler. A 200 means the file was
read; a 422 means MetaEditor ran on it, which is how a file's tokens end up in
`log`. The in-tree cases must still compile, so the check is not refusing
ordinary code.
"""

import os

import pytest

from tests.live.live_client import RestClient

_HTTP_NOT_FOUND = 404
_COMPILE_STATUSES = (200, 400, 422)
_COMPILE_TIMEOUT_SECONDS = 180

BODY = "\nint OnInit(){ return(INIT_SUCCEEDED); }\nvoid OnTick(){}\n"
TARGET = r"C:\Windows\win.ini"
# `..` walks from the temp directory, the include tree and the MQL5 root back
# to the drive root, then down to the target.
UP_WORK = "..\\" * int(os.environ.get("MT5_LIVE_COMPILE_WORK_DEPTH") or "7")
UP_INCLUDE = "..\\" * int(os.environ.get("MT5_LIVE_COMPILE_INCLUDE_DEPTH") or "9")
UP_MQL5 = UP_INCLUDE[3:]
TAIL = r"Windows\win.ini"


@pytest.fixture(scope="module")
def compile_source(rest: RestClient):
    """POST /compile on the target; skip the module when it has no /compile."""

    def run(source: str) -> tuple[int, dict]:
        resp = rest.session.post(
            rest.base + "/compile",
            json={"source": source, "filename": "reach.mq5"},
            timeout=_COMPILE_TIMEOUT_SECONDS,
        )
        if resp.status_code == _HTTP_NOT_FOUND:
            pytest.skip("POST /compile is not available on the target")
        assert resp.status_code in _COMPILE_STATUSES, f"HTTP {resp.status_code}: {resp.text[:300]}"
        return resp.status_code, resp.json()

    return run


ESCAPES = {
    "include absolute": f'#include "{TARGET}"',
    "include absolute, forward slashes": '#include "' + TARGET.replace("\\", "/") + '"',
    "include <absolute>": f"#include <{TARGET}>",
    "include .. walk": f'#include "{UP_WORK}{TAIL}"',
    "include .. walk, forward slashes": '#include "' + (UP_WORK + TAIL).replace("\\", "/") + '"',
    "include <..> walk": f"#include <{UP_INCLUDE}{TAIL}>",
    "include UNC": r'#include "\\localhost\C$\Windows\win.ini"',
    "include drive root": r'#include "\Windows\win.ini"',
    "include indented": f'   #include "{TARGET}"',
    "include after a comment": f'/* x */#include "{TARGET}"',
    "resource absolute": f'#resource "{TARGET}" as string r',
    "resource .. walk": f'#resource "{UP_WORK}{TAIL}" as string r',
    "resource \\.. walk": f'#resource "\\{UP_MQL5}{TAIL}" as string r',
    "property icon .. walk": f'#property icon "{UP_WORK}{TAIL}"',
    "property icon \\.. walk": f'#property icon "\\{UP_MQL5}{TAIL}"',
    "property icon absolute": f'#property icon "{TARGET}"',
}

IN_TREE = {
    "include <Trade\\Trade.mqh>": "#include <Trade\\Trade.mqh>\nCTrade trade;",
    "include <Trade/Trade.mqh>": "#include <Trade/Trade.mqh>\nCTrade trade;",
    "resource from the MQL5 tree": '#resource "\\Include\\Trade\\Trade.mqh" as string r',
    "import is not a compile-time read": '#import "kernel32.dll"\nuint GetTickCount();\n#import',
}


@pytest.mark.parametrize("name", list(ESCAPES))
def test_source_cannot_reach_outside_the_sandbox(compile_source, name):
    status, body = compile_source(ESCAPES[name] + BODY)
    assert status == 400, f"{name}: HTTP {status}, log={body.get('log', '')[:300]!r}"
    assert body["ok"] is False
    assert "refused" in body["log"] or "literal" in body["log"]


@pytest.mark.parametrize("name", list(IN_TREE))
def test_in_tree_directives_still_compile(compile_source, name):
    status, body = compile_source(IN_TREE[name] + BODY)
    assert status == 200, f"{name}: HTTP {status}, log={body.get('log', '')[-300:]!r}"
    assert body["ok"] is True
    assert body["ex5_base64"]
