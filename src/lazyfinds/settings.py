"""The user's choices, in %LOCALAPPDATA%\\lazyFinds\\settings.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import paths


@dataclass
class Settings:
    folders: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    code_folders: list[str] = field(default_factory=list)   # searched with "code:", in an index of their own
    hotkey: str = "Ctrl+Alt+Space"
    watch: bool = True
    backend: str = "auto"          # auto | cuda | vulkan | cpu
    bengali_ocr: bool = True       # Tesseract for Bengali text in pictures, when it's installed
    first_run_done: bool = False

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(paths.settings_file().read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        f = paths.settings_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
