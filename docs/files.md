# File API: read and write a terminal's files over HTTP

List, download, upload, unzip and delete files in a terminal's install directory, the folder holding `terminal64.exe`. That covers shared libraries in `MQL5/Include`, DLLs and `.ex5` imports in `MQL5/Libraries`, an expert's own data in `MQL5/Files`, and the logs, all without RDP or touching the host. A second tree reaches the MQL5 directory `POST /compile` builds against, so a whole library can be uploaded once and compiled against from then on.

## Contents

- [Enabling it](#enabling-it)
- [Endpoints](#endpoints)
- [Uploading a zip](#uploading-a-zip)
- [What the API refuses](#what-the-api-refuses)
- [The compile tree](#the-compile-tree)
- [MCP tools](#mcp-tools)
- [Errors](#errors)

## Enabling it

The file API is opt-in and off by default, because it reaches everything an expert can, including code that runs inside the terminal:

```yaml
# config/config.yaml
files:
  enabled: true
  max_extract_bytes: 209715200   # 200 MiB unpacked per ?extract (default)
  max_extract_files: 10000       # files per ?extract (default)
```

`files: true` is shorthand for `files: {enabled: true}`. A terminal entry can opt out with `files: false`. Unlike Chart Deployments, the file API also works on backtest-mode terminals. With it off, every route below answers 404.

## Endpoints

Paths are relative to the terminal's install directory and mirror it, `/`-separated:

```
http://localhost:8888/<broker>/<account>[/<instance>]/files/<path>
```

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/files`, `/files/<dir>` | List a directory |
| `GET` | `/files/<file>` | Download a file (`application/octet-stream`) |
| `PUT` | `/files/<file>` | Create or replace a file, creating its directories |
| `PUT` | `/files/<dir>?extract` | Unpack a zip into a directory (see below) |
| `DELETE` | `/files/<path>` | Delete a file, or an empty directory |
| `DELETE` | `/files/<dir>?recursive` | Delete a directory and everything in it |

The same routes exist under `/compile/files` for [the compile tree](#the-compile-tree).

```bash
AUTH=(-H "Authorization: Bearer $MT5_API_TOKEN")

# list a folder
curl "${AUTH[@]}" "$MT5_API_URL/files/MQL5/Include"

# upload one file (raw body; a multipart field named "file" works too)
curl "${AUTH[@]}" -X PUT --data-binary @Signals.mqh \
  "$MT5_API_URL/files/MQL5/Include/MyLib/Signals.mqh"

# read what an expert wrote
curl "${AUTH[@]}" "$MT5_API_URL/files/MQL5/Files/mybot/state.json"

# the terminal's journal for today (UTF-16, like every MT5 log)
curl "${AUTH[@]}" "$MT5_API_URL/files/logs/$(date +%Y%m%d).log" -o journal.log

# remove a library folder
curl "${AUTH[@]}" -X DELETE "$MT5_API_URL/files/MQL5/Include/MyLib?recursive"
```

A directory listing is the JSON version of `ls -al`. The response carries `count` and `total_size`, plus one entry per file or directory, directories first:

```json
{
  "tree": "terminal",
  "path": "MQL5/Include/MyLib",
  "count": 1,
  "total_size": 5000,
  "entries": [
    {
      "name": "Signals.mqh",
      "path": "MQL5/Include/MyLib/Signals.mqh",
      "type": "file",
      "size": 5000,
      "size_human": "4.9K",
      "mode": "-rw-rw-rw-",
      "attributes": ["archive"],
      "nlink": 1,
      "modified_at": 1791580800,
      "modified": "2026-10-09T20:00:00Z",
      "created_at": 1791580800,
      "created": "2026-10-09T20:00:00Z",
      "accessed_at": 1791580800,
      "accessed": "2026-10-09T20:00:00Z",
      "is_symlink": false,
      "readable": true,
      "writable": true
    }
  ]
}
```

- `size_human` is `ls -h` style. Directories report size 0.
- `mode` is `ls`'s mode string.
- `attributes` lists the Windows file attributes (`readonly`, `hidden`, `system`, `archive`, `reparse_point`, ...). Windows has no owner or group in the `ls` sense, so the listing has none.
- Times come as epoch seconds (`*_at`) and as UTC ISO 8601.
- `readable` and `writable` say what this API allows, not what the file system does.
- A symlink or junction also carries `link_target` and `link_outside_tree`. One that leads out of the tree is listed but can be neither read nor written.

An upload answers `201` when it created the file and `200` when it replaced one, with the stored `size` and `sha256`.

Uploads are capped at `max_upload_body_bytes` (25 MiB, the same as nginx's limit). Every write is atomic: the bytes land in a staging file beside the target and are renamed over it, so the terminal never reads half a file.

## Uploading a zip

Add `?extract` to a `PUT` and the body is treated as a zip. The path then names the directory it unpacks into:

```bash
cd MyLib && zip -r ../MyLib.zip . && cd ..
curl "${AUTH[@]}" -X PUT --data-binary @MyLib.zip \
  "$MT5_API_URL/files/MQL5/Include/MyLib?extract"
```

- The archive's tree is merged into the directory. Files it names are replaced, and everything else already there is left alone. To get an exact copy, delete the directory first with `?recursive`.
- The zip itself is never stored. The API unpacks it into a staging directory, checks every entry, and only then moves the files into place. A bad entry anywhere fails the whole upload with nothing written.
- The response lists every file written, with `path`, `size` and `sha256`.
- Refused: entries that would leave the target (`../`, absolute paths, drive letters), symlinks, encrypted entries, the same file named twice, and anything the single-file rules refuse. An archive that unpacks to more than `max_extract_files` files or `max_extract_bytes` bytes is refused with 413. The API counts the bytes it actually inflates, not the size the archive declares.
- `?extract=0` (or `false`) stores the bytes as an ordinary file.

## What the API refuses

The API refuses some paths in every request, with no setting to change that:

| Path | Read | Write / delete | Why |
| --- | --- | --- | --- |
| `mt5start.ini`, `Config/accounts.dat` | no | no | The broker login and password, and the terminal's saved accounts |
| `terminal64.exe`, `MetaEditor64.exe`, `metatester64.exe` | yes | no | The running terminal's binaries |
| `MQL5/Experts/Uploaded/`, `MQL5/Files/chartctl/`, `chartctl/` | yes | no | Chart Deployments' staged experts, protocol files and registry. Use the [Chart Deployments](chart-deployments.md) endpoints for those |
| The tree root, or a directory holding any of the above | | no | |

Every path is also checked for anything that could leave the tree on Linux or Windows: `..` segments, absolute and drive paths, `:` (alternate data streams), Windows device names (`CON`, `NUL`, `COM1`, ...), characters Windows forbids, segments ending in a dot or space, and symlinks or junctions that resolve outside the root. Comparisons ignore case, as Windows does. A protected path answers 403 whether it exists or not.

Everything else is open, `MQL5/Libraries` included. A DLL put there runs inside the terminal the next time an expert imports it (`AllowDllImport` is on in every terminal this stack provisions). Hand out the token that can call these routes accordingly.

## The compile tree

`/compile/files` is rooted at the MQL5 directory `POST /compile` builds against (`compile_include_dir`, by default `<compile_terminal_dir>/MQL5`). Every terminal on the VM shares it, so any terminal's prefix reaches the same files. Put a library there and every compile can `#include` it:

```bash
curl "${AUTH[@]}" -X PUT --data-binary @MyLib.zip \
  "$MT5_API_URL/compile/files/Include/MyLib?extract"

# then compile against it
#   #include <MyLib/Signals.mqh>
```

With `compile_local_cache` set, compiles read a mirror of this tree on the VM's own disk. A write or delete through `/compile/files` marks the mirror stale, and the next compile in any terminal's API process re-validates it before building, so it never builds against the old copy. A change made any other way, such as directly on the host, still waits up to 60 seconds, as described in [Compiling MQL5](compiling.md#custom-includes).

The compiled `.ex5` carries the library inside it, so the terminal that runs the expert never needs the `.mqh` files. The exception is `#import` of an `.ex5` or DLL: those load at run time from the running terminal's own `MQL5/Libraries`, so upload them there through `/files`.

## MCP tools

Both MCP endpoints have typed tools for this, so an agent never needs curl:

- `list_files(path?, tree?)`
- `get_file(path, tree?)` returns `text` for UTF-8 or UTF-16 text (MT5 logs are UTF-16), otherwise `content_base64`, plus `size` and `sha256`. Files over 16 MiB are refused; use REST for those.
- `put_file(path, content? | content_base64?, extract?, tree?)`
- `delete_file(path, recursive?, tree?)`

`tree` is `terminal` (the default) or `compile`. On the unified endpoint each tool also takes `broker`, `account` and `instance`, and `list_terminals` reports `files` per terminal. A per-terminal endpoint lists the tools only when the file API is on for it. The unified endpoint always lists them, and calling one on a terminal without the file API says so instead of returning a bare 404. The `/mcp` request is capped at 25 MiB, about 18 MiB of file after base64.

## Errors

Errors are JSON with an `error` message and a `code`:

| Status | `code` | Meaning |
| --- | --- | --- |
| 400 | `BAD_PATH` | The path is malformed or would leave the tree |
| 400 | `BAD_ARCHIVE` | The `?extract` body is not a usable zip |
| 400 | `BAD_REQUEST` | A multipart upload without a `file` field |
| 403 | `PROTECTED` | See [What the API refuses](#what-the-api-refuses) |
| 404 | `NOT_FOUND` | Nothing at that path |
| 409 | `CONFLICT` | A file where a directory is needed or the other way round, or a non-empty directory deleted without `?recursive` |
| 409 | `FILE_LOCKED` | Windows refused because another process, usually the terminal, holds the file open. Retry once it lets go |
| 413 | `TOO_LARGE`, `ARCHIVE_TOO_LARGE` | The upload or the unpacked archive is over its cap |
