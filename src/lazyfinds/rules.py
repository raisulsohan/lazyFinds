"""Which files lazyFinds reads, and which it never touches.

What it reads is decided by extension; what it never reads is decided first: Windows' own folders, folders tools fill
with downloaded or built files, hidden and system items, links (a junction can loop), files that only live in OneDrive
(reading one would download it), half-finished downloads, Office's lock files, and anything that looks like a key or a
password store. Code files go only to the code index, and only from the folders chosen for code.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

KINDS = {
    "image": {"jpg", "jpeg", "jfif", "png", "webp", "bmp", "gif", "tif", "tiff", "heic", "heif", "avif"},
    "pdf": {"pdf"},
    "doc": {"txt", "md", "markdown", "rtf", "docx", "doc", "pptx", "ppt", "odt", "html", "htm"},
    "audio": {"mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "wma", "aiff", "amr"},
    "video": {"mp4", "mkv", "mov", "avi", "webm", "wmv", "m4v", "mpg", "mpeg", "3gp", "mts", "m2ts", "flv"},
}
EXTENSION_KIND = {ext: kind for kind, exts in KINDS.items() for ext in exts}

CODE_EXTENSIONS = {
    "py", "pyw", "ipynb", "js", "mjs", "cjs", "jsx", "ts", "tsx", "vue", "svelte", "java", "kt", "kts", "scala", "go",
    "rs", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "cs", "fs", "vb", "swift", "m", "mm", "rb", "php", "lua", "r",
    "jl", "dart", "sql", "sh", "bash", "zsh", "ps1", "psm1", "bat", "cmd", "css", "scss", "less", "html", "htm",
    "jsx", "glsl", "hlsl", "wgsl", "cu", "zig", "nim", "ex", "exs", "erl", "hs", "ml", "clj", "groovy", "gradle",
    "cmake", "md", "rst", "toml", "yaml", "yml", "jsx",
}
CODE_NAMES = {"makefile", "dockerfile", "cmakelists.txt", "justfile", "rakefile", "gemfile", "vagrantfile"}

# Folders never walked into, wherever they are (compared in lower case).
NEVER_FOLDERS = {
    "windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin", "$windows.~bt",
    "$windows.~ws", "system volume information", "recovery", "perflogs", "msocache", "config.msi",
    "node_modules", "bower_components", "jspm_packages", "__pycache__", ".venv", "venv", "env", "site-packages",
    "dist-packages", ".tox", ".mypy_cache", ".pytest_cache", ".gradle", ".m2", ".nuget", "packages",
    "target", "build", "dist", "out", "obj", "bin", "debug", "release", "x64", "cmake-build-debug",
    "cmake-build-release", "deriveddata", "pods", ".next", ".nuxt", ".svelte-kit", ".cache", "vendor",
    "third_party", "thirdparty", "external", "deps",
}
# Of those, the ones that hold people's own files outside code projects: walked unless inside a project.
PROJECT_ONLY = {"build", "dist", "out", "bin", "debug", "release", "x64", "packages", "vendor", "external", "deps",
                "target", "env"}
PROJECT_MARKERS = {".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py", "cargo.toml", "go.mod",
                   "pom.xml", "build.gradle", "build.gradle.kts", "cmakelists.txt", "composer.json", "gemfile"}

SECRET_EXTENSIONS = {"pem", "key", "pfx", "p12", "jks", "keystore", "kdbx", "kdb", "ppk", "ovpn", "gpg", "pgp",
                     "asc", "cer", "crt", "der", "p7b", "p8", "agilekeychain", "1pif", "psafe3"}
SECRET_WORDS = ("password", "passwd", "credential", "secret", "private_key", "privatekey", "id_rsa", "id_ed25519",
                "id_ecdsa", "id_dsa", "recovery-codes", "recovery_codes", "backup-codes", "2fa", "seed-phrase",
                "wallet")
PARTIAL_EXTENSIONS = {"crdownload", "part", "partial", "download", "tmp", "temp", "!ut", "opdownload"}

_HIDDEN_OR_SYSTEM = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
_LINK = stat.FILE_ATTRIBUTE_REPARSE_POINT
# OneDrive's placeholders: "recall on data access/open" and "offline". Opening one downloads it.
_CLOUD_ONLY = 0x00400000 | 0x00040000 | stat.FILE_ATTRIBUTE_OFFLINE


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_secret(name: str) -> bool:
    lower = name.lower()
    if extension(lower) in SECRET_EXTENSIONS or lower.startswith(".env"):
        return True
    return any(word in lower for word in SECRET_WORDS)


def is_partial(name: str) -> bool:
    return name.startswith("~$") or extension(name) in PARTIAL_EXTENSIONS


def kind_of(path: Path) -> str | None:
    """The kind for the main index ("image", "screenshot", "pdf", "doc", "audio", "video"), or None."""
    name = path.name
    if is_secret(name) or is_partial(name) or name.startswith("."):
        return None
    kind = EXTENSION_KIND.get(extension(name))
    if kind == "image" and is_screenshot(path):
        return "screenshot"
    return kind


def is_code(path: Path) -> bool:
    name = path.name
    if is_secret(name) or is_partial(name):
        return False
    lower = name.lower()
    if lower.endswith((".min.js", ".min.css", ".bundle.js", ".lock", "-lock.json")):
        return False
    return extension(name) in CODE_EXTENSIONS or lower in CODE_NAMES


def is_screenshot(path: Path) -> bool:
    """Windows' Snipping Tool and Win+PrintScreen name them "Screenshot …" and keep them in a Screenshots folder; ShareX,
    Greenshot and Lightshot have their own names."""
    lower = path.name.lower()
    if lower.startswith(("screenshot", "screen shot", "screen_shot", "snip", "capture", "sharex", "greenshot",
                         "lightshot", "scr_", "scr-")):
        return True
    return path.parent.name.lower() in ("screenshots", "screen captures", "screenshot")


def folder_verdict(entry: os.DirEntry, in_project: bool, excluded: list[str]) -> str | None:
    """Why a folder isn't walked into, or None to walk it."""
    lower = entry.name.lower()
    if lower.startswith((".", "$")):
        return "hidden folder"
    if lower in NEVER_FOLDERS and (in_project or lower not in PROJECT_ONLY):
        return "system or tool folder"
    try:
        attributes = entry.stat(follow_symlinks=False).st_file_attributes
    except OSError:
        return "can't be read"
    if attributes & _LINK:
        return "link"
    if attributes & _HIDDEN_OR_SYSTEM:
        return "hidden folder"
    here = os.path.normcase(entry.path)
    for folder in excluded:
        out = os.path.normcase(folder.rstrip("\\/"))
        if here == out or here.startswith(out + os.sep):
            return "left out in Settings"
    return None


def file_verdict(st: os.stat_result) -> str | None:
    if st.st_file_attributes & _CLOUD_ONLY:
        return "only in OneDrive"
    if st.st_file_attributes & _HIDDEN_OR_SYSTEM:
        return "hidden file"
    if st.st_size == 0:
        return "empty"
    return None


def is_project(names: set[str]) -> bool:
    return bool({n.lower() for n in names} & PROJECT_MARKERS) or any(n.lower().endswith((".sln", ".csproj"))
                                                                    for n in names)
