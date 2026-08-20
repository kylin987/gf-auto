import hashlib


def generate_mtop_sign(token, t, app_key, data):
    """生成 mtop 接口的 sign 参数（算法参考 sign.py）。"""
    token = str(token).split('_')[0]
    arg = f'{token}&{t}&{app_key}&{data}'
    return hashlib.md5(arg.encode('utf-8')).hexdigest()


def gen_sign(_m_h5_tk, params, data):
    """兼容 sign.py 的原始调用方式。"""
    return generate_mtop_sign(_m_h5_tk, params['t'], params['appKey'], data['data'])
