# -*- coding: utf-8 -*-
"""本地流媒体代理

Kodi 播放器无法为视频 URL 附加自定义认证头（Authorization/Authx/Cookie），
因此插件在本机 127.0.0.1 启动一个轻量 HTTP 代理：

  GET /stream/{media_guid}[/文件名]
                             视频流。优先云盘直链（带 Cookie/UA），否则转发 NAS
                             的 /v/api/v1/media/range/{media_guid}（带 Authorization）。
                             路径尾部可带真实文件名（含后缀，URL 编码），仅供播放器
                             识别文件名/容器类型与 OSD 显示，路由按 media_guid 查找
  GET /image?url=<encoded>   海报/剧照等图片，附加认证头后转发

Range / If-Range 等头原样透传，支持 Kodi 拖动进度条。
对应 fntv-electron 中 Go 代理（127.0.0.1:22345）的透明代理模式。

上游请求使用标准库 urllib（零外部依赖）。
"""
import os
import threading
import time
import urllib.error
import http.client
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

import xbmc

from resources.lib import util

_server = None
_server_lock = threading.Lock()
_client = None            # 当前 API 客户端（由插件进程设置）
_proxy_port = None

CHUNK_SIZE = 64 * 1024
UPSTREAM_TIMEOUT = 120    # 连接+读取超时（秒）
FORWARD_HEADERS = (
    'Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges',
    'Last-Modified', 'ETag', 'Content-Encoding',
)

# 上游 Content-Type 缺失或为 octet-stream 时，按 URL 尾部文件名后缀补全
MIME_BY_EXT = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
    '.mkv': 'video/x-matroska', '.webm': 'video/webm',
    '.avi': 'video/x-msvideo', '.wmv': 'video/x-ms-wmv',
    '.flv': 'video/x-flv', '.ts': 'video/mp2t', '.m2ts': 'video/mp2t',
    '.mts': 'video/mp2t', '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg',
    '.vob': 'video/mpeg', '.3gp': 'video/3gpp', '.ogv': 'video/ogg',
    '.iso': 'application/x-iso9660-image',   # 蓝光/DVD 原盘镜像
}


def _guess_mime(filename):
    ext = os.path.splitext(filename or '')[1].lower()
    return MIME_BY_EXT.get(ext)


# 115 等云盘直链的实测并发上限：每链路约 2-3 个并发连接，超出的请求被 403
# 拒绝（"115 pmt 3-2"）。BDMV ISO 挂载会产生并发随机读，一旦打爆会引发
# 403 风暴 → 反复失效直链缓存 → 反复 POST /stream 触发服务端扫描 → 卡死。
# 用信号量把对云盘直链的并发限制在安全值内，超出排队。
CLOUD_CONCURRENCY = 2
CLOUD_QUEUE_TIMEOUT = 30          # 排队等云盘槽位的超时（秒）
_cloud_slots = threading.BoundedSemaphore(CLOUD_CONCURRENCY)

# NAS media/range 对云盘 strm 返回 400：负缓存已知"仅云盘"的 guid，
# 后续请求跳过注定失败的 NAS 尝试（省一跳 RTT，也让 403 重试路径更干净）。
NAS_FAIL_TTL = 600
_nas_fail = {}
_nas_fail_lock = threading.Lock()


def _nas_only(media_guid):
    with _nas_fail_lock:
        return _nas_fail.get(media_guid, 0) > time.time()


def _mark_nas_fail(media_guid):
    with _nas_fail_lock:
        if len(_nas_fail) > 500:
            _nas_fail.clear()
        _nas_fail[media_guid] = time.time() + NAS_FAIL_TTL


# 云盘直链若为 302 中转（189/天翼等 strm 转发服务），每个播放请求都要重走
# 中转→云盘 API 签链→CDN 一整条链：中转侧对 QPS 敏感且偶发抖动会直接打断
# 播放（真机坑⑮）。首次请求跟随 302 后把最终 CDN 地址缓存（进程内短 TTL），
# 后续 Range/seek 直连最终地址；云链重签或 403 时自动失效回退中转重解析。
FINAL_URL_TTL = 600
_final_urls = {}             # media_guid -> (中转 URL, 过期时间, 最终 URL)
_final_urls_lock = threading.Lock()


