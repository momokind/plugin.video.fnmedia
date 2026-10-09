# -*- coding: utf-8 -*-
"""原盘镜像字节嗅探：区分"真 ISO 镜像"与 NAS 转换出的正片 TS 流

fnOS mediasrv 对 NAS 本地的 iso / BDMV 原盘，media/range 不返回镜像原始
字节，而是返回抽取的正片 M2TS 流（Content-Type: video/mp2t，192 字节包
对齐），却仍用 Content-Range/File-Size 伪装成原文件的字节区间（真机实测
2026-10-09：《007：大破量子危机》《变形金刚3》两 ISO 播放失败——Kodi 按
.iso 扩展名先用 udf:// 探测 BDMV/index.bdmv，在 TS 字节里找不到目录结构，
回落 libdvd 找 VIDEO_TS.IFO 同样失败，报 "Error opening image file"；普通
mkv 则原样透传真实字节，Content-Type: application/octet-stream、offset0 为
EBML 魔数；BDMV 文件夹原盘与 iso 同待遇）。

播放 URL 以 .iso/.img 结尾时 Kodi 必按光盘镜像挂载，拿到 TS 字节必失败。
起播前嗅探一次：真镜像保持 .iso（Kodi→libbluray）；TS 流把 URL 文件名装饰
换成 .ts，Kodi 直接 demux 正片（无菜单导航）。

判定依据（按优先级）：
① sector 16 起的卷识别区魔数（最权威，规范规定）：UDF 必有 BEA01/NSR02/
   NSR03/TEA01（ECMA-167 卷识别序列），ISO9660 必有 CD001（PVD 偏移 1）；
② 上游 Content-Type: video/mp2t（mediasrv 转换流的自述）；
③ 0x47 同步字节按 188/192 字节步长连续 ≥4 个（MPEG-TS/M2TS 特征）。

结果按 media_guid 落盘缓存（同一文件字节不变，7 天 TTL 自愈）；400 = NAS
无此文件（云盘专用 guid），不缓存、维持镜像播放——云链路径给的是真实
文件字节，光盘镜像挂载在那里是正确行为。
"""
import threading
import time
import urllib.error

import xbmc

from resources.lib import util

KIND_DISC = 'disc'     # 真实光盘镜像字节（UDF/ISO9660）
KIND_TS = 'ts'         # 转换后的正片 TS/M2TS 流
KIND_CLOUD = 'cloud'   # NAS media/range 400：仅云盘文件（云链才是真字节）

DISC_EXTS = ('.iso', '.img')

KIND_CACHE_FILE = 'media_kind_cache.json'
KIND_CACHE_TTL = 7 * 86400      # 与 stream_size_cache 同策略：字节内容稳定
SNIFF_TIMEOUT = 8               # 中转抖动大（实测 0.26s~20s），单次探测预算

_kind_cache = {}                # media_guid -> (kind, expire)
_kind_lock = threading.Lock()


def is_disc_image_name(file_name):
    """文件名（剥 .strm 壳后）是否会被播放器按光盘镜像处理"""
    return bool(file_name) and str(file_name).lower().endswith(DISC_EXTS)


def _get_cached(media_guid):
    now = time.time()
    with _kind_lock:
        hit = _kind_cache.get(media_guid)
        if hit and hit[1] > now:
            return hit[0]
    entry = (util.load_json(KIND_CACHE_FILE) or {}).get(media_guid)
    if entry and entry.get('kind') in (KIND_DISC, KIND_TS) \
            and entry.get('expire', 0) > now:
        kind = entry['kind']
        with _kind_lock:
            _kind_cache[media_guid] = (kind, entry['expire'])
        return kind
    return None


def remember(media_guid, kind):
    """缓存判定结果（代理转发路径发现上游自述 mp2t 时也会调用）"""
    if kind not in (KIND_DISC, KIND_TS) or not media_guid:
        return
    expire = time.time() + KIND_CACHE_TTL
    with _kind_lock:
        _kind_cache[media_guid] = (kind, expire)
        if len(_kind_cache) > 500:
            _kind_cache.clear()
    try:
        disk = util.load_json(KIND_CACHE_FILE) or {}
        disk[media_guid] = {'kind': kind, 'expire': expire}
        if len(disk) > 500:
            ordered = sorted(disk.items(), key=lambda kv: kv[1].get('expire', 0))
            disk = dict(ordered[-500:])
        util.save_json(KIND_CACHE_FILE, disk)
    except Exception:
        pass


def _sector_magic(head):
    """sector 16 起的 2048 字节扇区流里找光盘镜像卷识别魔数"""
    for off in range(0, max(0, len(head) - 5), 2048):
        sec = head[off:off + 6]
        if sec[:5] in (b'BEA01', b'NSR02', b'NSR03', b'TEA01'):
            return True
        if sec[1:6] == b'CD001':
            return True
    return False


def _ts_sync(head):
    """MPEG-TS(188)/M2TS(192) 同步特征：同一相位连续 ≥4 个 0x47"""
    for stride in (188, 192):
        for phase in range(stride):
            if phase + 3 * stride >= len(head):
                break
            n, i = 0, phase
            while i < len(head) and head[i] == 0x47:
                n += 1
                i += stride
                if n >= 4:
                    return True
    return False


def probe(client, media_guid, log_tag=''):
    """嗅探 media/range 的真实字节。返回 KIND_DISC / KIND_TS / KIND_CLOUD / None。

    只读 sector 16 起 8KB（Range GET，不走代理自身）；探测失败不缓存、
    下次播放重试；disc/ts 结果落盘缓存，同一文件终身只探测一次。
    """
    cached = _get_cached(media_guid)
    if cached:
        return cached

    kind = None
    resp = None
    try:
        resp = client.http_open(
            'GET', client.get_video_url(media_guid),
            headers={
                'Authorization': client.token,
                'Cookie': 'mode=relay',
                'Range': 'bytes=32768-40959',   # sector 16-19 卷识别区
            },
            timeout=SNIFF_TIMEOUT)
        status = getattr(resp, 'status', None) or resp.getcode()
        if status and status < 400:
            ctype = ((resp.headers.get('Content-Type') or '')
                     .split(';')[0].strip().lower())
            body = resp.read(8192)
            if _sector_magic(body):
                kind = KIND_DISC
            elif ctype == 'video/mp2t' or _ts_sync(body):
                kind = KIND_TS
    except urllib.error.HTTPError as e:
        if e.code == 400:
            kind = KIND_CLOUD     # NAS 无此文件（云盘专用 guid）
        else:
            util.log('镜像嗅探 %sHTTP %d（media=%s）'
                     % (log_tag, e.code, media_guid[:16]), xbmc.LOGWARNING)
    except Exception as e:
        util.log('镜像嗅探 %s失败: %s（media=%s，下次播放重试）'
                 % (log_tag, e, media_guid[:16]), xbmc.LOGWARNING)
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    if kind in (KIND_DISC, KIND_TS):
        remember(media_guid, kind)
        util.debug('镜像嗅探: %s -> %s（media=%s）'
                   % (log_tag or '', kind, media_guid[:16]))
    return kind
