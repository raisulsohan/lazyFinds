"""Drives the app the way a person would and saves pictures of its windows (widget grabs, not the screen).

    LAZYFINDS_HOME=<test home> LAZYFINDS_INDEX=<test index> uv run python scripts/snapshot_ui.py <out folder> "query" ...

For each query: open the panel, type it, wait for meaning results, save the panel; then move to the first result,
preview it (Space), save the preview. Quits at the end.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lazyfinds import app as A


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    queries = sys.argv[2:]
    A.setup_logging()
    qt = QApplication(sys.argv[:1])
    qt.setQuitOnLastWindowClosed(False)
    lazy = A.LazyFindsApp(qt)
    lazy.start()
    steps = []

    def run_query(i, q):
        def go():
            lazy.preview.close()
            lazy.panel.summon()
            lazy.panel.query.setText(q)
        steps.append((go, 4500))

        def grab_panel():
            lazy.panel.grab().save(str(out / f"{i:02d}-panel.png"))
            print("panel:", q, "->", [Path(lazy.panel.list.item(r).data(256).path).name
                                      for r in range(lazy.panel.list.count())
                                      if lazy.panel.list.item(r).data(256)], flush=True)
            lazy.panel._step(1)
            lazy.panel.preview_current()
        steps.append((grab_panel, 2500))

        def grab_preview():
            lazy.preview.grab().save(str(out / f"{i:02d}-preview.png"))
            player = lazy.preview.player
            if player:
                print("  player at", player.position(), "ms of", player.duration(), flush=True)
        steps.append((grab_preview, 300))

    for i, q in enumerate(queries):
        run_query(i, q)

    def settings():
        lazy.preview.close()
        lazy.show_settings()
    steps.append((settings, 800))
    steps.append((lambda: lazy.settings_window.grab().save(str(out / "settings.png")), 300))
    steps.append((lazy.quit, 0))

    def advance():
        if not steps:
            return
        action, delay = steps.pop(0)
        action()
        QTimer.singleShot(delay, advance)

    QTimer.singleShot(1500, advance)
    qt.exec()


if __name__ == "__main__":
    main()
