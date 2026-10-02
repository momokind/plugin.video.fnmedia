# -*- coding: utf-8 -*-
"""飞牛影视 API 认证签名

移植自 fntv-electron src/modules/fn_api/request.ts 的 genFnAuthx：

    sign = MD5( [api_key, url, nonce, timestamp, MD5(json_body), api_secret].join('_') )
    Authx header = "nonce=<6位>&timestamp=<毫秒>&sign=<md5>"

注意事项：
- GET 请求 body 为空字符串，MD5('') = d41d8cd98f00b204e9800998ecf8427e
- POST/PUT 请求体末尾会附加随机 nonce 字段，签名与实际发送的 body 必须完全一致
- JSON 序列化必须与 JS JSON.stringify 一致：紧凑分隔符（无空格）、保持键插入顺序
"""
import hashlib
import json
import random
import time

API_KEY = 'NDzZTVxnRKP8Z0jXg1VAMonaG8akvh'
API_SECRET = '16CCEB3D-AB42-077D-36A1-F355324E4237'


def md5(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def dumps_compact(obj):
    """与 JS JSON.stringify 等价的紧凑序列化"""
    if obj is None:
        return ''
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)


def generate_nonce():
    """6 位随机数字字符串（防重放）"""
    return str(random.randint(100000, 999999))


def gen_authx(url_path, body_str=None):
    """生成 Authx 请求头

    :param url_path: API 路径（含 /v/api/v1 前缀，不含域名），必须与实际请求路径完全一致
    :param body_str: 实际发送的 JSON body 字符串；GET 传 None
    """
    nonce = generate_nonce()
    timestamp = str(int(time.time() * 1000))
    body_md5 = md5(body_str if body_str else '')
    sign_str = '_'.join([API_KEY, url_path, nonce, timestamp, body_md5, API_SECRET])
    sign = md5(sign_str)
    return 'nonce=%s&timestamp=%s&sign=%s' % (nonce, timestamp, sign)


def string_to_uuid(text):
    """把账号名转换为 UUID 形式的伪 IP 标识

    移植自 Go proxy utils.StringToUUID：sha1 取前 16 字节 hex，按 8-4-4-4-12 分段
    """
    if not text:
        return '00000000-0000-0000-0000-000000000000'
    digest = hashlib.sha1(text.encode('utf-8')).digest()[:16]
    hex_str = digest.hex()
    return '%s-%s-%s-%s-%s' % (
        hex_str[:8], hex_str[8:12], hex_str[12:16], hex_str[16:20], hex_str[20:],
    )
