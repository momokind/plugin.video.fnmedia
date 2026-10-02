# -*- coding: utf-8 -*-
"""通用工具：设置读写、日志、通知、客户端管理"""
import json
import os
import threading
import time
from urllib.parse import urlparse, quote

import xbmc
import xbmcaddon
import xbmcvfs

ADDON_ID = 'plugin.video.fnmedia'
ADDON = xbmcaddon.Addon()
_ADDON_DATA = None

_client = None          # FnClient 单例（本进程内）
_client_sig = None      # 构建客户端时使用的配置签名，配置变化时重建


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[FNMedia] %s' % msg, level)


def debug(msg):
    log(msg, xbmc.LOGDEBUG)


def kodi_user_agent():
    """Kodi 播放器的默认 HTTP User-Agent（'Kodi/<版本>'）。

    用于云盘直链探测：探测 UA 与播放器实际播放时的 UA 一致 →
    探测 206 则播放必 206、探测 403 则播放也 403，零假设自洽。
    """
    try:
        ver = (xbmc.getInfoLabel('System.BuildVersion') or '').split()[0]
        return 'Kodi/%s' % ver if ver else 'Kodi'
    except Exception:
        return 'Kodi'


def notify(msg, error=False):
    import xbmcgui
    icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
    xbmcgui.Dialog().notification('飞牛影视', msg, icon=icon, time=5000)


# ---------------------------------------------------------------- 视频元数据
# Kodi 21 起 ListItem.setInfo('video',...)/setCast()/setProperty('resumetime'...)
# 均废弃，改用 InfoTagVideo 的 setter。下列 helper 统一封装，键名沿用
# setInfo 的 video 字段名，内部映射到 InfoTagVideo；老版本无 getInfoTagVideo
# 时回退到废弃 API（仍可用，仅告警）。

def _info_tag(li):
    """取 ListItem 的 InfoTagVideo。

    真机验证 Kodi 21 的方法名是 getVideoInfoTag()（返回 InfoTagVideo）；
    getInfoTagVideo 仅作未来改名时的防御性备选。
    """
    for name in ('getVideoInfoTag', 'getInfoTagVideo'):
        getter = getattr(li, name, None)
        if getter is None:
            continue
        try:
            tag = getter()
            if tag is not None:
                return tag
        except Exception:
            continue
    return None


def _as_list(v):
    """标量/逗号串/列表 → 字符串列表（InfoTagVideo 的 setDirectors 等要 list）"""
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [s.strip() for s in str(v).split(',') if s.strip()]


def apply_video_info(li, infos):
    """用 InfoTagVideo 设置元数据（替代废弃的 ListItem.setInfo('video', infos)）"""
    tag = _info_tag(li)
    if tag is None:
        try:
            li.setInfo('video', infos)
        except Exception:
            pass
        return
    for k, v in infos.items():
        if v is None or v == '':
            continue
        try:
            if k == 'title':
                tag.setTitle(str(v))
            elif k == 'plot':
                tag.setPlot(str(v))
            elif k == 'mediatype':
                tag.setMediaType(str(v))
            elif k == 'tvshowtitle':
                tag.setTvShowTitle(str(v))
            elif k == 'season':
                tag.setSeason(int(v))
            elif k == 'episode':
                tag.setEpisode(int(v))
            elif k == 'rating':
                tag.setRating(float(v))
            elif k == 'originaltitle':
                tag.setOriginalTitle(str(v))
            elif k == 'year':
                tag.setYear(int(v))
            elif k == 'genre':
                tag.setGenres(list(v) if isinstance(v, (list, tuple)) else [str(v)])
            elif k == 'director':
                tag.setDirectors(_as_list(v))
            elif k == 'writer':
                tag.setWriters(_as_list(v))
            elif k == 'country':
                tag.setCountries(_as_list(v))
            elif k == 'aired':
                tag.setFirstAired(str(v))
            elif k == 'premiered':
                tag.setPremiered(str(v))
            elif k == 'duration':
                tag.setDuration(int(v))
            elif k == 'playcount':
                tag.setPlaycount(int(v))
        except Exception:
            pass


def set_video_cast(li, cast):
    """设置演员表（替代废弃的 ListItem.setCast）

    InfoTagVideo.setCast 只接受 xbmc.Actor 对象（真机验证：传 dict 会报
    "Non api type passed to setCast ... expected XBMCAddon::xbmc::Actor"），
    这里把 {name,role,order,thumbnail} dict 转成 xbmc.Actor 再传入；
    Actor 类不可用（老 Kodi）或失败时回退老 ListItem.setCast（收 dict）。"""
    if not cast:
        return
    tag = _info_tag(li)
    if tag is not None:
        try:
            Actor = getattr(xbmc, 'Actor', None)
            if Actor is not None:
                actors = []
                for a in cast:
                    name = a.get('name') if isinstance(a, dict) else None
                    if not name:
                        continue
                    actors.append(Actor(
                        str(name),
                        str(a.get('role') or ''),
                        int(a.get('order') or 0),
                        str(a.get('thumbnail') or ''),
                    ))
                if actors:
                    tag.setCast(actors)
                    return
        except Exception:
            pass
    try:
        li.setCast(cast)
    except Exception:
        pass


