# -*- coding: utf-8 -*-
"""伪造元数据写入（-t）：文本写入所有字段；预设、编辑、模板；--set；各图片格式。"""
import json
import sys

import numpy as np
import pytest
from PIL import Image
from test_roundtrip import nai_png, random_rgba

from nai_meta.core import NAI_TEXT_KEYS, STEALTH_KEYS, summarize
from nai_meta.nai_inspect import choose_meta, inspect_file
from nai_meta.nai_strip import main as strip_main
from nai_meta.nai_strip import BUILTIN_PRESETS, list_presets, webp_chunks


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'cfg'))
    return tmp_path / 'cfg' / 'nai-meta'


def test_text_fills_every_chunk_and_stealth(tmp_path, cfg):
    src = tmp_path / 'a.png'
    nai_png(src)
    assert strip_main([str(src), '-t', '杂鱼']) == 0
    dst = tmp_path / 'a_poison.png'
    rec = inspect_file(dst)
    assert rec['text_chunks'] == {k: '杂鱼' for k in NAI_TEXT_KEYS}
    st = rec['stealth']['meta']
    assert {k: st[k] for k in STEALTH_KEYS} == {k: '杂鱼' for k in STEALTH_KEYS}
    assert np.array_equal(np.asarray(Image.open(dst))[..., :3], np.asarray(Image.open(src))[..., :3])
    assert 'ABCD1234' not in dst.read_bytes().decode('latin-1')          # 原有的模型哈希等内容已全部移除


def test_text_with_set_makes_comment_json(tmp_path, cfg):
    src = tmp_path / 'b.png'
    Image.fromarray(random_rgba()[:, :, :3]).save(src)                   # RGB 图像，不含元数据
    assert strip_main([str(src), '-t', 'x', '--set', 'seed=7', '--set', 'Title=t']) == 0
    dst = tmp_path / 'b_poison.png'
    rec = inspect_file(dst)
    assert rec['mode'] == 'RGBA' and rec['text_chunks']['Title'] == 't' and rec['text_chunks']['Software'] == 'x'
    meta, _ = choose_meta(rec, 'auto')
    s = summarize(meta)
    assert s['prompt'] == 'x' and s['uc'] == 'x' and s['seed'] == 7
    assert rec['consistent'] is True


def test_set_alone_keeps_original_and_overrides(tmp_path, cfg):
    src = tmp_path / 'c.png'
    nai_png(src)
    assert strip_main([str(src), '--set', 'seed=9', '--set', 'uc=bad hands']) == 0
    rec = inspect_file(tmp_path / 'c_poison.png')
    s = summarize(choose_meta(rec, 'auto')[0])
    assert s['prompt'] == '1girl, solo' and s['seed'] == 9 and s['uc'] == 'bad hands'
    assert s['signed_hash'] is None and rec['consistent'] is True


def drive(argv, text):
    """将 text 作为终端输入传给 prompt_toolkit，并运行 nais。"""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(text)
            return strip_main(argv)


