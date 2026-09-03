# -*- coding: utf-8 -*-
"""nai 总入口的分发、通配符自展开。"""
import numpy as np
from PIL import Image

from pynai.cli import main as nai
from pynai.core import iter_images


def _png(path):
    Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(path)


def test_dispatch(tmp_path, capsys):
    p = tmp_path / 'a.png'
    _png(p)
    assert nai(['i', str(p)]) == 0
    assert 'a.png' in capsys.readouterr().out
    assert nai(['s', '-n', str(p)]) == 0
    assert 'dry-run' in capsys.readouterr().out
    assert nai(['inspect', '-j', str(p)]) == 0
    assert nai(['strip', '-n', str(p)]) == 0


def test_dispatch_errors(capsys):
    assert nai([]) == 1
    assert nai(['--help']) == 0
    assert '子命令' in capsys.readouterr().out
    assert nai(['bogus']) == 2
    assert nai(['-V']) == 0
    assert capsys.readouterr().out.startswith('pynai ')


def test_glob_expansion(tmp_path):
    for n in ('b.png', 'a.png', 'c.jpg', 'note.txt'):
        (tmp_path / n).write_bytes(b'')
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'd.png').write_bytes(b'')
    names = [f.name for f, _ in iter_images([str(tmp_path / '*.png')])]
    assert names == ['a.png', 'b.png']
    names = [f.name for f, _ in iter_images([str(tmp_path / '**' / '*.png')])]
    assert names == ['a.png', 'b.png', 'd.png']
    assert list(iter_images([str(tmp_path / 'zzz*.png')])) == []
