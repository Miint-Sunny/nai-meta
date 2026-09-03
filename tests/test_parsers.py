# -*- coding: utf-8 -*-
"""真图里见到的各种情况：两层正常差异、枚举 Source、Enhance、Director Tools、inpaint 子字典、A1111 格式。"""
import json

from pynai.core import diff_meta, meta_from_text, parse_a1111, summarize

BASE = {'prompt': '1girl', 'uc': 'lowres', 'seed': 1, 'steps': 28, 'scale': 5.0, 'sampler': 'k_euler_ancestral',
        'noise_schedule': 'karras', 'width': 832, 'height': 1216, 'request_type': 'PromptGenerateRequest'}


def _meta(source='NovelAI Diffusion V4.5 4BDE2A90', **extra):
    return {'Software': 'NovelAI', 'Source': source, 'Comment': {**BASE, **extra}}


def test_diff_ignores_stealth_omitted_fields_and_signature():
    text = _meta(reference_image_multiple=['b64', 'b64'], director_references=[], signed_hash='AAA')
    stealth = _meta(reference_image_multiple=None, director_references=None, signed_hash='BBB')
    assert diff_meta(text, stealth) == []
    poisoned = _meta(prompt='杂鱼, 杂鱼', signed_hash='CCC')
    assert diff_meta(text, poisoned) == ['Comment.prompt']


def test_model_name_from_hash_when_source_is_enum():
    s = summarize(_meta('DiffusionModelMetaName.NAIv4next 4BDE2A90'))
    assert s['model']['name'] == 'NovelAI Diffusion V4.5' and s['model']['hash'] == '4BDE2A90'
    s = summarize({'Comment': {**BASE, 'model_hash': '0ADF9AB7'}})          # Source 整个丢了
    assert s['model']['name'] == 'NovelAI Diffusion V5'
    s = summarize(_meta('Stable Diffusion XL C1E1DE52'))                    # Source 正常时以它为准
    assert s['model'] == {'name': 'Stable Diffusion XL', 'hash': 'C1E1DE52', 'source': 'Stable Diffusion XL C1E1DE52', 'software': 'NovelAI'}


def test_enhance_and_director_tool_and_inpaint_subdict():
    s = summarize(_meta(request_type='Img2ImgRequest', upscaled_enhance=True, upscaled_width=2144, strength=0.38, noise=0.0))
    assert s['type']['kind'] == 'enhance' and s['type']['strength'] == 0.38
    s = summarize({'Comment': {**BASE, 'request_type': None, 'req_type': 'emotion', 'defry': 3}})
    assert s['type']['kind'] == 'director_tool' and s['type']['label'] == '导演工具 emotion' and s['type']['defry'] == 3
    s = summarize(_meta(request_type='NativeInfillingRequest', strength=None, img2img={'noise': 0.2, 'strength': 0.7}))
    assert s['type']['kind'] == 'inpaint' and s['type']['strength'] == 0.7 and s['type']['noise'] == 0.2


A1111 = ('masterpiece, 1girl, red hair\nNegative prompt: lowres, bad hands\n'
         'Steps: 30, Sampler: DPM++ 2M Karras, Schedule type: Karras, CFG scale: 7, Seed: 12345, Size: 832x1216, '
         'Model hash: 1a2b3c4d, Model: animagineXL, Denoising strength: 0.55, Hashes: {"model": "1a2b3c4d"}, Version: v1.9.0')


def test_parse_a1111_parameters():
    m = parse_a1111(A1111)
    s = summarize(m)
    assert s['prompt'] == 'masterpiece, 1girl, red hair' and s['uc'] == 'lowres, bad hands'
    assert (s['steps'], s['scale'], s['seed'], s['width'], s['height']) == (30, 7.0, 12345, 832, 1216)
    assert s['sampler'] == 'DPM++ 2M Karras' and s['noise_schedule'] == 'Karras'
    assert s['model'] == {'name': 'animagineXL', 'hash': '1a2b3c4d', 'source': None, 'software': 'Stable Diffusion WebUI'}
    assert s['type']['kind'] == 'a1111_img2img' and s['type']['strength'] == 0.55
    assert meta_from_text(A1111)['Comment']['steps'] == 30      # EXIF UserComment 里的同款文本也认
    assert parse_a1111('just a caption') is None
