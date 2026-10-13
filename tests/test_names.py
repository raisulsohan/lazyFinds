"""Name search over a small folder: pieces of names, several pieces at once, ranking, and live changes."""

from lazyfinds import names as N


def tree(root):
    for rel in ["Documents/contract_signed.docx", "Documents/report.txt", "Documents/old/report-2024.pdf",
                "Photos/IMG_0412.jpg", "Photos/birthday cake.jpg", "Music/প্রিয় গান.mp3", "Work/reports/"]:
        p = root / rel
        if rel.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")


def scanned(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYFINDS_NAMES", str(tmp_path / "names.sqlite"))
    tree(tmp_path / "pc")
    names = N.Names()
    names.scan(drives=[str(tmp_path / "pc")])
    return names


def found(names, query, **kw):
    return [f.name for f in names.search(query, **kw)]


def test_any_piece_of_a_name(tmp_path, monkeypatch):
    names = scanned(tmp_path, monkeypatch)
    assert found(names, "ract_si") == ["contract_signed.docx"]
    assert found(names, "0412") == ["IMG_0412.jpg"]
    assert found(names, "প্রিয়") == ["প্রিয় গান.mp3"]
    assert found(names, "CAKE") == ["birthday cake.jpg"]


def test_pieces_must_all_be_there(tmp_path, monkeypatch):
    names = scanned(tmp_path, monkeypatch)
    assert found(names, "report 2024") == ["report-2024.pdf"]
    assert found(names, "report zzz") == []


def test_exact_name_first_then_starts(tmp_path, monkeypatch):
    names = scanned(tmp_path, monkeypatch)
    assert found(names, "report") == ["report.txt", "reports", "report-2024.pdf"]
    assert found(names, "report", folders_only=True) == ["reports"]
    assert found(names, "report", extensions={"pdf"}) == ["report-2024.pdf"]


def test_saved_list_loads_and_follows_changes(tmp_path, monkeypatch):
    names = scanned(tmp_path, monkeypatch)
    again = N.Names()
    assert again.load() and again.count == names.count
    new = tmp_path / "pc" / "Documents" / "budget plan.xlsx"
    new.write_text("x", encoding="utf-8")
    again.added(str(new))
    assert found(again, "budget") == ["budget plan.xlsx"]
    new.unlink()
    again.removed(str(new))
    assert found(again, "budget") == []
    again.removed(str(tmp_path / "pc" / "Documents" / "report.txt"))
    assert "report.txt" not in found(again, "report")
