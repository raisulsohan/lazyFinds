import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from lazyfinds import extract as X
from lazyfinds.store import Store

R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
PICTURE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
NOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"


def png(w: int, h: int, colour=(200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buffer, "PNG")
    return buffer.getvalue()


def rels(*items) -> str:
    rows = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{target}"{extra}/>' for i, t, target, extra in items)
    return f'<?xml version="1.0"?><Relationships xmlns="x">{rows}</Relationships>'


def office_file(path, members: dict):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def drawing(rid: str, descr: str = "") -> str:
    """An inline picture as Word writes it."""
    return (f'<w:drawing><wp:inline><wp:docPr id="1" name="Picture 1" descr="{descr}"/><a:graphic><a:graphicData>'
            f'<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="photo.png"/></pic:nvPicPr><pic:blipFill>'
            f'<a:blip r:embed="{rid}"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>')


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch):
    monkeypatch.setattr(X.ocr, "text_of", lambda image: "")


@pytest.fixture
def docx(tmp_path):
    body = (f'<w:document {R}><w:body>'
            f'<w:p><w:r><w:t>Our trip to the coast.</w:t></w:r></w:p>'
            f'<w:p><w:r>{drawing("rId5", "A red bicycle leaning on a wall&#xA;&#xA;Description automatically generated")}'
            f'</w:r></w:p>'
            f'<w:p><w:r>{drawing("rId6", "an icon")}</w:r></w:p>'
            # the same picture again, with the copy Word writes for old readers
            f'<w:p><w:r><mc:AlternateContent><mc:Choice>{drawing("rId5")}</mc:Choice><mc:Fallback><w:pict>'
            f'<v:shape alt="old copy"><v:imagedata r:id="rId5"/></v:shape></w:pict></mc:Fallback>'
            f'</mc:AlternateContent></w:r></w:p>'
            f'<w:p><w:r>{drawing("rId9", "a linked picture")}</w:r></w:p>'
            f'</w:body></w:document>')
    return office_file(tmp_path / "trip.docx", {
        "word/document.xml": body,
        "word/_rels/document.xml.rels": rels(("rId5", PICTURE, "media/image1.png", ""),
                                             ("rId6", PICTURE, "media/image2.png", ""),
                                             ("rId9", PICTURE, "http://example.com/x.png", ' TargetMode="External"')),
        "word/media/image1.png": png(600, 400),
        "word/media/image2.png": png(40, 40),
    })


@pytest.fixture
def pptx(tmp_path):
    def slide(text: str, picture: str = "") -> str:
        return (f'<p:sld {R}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p>'
                f'<a:p><a:fld type="slidenum"><a:t>77</a:t></a:fld></a:p></p:txBody></p:sp>{picture}'
                f'</p:spTree></p:cSld></p:sld>')

    pic = ('<p:pic><p:nvPicPr><p:cNvPr id="4" name="Picture 3" descr="Two goats on a hill"/></p:nvPicPr>'
           '<p:blipFill><a:blip r:embed="rId2"/></p:blipFill></p:pic>')
    return office_file(tmp_path / "farm.pptx", {
        "ppt/presentation.xml": f'<p:presentation {R}><p:sldIdLst><p:sldId id="256" r:id="rId3"/>'
                                f'<p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": rels(("rId2", "slide", "slides/slide1.xml", ""),
                                                ("rId3", "slide", "slides/slide2.xml", "")),
        # slide2.xml is shown first
        "ppt/slides/slide2.xml": slide("Welcome<a:br/>to the farm"),
        "ppt/slides/_rels/slide2.xml.rels": rels(),
        "ppt/slides/slide1.xml": slide("Animals", pic),
        "ppt/slides/_rels/slide1.xml.rels": rels(("rId2", PICTURE, "../media/image1.png", ""),
                                                 ("rId9", NOTES, "../notesSlides/notesSlide1.xml", "")),
        "ppt/notesSlides/notesSlide1.xml": '<p:notes><a:p><a:r><a:t>Say how many goats</a:t></a:r></a:p></p:notes>',
        "ppt/media/image1.png": png(800, 600, (40, 160, 40)),
    })


