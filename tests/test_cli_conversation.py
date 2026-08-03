import re
from pathlib import Path
from app.cli import _ts, _open_conv_file


def test_ts_format():
    result = _ts()
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)


def test_open_conv_file_creates_dir(tmp_path):
    base = tmp_path / ".conversations"
    f = _open_conv_file(base)
    f.close()
    assert base.is_dir()


def test_open_conv_file_name_format(tmp_path):
    base = tmp_path / ".conversations"
    f = _open_conv_file(base)
    name = Path(f.name).name
    f.close()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.txt", name)


def test_open_conv_file_is_writable(tmp_path):
    f = _open_conv_file(tmp_path / ".conversations")
    f.write("hello\n")
    f.close()
