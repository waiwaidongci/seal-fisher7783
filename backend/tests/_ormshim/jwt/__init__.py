"""测试专用最小 PyJWT 垫片（鉴权非本功能重点，仅保证导入与解码不报错）。"""


def decode(token, secret, algorithms=None):
    # 测试不真正签发 JWT；解码失败由调用方统一转 401
    raise Exception('shim 不校验 token')


def encode(payload, secret, algorithm=None):
    return 'shim-token'