def test_word_pictures_are_read_once_with_their_alt_text(docx):
    reading = X.read_document(docx)
    pictures = [p for p in reading.pieces if p.part == "inner_picture"]
    # the icon is too small, the copy is the same picture, the linked one isn't in the file
    assert len(pictures) == 1
    p = pictures[0]
    assert p.start == 1 and p.stop is None and p.source == "image" and p.embed[0] == "media"
    assert p.line == "A red bicycle leaning on a wall"
    text = "\n".join(p.body for p in reading.pieces if p.part == "passage")
    assert "Our trip to the coast." in text and "A red bicycle leaning on a wall" in text
    assert "automatically generated" not in text and "old copy" not in text
    assert X.inner_picture(docx, 1).size == (600, 400)
    assert X.thumbnail(docx, "doc", 1, size=50, part="inner_picture").size == (100, 67)


def test_slides_in_show_order_with_notes_and_pictures(pptx):
    reading = X.read_document(pptx)
    slides = [(p.start, p.body) for p in reading.pieces if p.part == "slide"]
    assert slides[0] == (1, "Welcome\nto the farm")
    assert slides[1][0] == 2
    assert "Animals" in slides[1][1] and "Two goats on a hill" in slides[1][1] and "Say how many goats" in slides[1][1]
    assert "77" not in slides[1][1]   # the slide number
    (picture,) = [p for p in reading.pieces if p.part == "inner_picture"]
    assert (picture.start, picture.stop, picture.line) == (1, 2, "Two goats on a hill")
    assert reading.meta["slides"] == 2 and reading.meta["pictures"] == 1


def test_a_document_of_pictures_only_is_read(tmp_path):
    path = office_file(tmp_path / "scan.docx", {
        "word/document.xml": f'<w:document {R}><w:body><w:p><w:r>{drawing("rId1")}</w:r></w:p></w:body></w:document>',
        "word/_rels/document.xml.rels": rels(("rId1", PICTURE, "media/image1.png", "")),
        "word/media/image1.png": png(500, 500),
    })
    reading = X.read_document(path)
    assert [p.part for p in reading.pieces] == ["inner_picture"]


# Saved by Word and PowerPoint (Microsoft 365) as 97-2003 files
OLD = Path(__file__).parent / "reference" / "office97"


def test_old_word_file_text_fields_and_pictures():
    reading = X.read_document(OLD / "garden.doc")
    (passage,) = [p for p in reading.pieces if p.part == "passage"]
    # Bengali makes Word store the text as UTF-16; the hyperlink's field code is left out, its text kept
    assert passage.body == "Garden diary. টমেটোর চারা Read more here. A green fence with white stripes"
    pictures = [p for p in reading.pieces if p.part == "inner_picture"]
    # one in the line of text with no alt text, one floating with alt text
    assert [(p.start, p.line) for p in pictures] == [(1, None), (2, "A green fence with white stripes")]
    assert X.inner_picture(OLD / "garden.doc", 2).size == (420, 300)


def test_old_powerpoint_slides_in_show_order_with_notes():
    reading = X.read_document(OLD / "harvest.ppt")
    slides = [(p.start, p.body) for p in reading.pieces if p.part == "slide"]
    # the picture slide was moved first after the title slide was made
    assert slides == [(1, "A green fence\nTalk about the fence"), (2, "Harvest report\nTomatoes and beans")]
    (picture,) = [p for p in reading.pieces if p.part == "inner_picture"]
    assert (picture.start, picture.stop, picture.line) == (1, 1, "A green fence")
    assert X.inner_picture(OLD / "harvest.ppt", 1).size == (420, 300)


def test_old_files_locked_or_damaged(tmp_path):
    with pytest.raises(X.Skip, match="password"):
        X.read_document(OLD / "locked.doc")
    broken = tmp_path / "broken.doc"
    broken.write_bytes((OLD / "garden.doc").read_bytes()[:3000])
    with pytest.raises(X.Unreadable):
        X.read_document(broken)


def test_word_files_are_read_again_once(tmp_path):
    store = Store(tmp_path / "index.db")
    old = store.add_file(str(tmp_path / "a.DOCX"), "doc", 1, 1.0)
    other = store.add_file(str(tmp_path / "b.txt"), "doc", 1, 1.0)
    with store.transaction():
        store.add_piece(old, "passage", "text", 1, None, "words", "words")
    store.mark(old, "read")
    store.mark(other, "read")
    assert store.read_type_again("read again: docx pictures", ".docx") == 1
    assert store.read_type_again("read again: docx pictures", ".docx") == 0
    assert [r[0] for r in store.to_read()] == [old]
    assert store.db.execute("SELECT COUNT(*) FROM pieces WHERE file_id = ? AND part != 'name'", (old,)).fetchone()[0] == 0
    store.close()