def set_resume_point(li, time_s, total_s=0):
    """设置断点续播（替代废弃的 setProperty('resumetime'/'totaltime')）"""
    tag = _info_tag(li)
    if tag is not None:
        try:
            tag.setResumePoint(float(time_s or 0), float(total_s or 0))
            return
        except Exception:
            pass
    try:
        li.setProperty('resumetime', str(time_s))
        if total_s:
            li.setProperty('totaltime', str(total_s))
    except Exception:
        pass


def translate_path(path):
    """兼容 Kodi 19/20+ 的路径转换"""
    try:
        return xbmcvfs.translatePath(path)
    except AttributeError:
        return xbmc.translatePath(path)


def addon_data_dir():
    global _ADDON_DATA
    if _ADDON_DATA is None:
        _ADDON_DATA = translate_path(ADDON.getAddonInfo('profile'))
        if not os.path.isdir(_ADDON_DATA):
            os.makedirs(_ADDON_DATA, exist_ok=True)
    return _ADDON_DATA


def data_file(filename):
    return os.path.join(addon_data_dir(), filename)


def save_json(filename, obj):
    """原子写 JSON：先写线程唯一临时文件再 rename，杜绝并发写撞车与
    并发读看到截断内容（此前先 truncate 再 dump，另一进程/线程恰好来读
    会拿到半截 JSON 当空处理；共享 .tmp 名还会让并发 rename ENOENT）。"""
    try:
        path = data_file(filename)
        tmp = '%s.%d.tmp' % (path, threading.get_ident())
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log('save_json %s 失败: %s' % (filename, e), xbmc.LOGERROR)