def _get_final_url(media_guid, relay_url):
    with _final_urls_lock:
        hit = _final_urls.get(media_guid)
        if hit and hit[0] == relay_url and hit[1] > time.time():
            return hit[2]
    return None


def _remember_final_url(media_guid, relay_url, final_url):
    with _final_urls_lock:
        if len(_final_urls) > 200:
            _final_urls.clear()
        _final_urls[media_guid] = (relay_url, time.time() + FINAL_URL_TTL, final_url)


def _forget_final_url(media_guid):
    with _final_urls_lock:
        _final_urls.pop(media_guid, None)


# 文件总大小缓存：让 HEAD/Stat 能即时返回 Content-Length，不必为探测大小而打
# 上游（暖启动每次省 ~1-5s；冷启动避免 Kodi Stat 在服务端 65GB 扫描期间超时
# Timeout 28）。size 对 media_guid 稳定（同一文件），用长 TTL，下次 GET 以真实
# 值自动校正。内存缓存跨调用存活（代理常驻），磁盘缓存跨重启。
SIZE_CACHE_FILE = 'stream_size_cache.json'
SIZE_CACHE_TTL = 7 * 86400
_size_cache = {}              # media_guid -> (size, expire)
_size_cache_lock = threading.Lock()


def _get_cached_size(media_guid):
    now = time.time()
    with _size_cache_lock:
        hit = _size_cache.get(media_guid)
        if hit and hit[1] > now:
            return hit[0]
    entry = (util.load_json(SIZE_CACHE_FILE) or {}).get(media_guid)
    if entry and entry.get('expire', 0) > now and entry.get('size'):
        size = entry['size']
        with _size_cache_lock:
            _size_cache[media_guid] = (size, entry['expire'])
        return size
    return None


def _set_cached_size(media_guid, size):
    if not size or size <= 0:
        return
    expire = time.time() + SIZE_CACHE_TTL
    with _size_cache_lock:
        _size_cache[media_guid] = (size, expire)
        if len(_size_cache) > 500:
            _size_cache.clear()
    try:
        disk = util.load_json(SIZE_CACHE_FILE) or {}
        disk[media_guid] = {'size': size, 'expire': expire}
        if len(disk) > 500:
            ordered = sorted(disk.items(), key=lambda kv: kv[1].get('expire', 0))
            disk = dict(ordered[-500:])
        util.save_json(SIZE_CACHE_FILE, disk)
    except Exception:
        pass


def _extract_size(resp):
    """上游响应的文件总大小：206 取 Content-Range 的 total，200 取 Content-Length"""
    headers = getattr(resp, 'headers', None)
    if not headers:
        return None
    cr = headers.get('Content-Range')
    if cr and '/' in cr:
        total = cr.rsplit('/', 1)[-1].strip()
        if total.isdigit():
            return int(total)
    cl = headers.get('Content-Length')
    if cl and cl.isdigit():
        return int(cl)
    return None


# 后台预热云链：HEAD 即时返回的同时异步触发 stream_cached，让紧随其后的 GET
# 直接命中云链缓存（冷启动扫描与 Kodi 装配/首包并行，不额外阻塞 HEAD）。
# stream_cached 自身有 per-guid 单飞锁，后台线程与 GET 并发不会重复扫描；
# POST /stream 是 API 调用非 CDN 下载，不占 _cloud_slots，无 403 风暴（坑⑧）。
_bg_inflight = set()
_bg_lock = threading.Lock()


def _maybe_bg_resolve(media_guid):
    with _bg_lock:
        if media_guid in _bg_inflight:
            return
        _bg_inflight.add(media_guid)
    threading.Thread(target=_bg_resolve, args=(media_guid,), daemon=True).start()


def _bg_resolve(media_guid):
    try:
        client = get_client()
        if client is not None:
            # 代理转发可自行注入响应头里的 UA/Cookie，绑定 UA 不敏感；
            # 统一用默认（播放端完整）UA，与直链路径的缓存保持一致
            client.stream_cached(media_guid)
    except Exception:
        pass
    finally:
        with _bg_lock:
            _bg_inflight.discard(media_guid)