def test_edit_interactive_creates_and_uses_presets(tmp_path, cfg):
    # -t edit:3：用 KEY=VALUE 修改字段，:w 保存为预设 3；未指定图片时只保存预设
    assert drive(['-t', 'edit:3'], 'prompt=preset junk\nseed=null\n:w\n') == 0
    d = json.loads((cfg / 'presets' / '3.json').read_text('utf-8'))
    assert d['Comment']['prompt'] == 'preset junk' and d['Comment']['seed'] is None and d['Description'] == 'preset junk'
    # -t edit 4（以空格分隔的写法）：:all 将所有字段设为同一文本
    assert drive(['-t', 'edit', '4'], ':all 全塞\n:w\n') == 0
    d = json.loads((cfg / 'presets' / '4.json').read_text('utf-8'))
    assert d['Title'] == '全塞' and d['Comment'] == '全塞'
    # 使用预设 3：seed 为空时随机生成，宽高取图像实际尺寸
    src = tmp_path / 'd.png'
    nai_png(src)
    assert strip_main([str(src), '-t', '3']) == 0
    rec = inspect_file(tmp_path / 'd_poison.png')
    s = summarize(choose_meta(rec, 'auto')[0])
    assert s['prompt'] == 'preset junk' and s['seed'] not in (None, 42) and (s['width'], s['height']) == (160, 120)
    assert rec['consistent'] is True
    # 使用预设 4：所有字段（包括 Comment）均为该文本，Comment 不得被替换为默认 JSON
    assert strip_main([str(src), '-t', '4', '-o', str(tmp_path / 'f.png')]) == 0
    rec = inspect_file(tmp_path / 'f.png')
    assert rec['text_chunks'] == {k: '全塞' for k in NAI_TEXT_KEYS}
    assert {k: rec['stealth']['meta'][k] for k in STEALTH_KEYS} == {k: '全塞' for k in STEALTH_KEYS}
    assert strip_main([str(src), '-t', '9']) == 1                         # 不存在的预设
    assert strip_main(['-t', 'list']) == 0
    assert strip_main([str(src), '-t', f'@{cfg / "presets" / "3.json"}', '-o', str(tmp_path / 'e.png')]) == 0
    assert summarize(choose_meta(inspect_file(tmp_path / 'e.png'), 'auto')[0])['prompt'] == 'preset junk'


def test_edit_number_select_and_name_prompt(tmp_path, cfg):
    src = tmp_path / 'g.png'
    nai_png(src)
    # 不带名称的 -t edit：以源文件元数据为基础；选择第 3 项（Software），Ctrl-U 清除预填值后输入；:w 后询问预设名称
    assert drive([str(src), '-t', 'edit'], '3\n\x15Fake Soft\n:w\nmyname\n') == 0
    d = json.loads((cfg / 'presets' / 'myname.json').read_text('utf-8'))
    assert d['Software'] == 'Fake Soft' and d['Comment']['seed'] == 42          # 其余字段沿用源文件
    rec = inspect_file(tmp_path / 'g_poison.png')
    assert rec['text_chunks']['Software'] == 'Fake Soft'
    # 参数与已有预设同名时使用该预设
    assert strip_main([str(src), '-t', 'myname', '-o', str(tmp_path / 'h.png')]) == 0
    assert inspect_file(tmp_path / 'h.png')['text_chunks']['Software'] == 'Fake Soft'
    # :q 取消
    assert drive([str(src), '-t', 'edit'], ':q\n') == 1
    assert not (tmp_path / 'g_poison2.png').exists()


def test_edit_json_external(tmp_path, cfg, monkeypatch):
    ed = tmp_path / 'ed.py'                                              # 模拟外部编辑器：修改 prompt
    ed.write_text('import json,sys\np=sys.argv[1]\nd=json.load(open(p,encoding="utf-8"))\n'
                  'd["Comment"]["prompt"]="from editor"\n'
                  'json.dump(d,open(p,"w",encoding="utf-8"),ensure_ascii=False)\n')
    monkeypatch.setenv('EDITOR', f'{sys.executable} {ed}')
    assert drive(['-t', 'edit:3'], ':json\n:w\n') == 0
    assert json.loads((cfg / 'presets' / '3.json').read_text('utf-8'))['Comment']['prompt'] == 'from editor'


