"""The file list follows the folders, without the model."""

from pathlib import Path

from lazyfinds import rules
from lazyfinds.indexer import Indexer
from lazyfinds.store import Store


def make(folder: Path, names: list[str]):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_text("Notes about the garden and the roses we planted in spring.", encoding="utf-8")


def names(store: Store) -> list[str]:
    return sorted(Path(p).name for p in store.all_files())


def test_new_changed_and_gone(tmp_path):
    docs = tmp_path / "docs"
    make(docs, ["a.txt", "b.md"])
    indexer = Indexer(Store(tmp_path / "i.sqlite"))
    c = indexer.sync([docs])
    assert (c.added, c.changed, c.removed) == (2, 0, 0)
    (docs / "a.txt").write_text("longer text than before, so the size changes", encoding="utf-8")
    (docs / "b.md").unlink()
    c = indexer.sync([docs])
    assert (c.added, c.changed, c.removed) == (0, 1, 1)


def test_unplugged_drive_keeps_its_files(tmp_path):
    drive = tmp_path / "usb"
    make(drive, ["a.txt"])
    store = Store(tmp_path / "i.sqlite")
    indexer = Indexer(store)
    indexer.sync([drive])
    drive.rename(tmp_path / "elsewhere")
    assert indexer.sync([drive]).removed == 0 and names(store) == ["a.txt"]


def test_folder_taken_off_the_list_loses_its_files(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    make(one, ["a.txt"])
    make(two, ["b.txt"])
    store = Store(tmp_path / "i.sqlite")
    indexer = Indexer(store)
    indexer.sync([one, two])
    assert indexer.sync([one]).removed == 1 and names(store) == ["a.txt"]


def test_what_is_never_read(tmp_path):
    root = tmp_path / "home"
    make(root / ".hidden", ["x.txt"])
    make(root / "node_modules", ["y.txt"])
    make(root / "AppData", ["z.txt"])
    make(root, ["passwords.txt", "notes.txt", "~$report.docx", "movie.mp4.crdownload", "server.key"])
    project = root / "app"
    make(project / "build", ["out.txt"])        # a build folder inside a project
    make(project, ["README.md"])
    (project / "package.json").write_text("{}")
    make(root / "build", ["plan.txt"])          # a folder called "build" outside any project is the user's
    store = Store(tmp_path / "i.sqlite")
    Indexer(store).sync([root])
    assert names(store) == ["README.md", "notes.txt", "plan.txt"]


def test_kinds():
    assert rules.kind_of(Path("Screenshot 2026-09-02 093012.png")) == "screenshot"
    assert rules.kind_of(Path("C:/Users/x/Pictures/Screenshots/abc.png")) == "screenshot"
    assert rules.kind_of(Path("PXL_1.jpg")) == "image"
    assert rules.kind_of(Path("a.mp3")) == "audio" and rules.kind_of(Path("a.py")) is None
    assert rules.is_code(Path("a.py")) and not rules.is_code(Path("app.min.js"))


def test_files_that_waited_for_ffmpeg_are_read_again(tmp_path):
    from lazyfinds.extract import FFMPEG_MISSING

    make(tmp_path / "m", ["a.txt", "b.txt"])
    store = Store(tmp_path / "i.sqlite")
    Indexer(store).sync([tmp_path / "m"])
    ids = [row[0] for row in store.all_files().values()]
    store.mark(ids[0], "failed", f"{FFMPEG_MISSING}: Settings → Install ffmpeg")
    store.mark(ids[1], "failed", "can't read the picture (OSError)")
    assert store.read_again(FFMPEG_MISSING) == 1
    status = dict(store.db.execute("SELECT id, status FROM files"))
    assert status[ids[0]] == "new" and status[ids[1]] == "failed"