# 云盘 HTTPS 直链（115 等）的上游连接复用池。urllib 每请求新建 TCP+TLS（实测
# seek ~0.35s），复用连接可降到 ~0.1-0.2s（实测），使代理 seek 响应接近直连。
# 池仅用于云盘 HTTPS 最终直链；失败/3xx/stale 一律回退 urllib（见 _open_upstream）。
_cloud_pool = {}                 # (host, port) -> [idle HTTPSConnection, ...]
_cloud_pool_lock = threading.Lock()
CLOUD_POOL_MAX = CLOUD_CONCURRENCY   # 与并发槽位对齐（2）
_cloud_ssl_ctx = None


def _cloud_ssl_context():
    global _cloud_ssl_ctx
    if _cloud_ssl_ctx is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _cloud_ssl_ctx = ctx
    return _cloud_ssl_ctx


def _pool_take(host, port):
    """取一个空闲连接（可能 stale，调用方须能处理异常），无则返回 None"""
    key = (host, port)
    with _cloud_pool_lock:
        conns = _cloud_pool.get(key)
        if conns:
            return conns.pop()
    return None


def _pool_put(host, port, conn, reusable):
    """reusable 且池未满则放回，否则 close。Kodi seek 中断等未读尽响应一律不可复用。"""
    key = (host, port)
    if not reusable:
        try:
            conn.close()
        except Exception:
            pass
        return
    with _cloud_pool_lock:
        conns = _cloud_pool.setdefault(key, [])
        if len(conns) < CLOUD_POOL_MAX:
            conns.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass


def _new_https_conn(host, port):
    return http.client.HTTPSConnection(
        host, port, context=_cloud_ssl_context(), timeout=30)


def _discard_conn(conn_handle):
    """未读尽/重试时丢弃池化连接（不复用，避免 stale 连锁）"""
    if conn_handle:
        host, port, conn = conn_handle
        _pool_put(host, port, conn, reusable=False)


def set_client(client):
    """设置代理使用的 API 客户端（登录态变化后重新设置即可）"""
    global _client
    _client = client


def get_client():
    """取当前客户端；若设置里的令牌已更新则同步（复用的旧代理进程可能持过期 token）"""
    if _client is not None:
        try:
            token = util.get_setting('token', '')
            if token and token != _client.token:
                _client.token = token
                _client._token_checked = True
        except Exception:
            pass
    return _client


def _use_cloud_direct():
    return util.get_setting('usecloud', 'true') == 'true'


