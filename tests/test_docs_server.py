from pathlib import Path

import servers.docs as docs


async def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    return await fn(*args, **kwargs)


async def test_docs_write_file_replace_and_append(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(docs, "REPOS", tmp_path)

    result = await _call(
        docs.write_file,
        "notes/OPERATIONS.md",
        "line one",
        create_dirs=True,
    )
    assert result.startswith("OK: wrote")
    assert (tmp_path / "notes/OPERATIONS.md").read_text() == "line one"

    result = await _call(
        docs.write_file,
        "notes/OPERATIONS.md",
        "\nline two",
        mode="append",
    )
    assert result.endswith("(append)")
    assert (tmp_path / "notes/OPERATIONS.md").read_text() == "line one\nline two"


async def test_docs_write_file_rejects_escape_path(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(docs, "REPOS", tmp_path)
    result = await _call(docs.write_file, "../outside.md", "nope")
    assert result == "Error: path must be inside ~/REPOS"


async def test_docs_write_file_rejects_non_text_extension(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(docs, "REPOS", tmp_path)
    result = await _call(docs.write_file, "bin/image.png", "nope", create_dirs=True)
    assert result.startswith("Error: writes limited to text/documentation files")


async def test_docs_write_file_requires_existing_parent_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(docs, "REPOS", tmp_path)
    result = await _call(docs.write_file, "newdir/file.md", "hello")
    assert result.startswith("Error: parent directory does not exist:")