def test_poison_webp_and_jpeg(tmp_path, cfg):
    arr = random_rgba()
    w = tmp_path / 'a.webp'                                              # 无损 WebP：写入 EXIF 与隐写层
    Image.fromarray(arr).save(w, lossless=True)
    assert strip_main([str(w), '-t', 'x']) == 0
    rec = inspect_file(tmp_path / 'a_poison.webp')
    assert rec['exif_meta']['Description'] == 'x' and rec['stealth']['meta']['Description'] == 'x'
    j = tmp_path / 'a.jpg'                                               # JPEG：仅写入 EXIF，扫描数据不变
    Image.fromarray(arr[:, :, :3]).save(j, quality=90)
    assert strip_main([str(j), '-t', 'y']) == 0
    out = tmp_path / 'a_poison.jpg'
    assert inspect_file(out)['exif_meta']['Description'] == 'y'
    raw, new = j.read_bytes(), out.read_bytes()
    assert new[new.index(b'\xff\xda'):] == raw[raw.index(b'\xff\xda'):]
    lw = tmp_path / 'l.webp'                                             # 有损 WebP：在容器层写入 EXIF，VP8 数据不变
    Image.fromarray(arr).save(lw, quality=80)
    assert strip_main([str(lw), '-t', 'z']) == 0
    out = tmp_path / 'l_poison.webp'
    rec = inspect_file(out)
    assert rec['exif_meta']['Description'] == 'z' and rec['stealth'] is None
    assert [t for t, _ in webp_chunks(out.read_bytes())] == [b'VP8X', b'VP8 ', b'EXIF']
    assert dict(webp_chunks(out.read_bytes()))[b'VP8 '] == dict(webp_chunks(lw.read_bytes()))[b'VP8 ']


BLANK, ZAKO = ' ' * 512, ', '.join(['杂鱼~♥'] * 64)


@pytest.mark.parametrize('num, text', [('1', BLANK), ('2', ZAKO)])
def test_builtin_presets_fill_every_chunk(tmp_path, cfg, num, text):
    """内置预设 1（空格）与 2（杂鱼~♥ ×64）：两层的所有字段（包括 Comment）均为该文本。"""
    assert BUILTIN_PRESETS[num][1] == text
    src = tmp_path / 'a.png'
    nai_png(src)
    assert strip_main([str(src), '-t', num]) == 0
    rec = inspect_file(tmp_path / 'a_poison.png')
    assert rec['text_chunks'] == {k: text for k in NAI_TEXT_KEYS}
    assert {k: rec['stealth']['meta'][k] for k in STEALTH_KEYS} == {k: text for k in STEALTH_KEYS}
    assert 'ABCD1234' not in (tmp_path / 'a_poison.png').read_bytes().decode('latin-1')
    j = tmp_path / 'b.jpg'                                               # JPEG：EXIF 中同样为该文本
    Image.fromarray(random_rgba()[:, :, :3]).save(j, quality=90)
    assert strip_main([str(j), '-t', num]) == 0
    assert inspect_file(tmp_path / 'b_poison.jpg')['exif_meta']['Description'] == text


def test_builtin_preset_zako_shape_and_set(tmp_path, cfg):
    assert ZAKO.count('杂鱼~♥') == 64 and ZAKO.startswith('杂鱼~♥, 杂鱼~♥') and ZAKO.endswith('♥')
    src = tmp_path / 'a.png'
    nai_png(src)
    # --set 修改 Comment 内部字段时，Comment 变为对象，prompt 与 uc 仍为该文本
    assert strip_main([str(src), '-t', '2', '--set', 'seed=7']) == 0
    s = summarize(choose_meta(inspect_file(tmp_path / 'a_poison.png'), 'auto')[0])
    assert s['prompt'] == ZAKO and s['uc'] == ZAKO and s['seed'] == 7


def test_own_preset_overrides_builtin(tmp_path, cfg):
    text = list_presets()
    assert '[内置] 空格' in text and '[内置] 杂鱼' in text
    # -t edit 2：以内置预设为基础修改一个字段并保存，覆盖内置预设
    assert drive(['-t', 'edit', '2'], 'Title=mine\n:w\n') == 0
    d = json.loads((cfg / 'presets' / '2.json').read_text('utf-8'))
    assert d['Title'] == 'mine' and d['Comment'] == ZAKO
    assert '覆盖内置预设“杂鱼”' in list_presets()
    src = tmp_path / 'a.png'
    nai_png(src)
    assert strip_main([str(src), '-t', '2']) == 0
    chunks = inspect_file(tmp_path / 'a_poison.png')['text_chunks']
    assert chunks['Title'] == 'mine' and chunks['Software'] == ZAKO and chunks['Comment'] == ZAKO