def load_json(filename):
    try:
        with open(data_file(filename), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


_json_cache = {}          # 文件名 -> ((mtime_ns, size), 解析结果)


def load_json_cached(filename):
    """带进程内 mtime+size 校验的 JSON 读取（播放热路径防重复读盘解析）。

    写方（save_json）落盘会更新 mtime，下次读取自动重载，不惧数据过期；
    读不到/解析失败返回 {}。仅用于只读路径——返回的是共享对象，
    写路径请用 load_json 拿独立副本，避免调用方改动污染缓存。"""
    try:
        st = os.stat(data_file(filename))
        key = (st.st_mtime_ns, st.st_size)
        hit = _json_cache.get(filename)
        if hit is not None and hit[0] == key:
            return hit[1]
        data = load_json(filename)
        data = data if isinstance(data, dict) else {}
        _json_cache[filename] = (key, data)
        return data
    except Exception:
        return {}


def strip_pipe_url(url):
    """去掉 Kodi 管道参数（| 及其后）取纯 URL（进度匹配/扩展名检查用）"""
    return (url or '').split('|', 1)[0]


def build_pipe_options(headers):
    """把 HTTP 头编码为 Kodi 管道参数串（url|Key=Value&Key2=Value2）。

    Kodi 的 ffmpeg/curl 播放层支持在 URL 尾部附加 |Key=Value 传递请求头，
    是播放器无法逐条目设置 UA/Cookie 时的标准手段；值需 URL 编码，
    避免 & | = 被当作参数分隔符。headers 为空返回 ''。"""
    parts = []
    for key in sorted(headers or {}):
        value = headers[key]
        if value:
            parts.append('%s=%s' % (key, quote(str(value), safe='')))
    return ('|' + '&'.join(parts)) if parts else ''


def url_path_has_ext(url, ext):
    """URL 的 path 部分（? 与 | 之前）是否以指定扩展名结尾。

    Kodi 按扩展名识别容器：ISO/BDMV 挂载要求扩展名在 path 尾，
    放在 ? 查询参数里无效（插件机制限制，无法绕过）。"""
    ext = (ext or '').lower()
    if ext and not ext.startswith('.'):
        ext = '.' + ext
    return urlparse(strip_pipe_url(url)).path.lower().endswith(ext)


def b64url_encode(data):
    """bytes -> URL 安全 base64（去填充；解码端需补齐 = 至 4 的倍数）"""
    import base64
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


# ------------------------------------------------------------ 播放端 UA 捕获
# 网盘直链与换链时的声明 UA 精确绑定。Kodi 原生 curl 与 vfs.stream.fast
# （kodi::network::GetUserAgent()）默认发送的都是 Kodi 完整 UA 串（含平台
# 信息），但该串无法从 Python API 直接取得——由本地代理从真实请求中捕获，
# 换链时原样声明，使直链天然匹配播放端，无需依赖管道注入。

CLIENT_UA_FILE = 'client_ua.json'
_client_ua = None


def capture_client_ua(ua, is_video=False):
    """记录播放端实际发送的 UA（由本地代理对真实请求调用）。

    :param is_video: 是否来自视频流请求（/stream）。视频端（FastVFS 取流）
        的 UA 是直链绑定的目标，与图片加载端的 UA 不同时以视频端为准。"""
    global _client_ua
    if not ua:
        return
    if _client_ua == ua:
        return
    disk = load_json(CLIENT_UA_FILE) or {}
    current = disk.get('ua') or ''
    if current and current != ua and not is_video and not disk.get('is_video'):
        return    # 已有 UA 且本请求非视频端：不覆盖
    _client_ua = ua
    save_json(CLIENT_UA_FILE, {'ua': ua, 'is_video': bool(is_video)})


def get_client_ua():
    """播放端实际使用的完整 UA 串（未捕获到时返回 ''）"""
    global _client_ua
    if _client_ua is None:
        entry = load_json_cached(CLIENT_UA_FILE) or {}
        _client_ua = entry.get('ua') or ''
    return _client_ua


def delete_file(filename):
    try:
        p = data_file(filename)
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def temp_dir():
    """字幕等临时文件目录"""
    path = translate_path('special://temp/fnmedia')
    if not os.path.isdir(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
    return path


def get_setting(key, default=''):
    value = ADDON.getSetting(key)
    if value == '' or value is None:
        return default
    return value


def set_setting(key, value):
    ADDON.setSetting(key, value if value is not None else '')


def normalize_server(server, use_https):
    """把用户填写的服务器地址规范化为 http(s)://host[:port] 形式"""
    server = (server or '').strip().rstrip('/')
    if not server:
        return ''
    if server.startswith('http://') or server.startswith('https://'):
        return server
    scheme = 'https' if use_https else 'http'
    return '%s://%s' % (scheme, server)


def build_client():
    """根据设置构建 API 客户端"""
    from resources.lib.fnapi.client import FnClient
    server = normalize_server(get_setting('server'), get_setting('usehttps') == 'true')
    verify = get_setting('verifyssl', 'false') == 'true'
    client = FnClient(server, verify=verify)
    client.set_credentials(
        username=get_setting('username'),
        password=get_setting('password'),
        token=get_setting('token'),
    )
    return client


def ensure_client():
    """获取当前进程的客户端单例；服务器/账号变化时自动重建"""
    global _client, _client_sig
    sig = (
        get_setting('server'),
        get_setting('usehttps'),
        get_setting('username'),
        get_setting('token'),
    )
    if _client is None or sig != _client_sig:
        _client = build_client()
        _client_sig = sig
    return _client


# ------------------------------------------------------------ 首字母索引
# GB2312 一级汉字按拼音排序，用区位码边界反查首字母（纯标准库，覆盖
# 常用汉字；生僻字/非 CJK 归入"其它"）。用于大墙的 A-Z/0-9 快跳分组。
_GB2312_BOUNDS = (
    (0xB0A1, 'A'), (0xB0C5, 'B'), (0xB2C1, 'C'), (0xB4EE, 'D'), (0xB6EA, 'E'),
    (0xB7A2, 'F'), (0xB8C1, 'G'), (0xB9FE, 'H'), (0xBBF7, 'J'), (0xBFA6, 'K'),
    (0xC0AC, 'L'), (0xC2E8, 'M'), (0xC4C3, 'N'), (0xC5B6, 'O'), (0xC5BE, 'P'),
    (0xC6DA, 'Q'), (0xC8BB, 'R'), (0xC8F6, 'S'), (0xCBFA, 'T'), (0xCDDA, 'W'),
    (0xCEF4, 'X'), (0xD1B9, 'Y'), (0xD4D1, 'Z'),
)


def pinyin_initial(text):
    """标题首字符 → 分组键：'A'-'Z' / '0-9' / '其它'（空标题归其它）"""
    text = (text or '').strip()
    if not text:
        return '其它'
    c = text[0]
    if c.isdigit():
        return '0-9'
    if c.isascii() and c.isalpha():
        return c.upper()
    try:
        gb = c.encode('gb2312')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return '其它'
    if len(gb) < 2:
        return '其它'
    code = (gb[0] << 8) | gb[1]
    letter = '其它'
    for start, ch in _GB2312_BOUNDS:
        if code >= start:
            letter = ch
        else:
            break
    return letter


def base_url():
    return normalize_server(get_setting('server'), get_setting('usehttps') == 'true')
