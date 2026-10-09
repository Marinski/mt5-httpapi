"""File API: list, read, write, unzip and delete files in a terminal's
install directory (/files) and in the compile tree (/compile/files).

See docs/files.md for the REST and MCP surface.
"""
from mt5api.fileapi.tree import Tree

TERMINAL_TREE = "terminal"
COMPILE_TREE = "compile"

# Broker login and password (mt5start.ini, written by the API at boot) and the
# terminal's saved accounts.
_TERMINAL_HIDDEN = frozenset({
    "mt5start.ini",
    "config/accounts.dat",
})
# The running terminal's binaries, and the files Chart Deployments owns: its
# staged experts, the loader's protocol directory and its registry.
_TERMINAL_READONLY = (
    "terminal64.exe",
    "metaeditor64.exe",
    "metatester64.exe",
    "mql5/experts/uploaded",
    "mql5/files/chartctl",
    "chartctl",
)
# The compile tree is an MQL5 directory, possibly a live terminal's.
_COMPILE_READONLY = (
    "experts/uploaded",
    "files/chartctl",
)


def terminal_tree(terminal_dir: str) -> Tree:
    """The tree rooted at the terminal's install directory."""
    return Tree(
        name=TERMINAL_TREE,
        root=terminal_dir,
        hidden=_TERMINAL_HIDDEN,
        readonly=_TERMINAL_READONLY,
    )


def compile_tree(include_dir: str) -> Tree:
    """The tree rooted at the MQL5 directory MetaEditor compiles against."""
    return Tree(name=COMPILE_TREE, root=include_dir, readonly=_COMPILE_READONLY)