def _probe_existing(port, timeout=0.6):
    """探测指定端口是否已有本插件的代理在运行（跨进程复用，避免泄漏累积）"""
    import socket as _socket
    try:
        sock = _socket.create_connection(('127.0.0.1', port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(b'GET /ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
        # 响应头约 150 字节，标记在响应体里，需读够
        data = b''
        while len(data) < 512:
            chunk = sock.recv(512 - len(data))
            if not chunk:
                break
            data += chunk
        sock.close()
        return b'fnmedia-proxy' in data
    except Exception:
        return False


class _ProxyHTTPServer(ThreadingHTTPServer):
    """socketserver 默认 request_queue_size=5 太小：BDMV/UDF 挂载时播放器会
    并发突刺建立数十条连接，accept 队列溢出直接 RST（实测 16 并发 3 个秒断）"""
    daemon_threads = True
    request_queue_size = 128


def ensure_server():
    """获取代理端口：优先复用已运行的实例，否则启动新的

    Kodi 每次插件调用都是新 Python 进程，若各自启动代理会不断累积
    监听端口且旧进程无法回收（真机验证：一次会话泄漏 10 个端口）。
    因此先探测固定端口上的存活代理，命中则直接复用。
    """
    global _server, _proxy_port
    # 本进程已确认端口（自启服务或探测命中过）→ 直接返回，避免每次调用都探测
    # （build_cast 会调 ~40 次 image_url→ensure_server，每次探测 ~3ms 累计可观）
    if _proxy_port is not None:
        return _proxy_port
    with _server_lock:
        if _proxy_port is not None:
            return _proxy_port

        prefer_port = int(util.get_setting('proxyport', '22346') or 22346)

        # 已有代理存活（可能是之前插件进程启动的）→ 复用
        if _probe_existing(prefer_port):
            _proxy_port = prefer_port
            util.log('复用已运行的本地代理: 127.0.0.1:%d' % prefer_port)
            return prefer_port

        httpd = None
        port = prefer_port
        for candidate in (prefer_port, 0):  # 首选端口被其他程序占用则随机
            try:
                httpd = _ProxyHTTPServer(('127.0.0.1', candidate), ProxyHandler)
                port = httpd.server_address[1]
                break
            except OSError:
                continue
        if httpd is None:
            util.log('本地代理启动失败', xbmc.LOGERROR)
            return None

        thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                                  kwargs={'poll_interval': 0.5})
        thread.start()
        _server = httpd
        _proxy_port = port
        util.log('本地代理已启动: 127.0.0.1:%d' % port)
        return port


def stop_server():
    global _server, _proxy_port
    if _server is not None:
        try:
            _server.shutdown()
        except Exception:
            pass
        _server = None
        _proxy_port = None


def stream_url(media_guid, filename=None):
    """构造播放 URL；filename（真实文件名，含后缀）会编码后追加到路径尾部，
    供播放器识别文件名/容器类型。无 filename 时保持旧格式，完全兼容。"""
    port = ensure_server()
    if port is None:
        return None
    url = 'http://127.0.0.1:%d/stream/%s' % (port, media_guid)
    if filename:
        url += '/' + quote(filename, safe='')
    return url


def image_url(absolute_url):
    port = ensure_server()
    if port is None:
        return None
    return 'http://127.0.0.1:%d/image?url=%s' % (port, quote(absolute_url, safe=''))


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'   # 每请求一连接，按连接关闭分帧，最简单可靠

    def log_message(self, fmt, *args):
        util.debug('proxy: ' + (fmt % args))

    # -------------------------------------------------------------- 工具

    def _send_upstream_error(self, status, message=''):
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain')
            body = message.encode('utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    @staticmethod
    def _resp_final_url(resp):
        """urllib 跟随 302 后的最终地址（HTTPError 同样带 geturl）"""
        try:
            return resp.geturl()
        except Exception:
            return None

    @staticmethod
    def _upstream_host(resp, fetch_url):
        """故障日志用：实际上游主机（跟随重定向后的最终主机，兜底请求 URL 的主机）"""
        try:
            final = ProxyHandler._resp_final_url(resp)
            return urlparse(final or fetch_url).netloc or fetch_url
        except Exception:
            return fetch_url

    def _open_upstream(self, client, fetch_url, headers, is_cloud):
        """发起上游 GET，返回 (resp, conn_handle)。

        云盘 HTTPS 最终直链走连接池（复用 TLS，seek 延迟 ~0.35s→~0.1-0.2s）；
        其余（NAS http、非云盘）走 urllib client.http_open（每请求新连接）。

        conn_handle 非 None 表示用了池化 http.client 连接，调用方须在响应读尽后
        调 _pool_put 复用、中断时丢弃。池化失败/3xx/stale 一律回退 urllib——
        最坏情况等同当前 urllib 行为，绝不更差。
        """
        parsed = urlparse(fetch_url)
        use_pool = is_cloud and parsed.scheme == 'https' and parsed.netloc

        if not use_pool:
            resp = client.http_open('GET', fetch_url, headers=headers, timeout=UPSTREAM_TIMEOUT)
            return resp, None

        host = parsed.hostname
        port = parsed.port or 443
        req_path = parsed.path + (('?' + parsed.query) if parsed.query else '')

        # 合并默认头（与 http_open 一致），保留调用方传入的 Range/UA/Cookie
        req_headers = {'Accept-Encoding': 'identity'}
        req_headers.update(headers)

        # 尝试两次：第一次用池中空闲连接（可能 stale），第二次必用新连接。
        # 两次都失败则回退 urllib（其自动跟 302，能处理中转漏网场景）。
        for fresh in (False, True):
            conn = None
            try:
                conn = (_new_https_conn(host, port) if fresh
                       else (_pool_take(host, port) or _new_https_conn(host, port)))
                conn.request('GET', req_path, headers=req_headers)
                resp = conn.getresponse()
            except Exception:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                continue
            # 3xx（中转漏网）：http.client 不自动跟随，回退 urllib
            if 300 <= resp.status < 400:
                try:
                    resp.close()
                    conn.close()
                except Exception:
                    pass
                break
            return resp, (host, port, conn)

        # 回退 urllib
        resp = client.http_open('GET', fetch_url, headers=headers, timeout=UPSTREAM_TIMEOUT)
        return resp, None

    def _resolve_stream(self, media_guid, prefer_cloud=False):
        """决定视频流的上游地址与附加请求头

        返回 (target_url, headers, 错误, 是否云盘直链)。
        NAS 直连优先（media/range 无服务端扫描开销）；
        仅当 prefer_cloud=True（NAS 直连失败后的回退）且开启云盘直链时，
        才调用 POST /stream 获取云盘直链——该接口会触发服务端
        "获取文件视频信息"扫描，不能作为默认路径。
        """
        client = get_client()
        if client is None:
            return None, None, 'proxy 未初始化客户端', False

        fwd = {}
        range_header = self.headers.get('Range')
        if range_header:
            fwd['Range'] = range_header
        # If-Range 与 Range 配套：上游内容不变时保证断点续传一致性
        if_range = self.headers.get('If-Range')
        if if_range:
            fwd['If-Range'] = if_range

        target_url = client.get_video_url(media_guid)
        extra = {
            'Authorization': client.token,
            'Cookie': 'mode=relay',
        }
        is_cloud = False

        if prefer_cloud and _use_cloud_direct():
            try:
                # 代理可自行注入响应头里的 UA/Cookie；统一用默认（播放端
                # 完整）UA 换链，与直链路径的缓存保持一致
                stream_info = client.stream_cached(media_guid)
                direct = (stream_info.get('direct_link_qualities') or [])
                header = stream_info.get('header') or {}
                if direct and direct[0].get('url'):
                    target_url = direct[0]['url']
                    extra = {}
                    cookies = header.get('Cookie') or []
                    if cookies:
                        extra['Cookie'] = '; '.join(cookies)
                    for ua in (header.get('User-Agent') or []):
                        ua = (ua or '').strip()
                        if ua:
                            extra['User-Agent'] = ua
                            break
                    is_cloud = True
                    util.debug('使用云盘直链: %s' % target_url[:120])
            except Exception as e:
                util.log('获取云盘直链失败，回退 NAS 转发: %s' % e)

        headers = {}
        headers.update(fwd)
        headers.update(extra)
        return target_url, headers, None, is_cloud

    # -------------------------------------------------------------- 请求处理

    def do_HEAD(self):
        self._handle(body=False)

    def do_GET(self):
        self._handle(body=True)

    def _handle(self, body=True):
        try:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split('/') if p]

            # 捕获播放端真实 UA（Kodi/FastVFS 的完整 UA 串）：换链声明用，
            # 使网盘直链与播放端默认 UA 天然一致（/stream 来自视频端，优先）
            util.capture_client_ua(self.headers.get('User-Agent'),
                                   is_video=bool(parts and parts[0] == 'stream'))

            if parts and parts[0] == 'ping':
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', '13')
                self.end_headers()
                if body:
                    self.wfile.write(b'fnmedia-proxy')
            elif parts and parts[0] == 'stream' and len(parts) >= 2:
                # parts[2]（如有）是装饰性文件名，仅用于 Content-Type 补全
                name = unquote(parts[2]) if len(parts) >= 3 else ''
                if body:
                    self._proxy_stream(parts[1], True, name)
                else:
                    self._head_stream(parts[1], name)
            elif parts and parts[0] == 'image':
                qs = parse_qs(parsed.query)
                url = unquote((qs.get('url') or [''])[0])
                if not url:
                    self._send_upstream_error(400, 'missing url')
                    return
                self._proxy_image(url, body)
            else:
                self._send_upstream_error(404, 'not found: %s' % parsed.path)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Kodi seek 中断连接属正常现象
        except Exception as e:
            util.log('代理处理异常: %s' % e, xbmc.LOGERROR)
            try:
                self._send_upstream_error(502, str(e))
            except Exception:
                pass

    def _head_stream(self, media_guid, filename=''):
        """HEAD/Stat：即时返回，绝不打上游/扫描。

        命中缓存 size → 200 + Content-Length + Accept-Ranges；未命中 → 200 不带
        Content-Length（Kodi 照常走 GET 取真实大小，如 09:44 Stat 超时后仍 OpenFile
        可证）。同时后台预热云链，使紧随其后的 GET 命中缓存免去扫描。全程不取
        _cloud_slots、不阻塞——杜绝冷启动 Stat Timeout(28) 与暖启动每次的 NAS 400
        +云链 resolve+上游 open 开销。
        """
        _maybe_bg_resolve(media_guid)
        size = _get_cached_size(media_guid)
        guessed = _guess_mime(filename)
        try:
            self.send_response(200)
            self.send_header('Accept-Ranges', 'bytes')
            if guessed:
                self.send_header('Content-Type', guessed)
            if size:
                self.send_header('Content-Length', str(size))
            else:
                self.send_header('Connection', 'close')
            self.end_headers()
            util.debug('HEAD size=%s media=%s' % (size or 'unknown', media_guid[:16]))
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端在 HEAD 后立即发 GET，可能复用/关闭连接
        except Exception as e:
            util.log('HEAD 响应异常: %s' % e, xbmc.LOGWARNING)

    def _proxy_stream(self, media_guid, send_body, filename=''):
        client = get_client()
        if client is None:
            self._send_upstream_error(500, 'client not ready')
            return

        # 注意：上游一律用 GET——真机验证 NAS 的 media/range 不支持 HEAD（返回 404），
        # 而 Kodi 会先发 HEAD 探测。HEAD 请求转发为 GET，取到响应头后丢弃响应体。
        resp = None
        held_slot = False          # 是否持有云盘并发槽位（整个响应期间持有）
        conn_handle = None         # 池化上游连接（host,port,conn），None 表示走 urllib
        # NAS 400 负缓存命中 → 已知仅云盘，直接走直链
        tried_cloud = _nas_only(media_guid)
        try:
            for attempt in range(3):
                target_url, headers, err, is_cloud = self._resolve_stream(
                    media_guid, prefer_cloud=tried_cloud)
                if err:
                    self._send_upstream_error(500, err)
                    return

                # 中转型直链：命中最终地址缓存则直连 CDN，不再每请求打中转
                resolved_final = _get_final_url(media_guid, target_url) if is_cloud else None
                fetch_url = resolved_final or target_url
                if resolved_final:
                    util.debug('命中最终直链缓存，跳过中转: %s' % resolved_final[:120])

                if is_cloud and not held_slot:
                    if not _cloud_slots.acquire(timeout=CLOUD_QUEUE_TIMEOUT):
                        self._send_upstream_error(503, 'cloud queue busy')
                        return
                    held_slot = True

                try:
                    resp, conn_handle = self._open_upstream(client, fetch_url, headers, is_cloud)
                    status = getattr(resp, 'status', None) or resp.getcode()
                except urllib.error.HTTPError as e:
                    resp, status = e, e.code
                except urllib.error.URLError as e:
                    self._send_upstream_error(502, '上游请求失败: %s' % e.reason)
                    return
                except Exception as e:
                    self._send_upstream_error(502, '上游请求失败: %s' % e)
                    return

                # NAS 直连失败（云盘 strm 等非本地文件）→ 记负缓存并转云盘直链
                if status >= 400 and not tried_cloud and _use_cloud_direct():
                    util.debug('NAS 直连 %d，转云盘直链（media=%s）' % (status, media_guid[:16]))
                    try:
                        resp.close()
                    except Exception:
                        pass
                    resp = None
                    _discard_conn(conn_handle); conn_handle = None
                    if status in (400, 403, 404):
                        _mark_nas_fail(media_guid)
                    tried_cloud = True
                    continue

                # 云盘直链过期（401/403）→ 失效缓存重取一次；退避避免瞬时
                # 并发拒绝演变成 POST /stream 风暴（每次都是服务端扫描）
                if status in (401, 403) and tried_cloud and attempt < 2:
                    util.log('云盘直链 %d，失效重取（%s）' % (
                        status, self._upstream_host(resp, fetch_url)), xbmc.LOGWARNING)
                    try:
                        resp.close()
                    except Exception:
                        pass
                    resp = None
                    _discard_conn(conn_handle); conn_handle = None
                    client.invalidate_stream_cache(media_guid)
                    _forget_final_url(media_guid)
                    time.sleep(0.5)
                    continue

                # 首次健康响应且发生了 302 跳转 → 记住最终地址，后续请求直连
                if is_cloud and not resolved_final and status < 400:
                    final = self._resp_final_url(resp)
                    if final and final != fetch_url:
                        _remember_final_url(media_guid, target_url, final)
                break

            if resp is None:
                self._send_upstream_error(502, 'no upstream response')
                return

            status = getattr(resp, 'status', None) or resp.getcode()
            if status >= 400:
                util.log('上游 %s 返回 %d，已回传播放器（media=%s）' % (
                    self._upstream_host(resp, fetch_url), status, media_guid[:16]),
                    xbmc.LOGWARNING)
                self._send_upstream_error(status, '上游返回 %d' % status)
                return

            # 缓存文件总大小，供后续 HEAD/Stat 即时返回（避免每次探测都打上游）
            _size = _extract_size(resp)
            if _size:
                _set_cached_size(media_guid, _size)

            self.send_response(status)
            out_headers = {}
            if resp.headers:
                for name in FORWARD_HEADERS:
                    value = resp.headers.get(name)
                    if value:
                        out_headers[name] = value
            # 上游没给有效 Content-Type 时按文件名后缀补全（部分播放器依赖它识别容器）
            ctype = (out_headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if (not ctype or ctype == 'application/octet-stream') and filename:
                guessed = _guess_mime(filename)
                if guessed:
                    out_headers['Content-Type'] = guessed
            for name, value in out_headers.items():
                self.send_header(name, value)
            if not (resp.headers and resp.headers.get('Content-Length')):
                self.send_header('Connection', 'close')
            self.end_headers()

            drained = False
            if not send_body:
                return  # HEAD：只要响应头，响应体由 close 丢弃（drained=False→连接丢弃）

            sent = 0
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    drained = True
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
            if sent == 0:
                # 200 却 0 字节：上游/中转抽风的典型形态（真机坑⑮），必须留痕
                util.log('上游 %s 返回 %d 但响应体 0 字节（media=%s）' % (
                    self._upstream_host(resp, fetch_url), status, media_guid[:16]),
                    xbmc.LOGWARNING)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端断开（seek/停止）
        except Exception as e:
            util.log('流转发异常: %s' % e, xbmc.LOGWARNING)
        finally:
            # 池化连接：仅当干净读尽（drained）且服务端允许 keep-alive
            # （will_close=False）才放回池复用；中断/未读尽/Connection:close 一律丢弃
            reusable = False
            try:
                if resp is not None:
                    if conn_handle is not None:
                        reusable = drained and not getattr(resp, 'will_close', True)
                    resp.close()
            except Exception:
                pass
            if conn_handle is not None:
                host, port, conn = conn_handle
                _pool_put(host, port, conn, reusable=reusable)
            # 整个响应转发结束才释放云盘槽位：保证对云盘直链的并发连接数
            # 始终 ≤ CLOUD_CONCURRENCY，不触发 115 的并发 403
            if held_slot:
                _cloud_slots.release()

    def _proxy_image(self, url, send_body):
        client = get_client()
        if client is None:
            self._send_upstream_error(500, 'client not ready')
            return
        headers = {'Cookie': 'mode=relay'}
        if client.token:
            headers['Authorization'] = client.token
        # 图片走 /v/api/v1/sys/img 接口，需要 Authx 签名（真机验证）
        if '/v/api/v1/' in url:
            parsed = urlparse(url)
            path = parsed.path + ('?' + parsed.query if parsed.query else '')
            try:
                headers['Authx'] = client.authx_for(path)
            except Exception:
                pass
        try:
            resp = client.http_open('GET', url, headers=headers, timeout=30)
        except urllib.error.HTTPError as e:
            self._send_upstream_error(e.code, 'image %d' % e.code)
            return
        except Exception as e:
            self._send_upstream_error(502, 'image fetch failed: %s' % e)
            return

        try:
            status = getattr(resp, 'status', None) or resp.getcode()
            content = resp.read()
            self.send_response(status)
            for name in ('Content-Type', 'Content-Length', 'Cache-Control'):
                value = resp.headers.get(name) if resp.headers else None
                if value:
                    self.send_header(name, value)
            if content:
                self.send_header('Content-Length', str(len(content)))
            self.send_header('Connection', 'close')
            self.end_headers()
            if send_body and content:
                self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass
