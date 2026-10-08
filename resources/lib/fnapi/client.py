# -*- coding: utf-8 -*-
"""飞牛影视 API 客户端

移植自 fntv-electron src/modules/fn_api/api.ts + request.ts

响应包裹格式：{"code": 0, "msg": "", "data": {...}}，code==0 为成功。

网络层使用 Python 标准库 urllib（无任何外部依赖，
安装插件时不需要从 Kodi 仓库联网拉取 script.module.requests 等包）。
"""
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import xbmc

from resources.lib import util
from resources.lib.fnapi.auth import gen_authx, dumps_compact, generate_nonce, string_to_uuid

CODE_INVALID_SIGN = 5000
MAX_SIGN_RETRY = 5
STREAM_CACHE_TTL = 300  # 秒
CLOUD_LINK_CACHE_FILE = 'cloud_link_cache.json'
ITEM_LIST_CACHE_FILE = 'item_list_cache.json'
ITEM_LIST_CACHE_TTL = 300  # 秒；列表内 watched 等状态最长滞后该时长
USER_AGENT = 'fntv-kodi/0.1.1'

# item/list 分页（真机验证 2026-09）：服务端支持 page（1 起始）/page_size，
# 且不带分页参数时默认 page_size=500 会静默截断（1324 条的库只返回 500 条）。
WALK_PAGE_SIZE = 500
WALK_MAX_PAGES = 100     # 单范围安全上限（500×100=5 万条）
WALK_WORKERS = 3         # 并行页数：实测服务端查询吞吐是瓶颈，3×500 墙钟 ~2s
                         # 优于单请求大页（page_size=2000 实测 ~3s）


def _default_stream_ua():
    """换链声明的默认 UA：播放端（Kodi 原生 curl / vfs.stream.fast）实际
    发送的完整 UA 串（由本地代理从真实请求捕获）。网盘直链与换链 UA 精确
    绑定，声明什么 UA 拿到的直链就要求什么 UA——声明播放端自己的完整 UA，
    直链即与播放端默认行为天然一致，无需注入；未捕获到时退回短串。"""
    return util.get_client_ua() or util.kodi_user_agent()


class ApiError(Exception):
    def __init__(self, message, code=None):
        super(ApiError, self).__init__(message)
        self.code = code
        self.message = message


def _ssl_context(verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _response_status(resp):
    return getattr(resp, 'status', None) or resp.getcode()


def _origin(url):
    parsed = urlparse(url)
    return '%s://%s' % (parsed.scheme, parsed.netloc)


class FnClient(object):
    """飞牛影视 API 客户端（带登录态管理与流信息缓存）"""

    _stream_locks = {}            # media_guid -> Lock（直链解析单飞；类级共享）
    _stream_locks_guard = threading.Lock()

    def __init__(self, base_url, verify=True, timeout=30):
        self.base = (base_url or '').rstrip('/')
        self.verify = verify
        self.timeout = timeout
        self.token = ''
        self.username = ''
        self.password = ''
        self.ssl_context = _ssl_context(verify)
        self._stream_cache = {}       # (media_guid, ua) -> (data, expire_ts)
        self._item_list_mem = {}      # (parent_guid, ...) -> (data, expire_ts)
        self._playable_neg = {}       # media_guid -> 负缓存到期 ts（探测失败短时避免反复探测）
        self._token_checked = False
        self.on_token_refresh = None  # 回调 token 变化时触发
        self.on_base_change = None    # 回调重定向导致 base 变化时触发

    # ------------------------------------------------------------------ 基础

    def set_credentials(self, username='', password='', token=''):
        self.username = username or ''
        self.password = password or ''
        self.token = token or ''

    def _save_token(self, token):
        self.token = token
        self._token_checked = True
        if self.on_token_refresh:
            try:
                self.on_token_refresh(token)
            except Exception:
                pass

    # ------------------------------------------------------------------ HTTP

    def http_open(self, method, url, headers=None, data=None, timeout=None):
        """发起 HTTP 请求，返回可流式读取的响应对象（.status/.headers/.read(n)/.close()）

        3xx 重定向由 urllib 自动跟随（保留自定义头，对应 TS 客户端 moveUrl 行为）。
        4xx/5xx 抛 urllib.error.HTTPError（其本身可 read()）。
        """
        final_headers = {
            'User-Agent': USER_AGENT,
            'Accept-Encoding': 'identity',   # 不做 gzip，避免手动解压
        }
        if headers:
            final_headers.update(headers)
        req = urllib.request.Request(
            url, data=data, headers=final_headers, method=method.upper(),
        )
        return urllib.request.urlopen(
            req, timeout=timeout or self.timeout, context=self.ssl_context,
        )

    # ------------------------------------------------------------------ 请求

    def call(self, method, path, data=None, raw=False, timeout=None):
        """执行 API 请求

        :param method: 'get' / 'post'
        :param path:   以 /v/api/v1 开头的路径（签名用，须与实际请求一致）
        :param data:   POST 的业务数据 dict（nonce 会自动附加）
        :param raw:    True 时非 JSON 响应直接返回字节（字幕下载用）
        :param timeout: 本次请求的超时秒数（默认用客户端全局值）
        """
        method = method.lower()

        # POST/PUT 附加防重放 nonce，签名覆盖完整 body
        payload = data
        body_str = None
        if method in ('post', 'put'):
            payload = dict(data or {})
            payload['nonce'] = generate_nonce()
            body_str = dumps_compact(payload)

        last_error = None
        for sign_attempt in range(MAX_SIGN_RETRY + 1):
            headers = {
                'Content-Type': 'application/json',
                'Cookie': 'mode=relay',
                'Authx': gen_authx(path, body_str),
            }
            if self.token:
                headers['Authorization'] = self.token

            try:
                resp = self.http_open(
                    method, self.base + path, headers=headers,
                    data=body_str.encode('utf-8') if body_str else None,
                    timeout=timeout,
                )
                error_resp = None
            except urllib.error.HTTPError as e:
                resp, error_resp = e, e
            except urllib.error.URLError as e:
                reason = e.reason
                if isinstance(reason, ssl.SSLError):
                    raise ApiError('证书验证失败：%s（自签名证书请在插件设置中关闭"验证服务器证书"）' % reason)
                if isinstance(reason, (socket.timeout, ConnectionError, OSError)):
                    raise ApiError('无法连接服务器 %s：%s' % (self.base, reason))
                raise ApiError('请求失败：%s' % reason)
            except socket.timeout:
                raise ApiError('请求超时：%s' % path)

            try:
                status = _response_status(resp)

                # 令牌类错误：重新登录一次
                if error_resp is not None and status in (401, 403) and self._can_relogin():
                    self._relogin()
                    return self.call(method, path, data, raw, timeout)

                # 跟随重定向后若 origin 变化，更新 base（对应 TS 客户端 moveUrl 逻辑）
                final_url = resp.geturl() if hasattr(resp, 'geturl') else None
                if final_url and _origin(final_url) != _origin(self.base + path):
                    new_base = _origin(final_url)
                    if new_base != self.base:
                        self.base = new_base
                        util.log('服务器重定向，更新 base -> %s' % new_base)
                        if self.on_base_change:
                            try:
                                self.on_base_change(new_base)
                            except Exception:
                                pass

                content_type = resp.headers.get('Content-Type', '') if resp.headers else ''
                body_bytes = resp.read()
                if error_resp is None and raw and 'application/json' not in content_type:
                    return body_bytes

                if error_resp is not None:
                    raise ApiError('HTTP %d：%s' % (status, body_bytes[:200] or ''), status)

                try:
                    envelope = json.loads(body_bytes.decode('utf-8'))
                except (ValueError, UnicodeDecodeError):
                    raise ApiError('HTTP %d：响应不是 JSON（可能地址错误或被反代拦截）' % status)

                code = envelope.get('code')
                msg = envelope.get('msg', '')

                if code == 0:
                    return envelope.get('data')

                # 签名错误：重新生成 nonce/时间戳重试
                if code == CODE_INVALID_SIGN and msg == 'invalid sign' and sign_attempt < MAX_SIGN_RETRY:
                    last_error = ApiError('invalid sign', code)
                    payload = dict(data or {})
                    payload['nonce'] = generate_nonce()
                    body_str = dumps_compact(payload)
                    continue

                # 令牌失效类错误：重新登录一次
                if self._is_auth_error(code, msg) and self._can_relogin():
                    self._relogin()
                    return self.call(method, path, data, raw, timeout)

                raise ApiError(msg or ('错误码 %s' % code), code)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        raise last_error or ApiError('invalid sign（重试后仍失败）', CODE_INVALID_SIGN)

    def _can_relogin(self):
        return bool(self.username and self.password)

    def _is_auth_error(self, code, msg):
        text = (msg or '').lower()
        keywords = ('token', 'auth', 'unauthorized', 'login', 'expire', '登录', '授权', '令牌')
        return any(k in text for k in keywords)

    def _relogin(self):
        util.log('令牌疑似失效，尝试重新登录')
        self._token_checked = False
        self.login()

    # ------------------------------------------------------------------ 认证接口

    def login(self):
        """账号密码登录，成功后返回 token 并触发回调"""
        data = self.call('post', '/v/api/v1/login', {
            'app_name': 'trimemedia-web',
            'username': self.username,
            'password': self.password,
        })
        token = (data or {}).get('token')
        if not token:
            raise ApiError('登录成功但未返回 token')
        self._save_token(token)
        return token

    def ensure_token(self):
        """确保拥有有效 token：无 token 则登录；有则仅在未校验过时校验一次"""
        if not self.token:
            if not self._can_relogin():
                raise ApiError('未配置账号密码，且无可用令牌，请先在插件设置中配置')
            self.login()
            return self.token
        if not self._token_checked:
            try:
                self.call('get', '/v/api/v1/user/info')
                self._token_checked = True
            except ApiError:
                if not self._can_relogin():
                    raise
                self._relogin()
        return self.token

    def user_info(self):
        return self.call('get', '/v/api/v1/user/info')

    # ------------------------------------------------------------------ 浏览接口

    def item_list(self, parent_guid='', exclude_folder=0, sort_column='sort_title',
                  sort_type='ASC', ancestor_guid='', page=None, page_size=None):
        """列出目录/媒体库下的项目

        真机验证（fnOS 0.4.x，2026-09 复测）：
          - parent_guid 为空返回全库顶层（Movie+TV+Directory 混合）；
          - ancestor_guid 为有效的服务端按库过滤（total 精确、页内零串库）；
            mdb_guid/category/type 等仍被服务端忽略；
          - page（1 起始）+ page_size 分页生效；两者都不传时服务端默认
            page_size=500——大库会被静默截断，全量拉取务必走 item_list_walk。
        """
        body = {
            'parent_guid': parent_guid,
            'exclude_folder': exclude_folder,
            'sort_column': sort_column,
            'sort_type': sort_type,
        }
        if ancestor_guid:
            body['ancestor_guid'] = ancestor_guid
        if page:
            body['page'] = page
        if page_size:
            body['page_size'] = page_size
        return self.call('post', '/v/api/v1/item/list', body) or {}

    def item_list_walk(self, ancestor_guid='', parent_guid='', exclude_folder=0,
                       sort_column='sort_title', sort_type='ASC',
                       page_size=WALK_PAGE_SIZE):
        """分页并行拉取全量列表（整库直出的数据源，修复默认 500 条截断）。

        首页请求拿到 total，其余页并行拉取后按页序合并（顺序与服务端
        排序一致）。真机实测（1323 条库）：3×500 并行墙钟 ~2s，优于单
        请求大页（page_size=2000 ~3s）——瓶颈在服务端查询吞吐，并发不必
        开大。注意 parent_guid 非空（文件夹视图）时服务端忽略分页参数、
        一次返回全部子项，此场景无需 walk。
        """
        first = self.item_list(parent_guid, exclude_folder, sort_column, sort_type,
                               ancestor_guid=ancestor_guid, page=1, page_size=page_size)
        entries = list(first.get('list') or [])
        try:
            total = int(first.get('total') or 0)
        except (TypeError, ValueError):
            total = 0
        if total > len(entries):
            last_page = min((total + page_size - 1) // page_size, WALK_MAX_PAGES)

            def _page(p):
                d = self.item_list(parent_guid, exclude_folder, sort_column, sort_type,
                                   ancestor_guid=ancestor_guid, page=p, page_size=page_size)
                return d.get('list') or []

            with ThreadPoolExecutor(max_workers=WALK_WORKERS) as pool:
                for part in pool.map(_page, range(2, last_page + 1)):
                    entries.extend(part)
        data = dict(first)
        data['list'] = entries
        data['total'] = total or len(entries)
        return data

    def item_list_cached(self, parent_guid='', exclude_folder=0,
                         sort_column='sort_title', sort_type='ASC'):
        """带短 TTL 缓存的 item_list（大库全量拉取优化）。

        全库 item_list 数据量大且每次进列表都要拉（真机大库明显卡）。
        双层缓存：进程内存 + 落盘（Kodi 每次进列表都是新进程，落盘配合
        util.load_json_cached 的 mtime 校验让每个进程仅首次读盘解析）。
        TTL 内列表直接命中，进程内零读盘、零网络。
        注意：列表内 watched/playcount 等状态最长滞后 TTL——改服务端状态
        的路径（标记已观看、播放结束回传）须调 invalidate_item_list_cache()。
        网络失败时回退落盘的过期数据（服务器短暂不可达仍能进列表）。
        """
        key = (parent_guid, exclude_folder, sort_column, sort_type)
        key_str = '|'.join(str(x) for x in key)
        now = time.time()
        hit = self._item_list_mem.get(key)
        if hit and hit[1] > now:
            return hit[0]

        disk = util.load_json_cached(ITEM_LIST_CACHE_FILE)
        entry = disk.get(key_str)
        if entry and entry.get('base') == self.base \
                and now - entry.get('ts', 0) < ITEM_LIST_CACHE_TTL and entry.get('data'):
            data = entry['data']
            self._item_list_mem[key] = (data, now + ITEM_LIST_CACHE_TTL)
            util.debug('item_list 缓存命中: %s' % (parent_guid or '全库'))
            return data

        try:
            data = self.item_list(parent_guid, exclude_folder, sort_column, sort_type)
        except Exception:
            # 网络失败：有过期数据则顶上（陈旧总比白屏好），否则原样抛出
            if entry and entry.get('base') == self.base and entry.get('data'):
                util.log('item_list 拉取失败，回退过期缓存: %s' % (parent_guid or '全库'))
                data = entry['data']
            else:
                raise

        self._item_list_mem[key] = (data, now + ITEM_LIST_CACHE_TTL)

        # 写回磁盘（写路径用 load_json 取独立副本，避免污染共享缓存；仅保留
        # 最近 20 个条目防膨胀）
        disk_w = util.load_json(ITEM_LIST_CACHE_FILE)
        disk_w = disk_w if isinstance(disk_w, dict) else {}
        disk_w[key_str] = {'ts': now, 'base': self.base, 'data': data}
        if len(disk_w) > 20:
            ordered = sorted(disk_w.items(), key=lambda kv: kv[1].get('ts', 0))
            disk_w = dict(ordered[-20:])
        util.save_json(ITEM_LIST_CACHE_FILE, disk_w)
        return data

    def invalidate_item_list_cache(self):
        """清空列表缓存（服务端 watched/条目状态变化后调用，避免 TTL 内滞后）"""
        self._item_list_mem.clear()
        util.save_json(ITEM_LIST_CACHE_FILE, {})

    def mediadb_list(self):
        """媒体库列表（首页侧栏数据源）"""
        return self.call('get', '/v/api/v1/mediadb/list') or []

    def season_list(self, tv_guid):
        """列出某剧集的季（真机验证返回 [{guid,type:Season,season_number,title,...}]）"""
        return self.call('get', '/v/api/v1/season/list/%s' % tv_guid) or []

    def episode_list(self, parent_guid):
        """列出某季下的剧集"""
        return self.call('get', '/v/api/v1/episode/list/%s' % parent_guid) or []

    def item_detail(self, guid):
        """单个项目详情（比列表多 logos/backdrops/original_title 等字段）"""
        return self.call('get', '/v/api/v1/item/%s' % guid) or {}

    # ------------------------------------------------------------------ 图片

    def image_url(self, poster_path, width=400):
        """把服务端内部海报路径转换为可请求的图片 URL

        真机验证规则：{base}/v/api/v1/sys/img{poster_path}?w=400
        需要 Authx 签名 + Authorization 头（经本地代理转发）。
        ?w= 缩放参数：原图约 1.5MB，w=400 约 28KB。
        """
        if not poster_path:
            return None
        if isinstance(poster_path, (list, tuple)):
            # mediadb 层 posters 是列表（调用方取 [0]），详情层的 backdrops/
            # logos 等字段也存在列表误传的可能：统一取首项，避免
            # str(list) 拼出必败 URL 还每轮重试
            poster_path = poster_path[0] if poster_path else None
            if not poster_path:
                return None
        poster_path = str(poster_path)
        if poster_path.startswith('http'):
            return poster_path
        if not poster_path.startswith('/'):
            poster_path = '/' + poster_path
        url = '%s/v/api/v1/sys/img%s' % (self.base, poster_path)
        if width:
            url += '?w=%d' % width
        return url

    def authx_for(self, full_path):
        """为任意 API 路径生成 Authx 头（图片代理用，含查询串）"""
        return gen_authx(full_path, None)

    # ------------------------------------------------------------------ 播放接口

    def play_info(self, item_guid):
        """播放信息（含断点 ts、默认流 guid、元数据）"""
        return self.call('post', '/v/api/v1/play/info', {'item_guid': item_guid}) or {}

    def stream_list(self, item_guid, timeout=60):
        """流列表（files / video_streams / audio_streams / subtitle_streams）

        真机验证：服务端对未缓存过的 4K 大文件可能耗时 25 秒以上（预扫描），
        因此默认给 60 秒超时；播放主流程用后台线程短等待兜底。
        """
        return self.call('get', '/v/api/v1/stream/list/%s' % item_guid, timeout=timeout) or {}

    def stream(self, media_guid, user_agent=None):
        """直链流信息（qualities / direct_link_qualities / 所需请求头）

        :param user_agent: 向服务端声明的下载端 UA——网盘直链通常与换链 UA
            绑定，声明什么 UA 拿到的直链就要求什么 UA。默认用播放端的完整
            UA 串（Kodi 原生 curl 与 vfs.stream.fast 默认发送的正是它），
            直链无需注入即可直连。
        """
        if not user_agent:
            user_agent = _default_stream_ua()
        return self.call('post', '/v/api/v1/stream', {
            'header': {'User-Agent': [user_agent]},
            'level': 1,
            'media_guid': media_guid,
            'ip': string_to_uuid(self.username),
        }) or {}

    def stream_cached(self, media_guid, user_agent=None):
        """带缓存的 stream()

        双层缓存：进程内存（按 media_guid+UA 分键，不同 UA 绑定的直链互不
        覆盖）+ 落盘（跨插件调用复用，直链按 expired_at 提前 60 秒失效；
        磁盘键为 media_guid，后一次解析覆盖，绑定 UA 以 data.header 回显
        为准、由探测阶段自洽校验）。云盘直链解析会触发服务端"获取文件
        视频信息"扫描，落盘缓存把每次播放会话一次的解析降为每个有效期
        周期一次。并发请求同一直链时单飞：等第一个请求解析完成复用结果，
        避免 BDMV 挂载的并发读同时打出 N 个 POST /stream（N 次服务端扫描）。
        """
        ua = user_agent or _default_stream_ua()
        now = time.time()
        hit = self._stream_cache.get((media_guid, ua))
        if hit and hit[1] > now:
            return hit[0]

        with FnClient._stream_locks_guard:
            lock = FnClient._stream_locks.get(media_guid)
            if lock is None:
                lock = threading.Lock()
                FnClient._stream_locks[media_guid] = lock
        with lock:
            # 双重检查：等锁期间其他线程可能已完成解析
            hit = self._stream_cache.get((media_guid, ua))
            if hit and hit[1] > time.time():
                return hit[0]

            # 磁盘缓存（跨进程）
            disk = util.load_json(CLOUD_LINK_CACHE_FILE) or {}
            entry = disk.get(media_guid)
            if entry and entry.get('expire', 0) > time.time() and entry.get('data'):
                data = entry['data']
                self._stream_cache[(media_guid, ua)] = (data, entry['expire'])
                util.debug('云盘直链磁盘缓存命中: %s' % media_guid[:16])
                return data

            data = self.stream(media_guid, user_agent=ua)

            expiries = []
            for quality in (data.get('direct_link_qualities') or []):
                expired_at = quality.get('expired_at') or 0
                if expired_at > 1e12:      # 毫秒时间戳
                    expired_at /= 1000.0
                if expired_at > 0:
                    expiries.append(expired_at)
            expire = (min(expiries) - 60) if expiries else (time.time() + STREAM_CACHE_TTL)

            self._stream_cache[(media_guid, ua)] = (data, expire)

            # 写回磁盘（保留最近 100 条防膨胀；并发写竞争用整体覆盖容忍）
            disk[media_guid] = {'data': data, 'expire': expire}
            if len(disk) > 100:
                ordered = sorted(disk.items(), key=lambda kv: kv[1].get('expire', 0))
                disk = dict(ordered[-100:])
            util.save_json(CLOUD_LINK_CACHE_FILE, disk)
            return data

    def invalidate_stream_cache(self, media_guid=None):
        if media_guid:
            for key in list(self._stream_cache.keys()):
                if key[0] == media_guid:
                    self._stream_cache.pop(key, None)
            self._playable_neg.pop(media_guid, None)
            disk = util.load_json(CLOUD_LINK_CACHE_FILE) or {}
            if media_guid in disk:
                disk.pop(media_guid, None)
                util.save_json(CLOUD_LINK_CACHE_FILE, disk)
        else:
            self._stream_cache.clear()
            self._playable_neg.clear()
            util.save_json(CLOUD_LINK_CACHE_FILE, {})

    def _direct_pipe_headers(self, data):
        """从流信息的 header 回显构造直连所需请求头（UA/Cookie，缺省不给）"""
        header = data.get('header') or {}
        headers = {}
        cookies = [c for c in (header.get('Cookie') or []) if c]
        if cookies:
            headers['Cookie'] = '; '.join(cookies)
        for ua in (header.get('User-Agent') or []):
            ua = (ua or '').strip()
            if ua:
                headers['User-Agent'] = ua
                break
        return headers

    def _probe_direct(self, url, headers, declared_ua=None):
        """用与播放完全一致的请求头探测直链（Range 0-0），仅接受 206。

        探测头 = 播放头（管道头；无头时即换链声明的完整 UA——播放端默认
        发送的就是它），零假设自洽——探测 206 则播放必 206，杜绝 0.2.18
        修复过的 206 假阳性缓存坏直链。
        返回 (是否可用, 跟随 302 后的最终地址或 None)。失败原因写 INFO
        日志（换链限频/直链吊销/限速等 115 风控场景需要留痕）。"""
        probe = {'Accept-Encoding': 'identity', 'Range': 'bytes=0-0'}
        probe.update(headers or {})
        probe.setdefault('User-Agent', declared_ua or util.kodi_user_agent())
        req = urllib.request.Request(url, headers=probe, method='GET')
        try:
            resp = urllib.request.urlopen(req, timeout=20, context=self.ssl_context)
        except urllib.error.HTTPError as e:
            util.log('直链探测被拒: HTTP %s（115 风控/直链吊销时常见，回退代理）'
                     % e.code, xbmc.LOGINFO)
            return False, None
        except Exception as e:
            util.log('直链探测异常: %s（回退代理）' % str(e)[:80], xbmc.LOGINFO)
            return False, None
        status = _response_status(resp)
        final = None
        try:
            final = resp.geturl()
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass
        # 200（不认 Range）同样放弃：直连后 seek 会退化为全量重下
        if status == 206:
            return True, (final if final and final != url else None)
        util.log('直链探测未通过: HTTP %s（仅接受 206，回退代理）' % status,
                 xbmc.LOGINFO)
        return False, None

    def _persist_direct_play(self, media_guid, play_url):
        """把探测成功的直连地址写回磁盘缓存，跨重启免重复探测（失败静默）"""
        try:
            disk = util.load_json(CLOUD_LINK_CACHE_FILE) or {}
            entry = disk.get(media_guid)
            if entry and entry.get('data') is not None:
                entry['data']['direct_play'] = play_url
                disk[media_guid] = entry
                util.save_json(CLOUD_LINK_CACHE_FILE, disk)
        except Exception:
            pass

    def resolve_playable_url(self, media_guid, min_valid_secs=0):
        """解析可交给 Kodi 播放器直连的网盘播放地址（统一网盘直链逻辑）。

        :param min_valid_secs: 直链剩余有效期低于该值则放弃直连——直连后
            链接中途过期无自愈手段（代理路径才有失效重取），调用方按
            max(2×片长, 1h) 传入；0 表示不设门控。

        流程：
          1. 换链声明播放端完整 UA（本地代理捕获，与 Kodi 原生 curl /
             vfs.stream.fast 默认发送的串一致），直链绑定同 UA，多数情况
             零注入直连；响应 header 回显的 UA/Cookie 以管道参数兜底附加。
          2. 自洽探测：用与播放完全相同的头发 Range 0-0，仅接受 206。
          3. 失败 → 60s 负缓存 → 返回 None，调用方回退本地代理
            （NAS 中转 / 云链头注入转发）。

        返回 dict 或 None：url=交给播放器的完整地址（可能带管道），
        base=去掉管道的最终直链（扩展名检查用），headers=直连所需请求头，
        expire=直链过期时间戳（0 未知）。
        """
        now = time.time()
        if self._playable_neg.get(media_guid, 0) > now:
            return None

        try:
            data = self.stream_cached(media_guid)
        except Exception as e:
            util.log('换链失败: %s' % e)
            self._playable_neg[media_guid] = now + 60
            return None

        direct = (data.get('direct_link_qualities') or [])
        if not direct or not direct[0].get('url'):
            return None    # NAS 本地文件等无直链 → 走代理

        # 有效期门控
        expiries = []
        for quality in direct:
            expired_at = quality.get('expired_at') or 0
            if expired_at > 1e12:       # 毫秒时间戳
                expired_at /= 1000.0
            if expired_at > 0:
                expiries.append(expired_at)
        expire = min(expiries) if expiries else 0
        if min_valid_secs > 0 and expire and expire - now < min_valid_secs:
            util.log('直链剩余有效期 %d 分钟不足 %d 分钟，放弃直连走代理（115 限速期链接'
                     '有效期变短时会集中出现）' % ((expire - now) / 60, min_valid_secs / 60),
                     xbmc.LOGINFO)
            return None

        headers = self._direct_pipe_headers(data)

        # 命中：此前探测过且直链未过期（stream_cached 保证 data 有效）
        cached_play = data.get('direct_play')
        if cached_play:
            return {'url': cached_play, 'base': util.strip_pipe_url(cached_play),
                    'headers': headers, 'expire': expire}

        ok, final_url = self._probe_direct(direct[0]['url'], headers,
                                           declared_ua=_default_stream_ua())
        if not ok:
            self._playable_neg[media_guid] = now + 60
            return None

        play_url = (final_url or direct[0]['url']) + util.build_pipe_options(headers)
        data['direct_play'] = play_url
        self._persist_direct_play(media_guid, play_url)
        return {'url': play_url, 'base': util.strip_pipe_url(play_url),
                'headers': headers, 'expire': expire}

    def get_video_url(self, media_guid):
        """NAS 本地文件直链（需 Authorization 头）"""
        return '%s/v/api/v1/media/range/%s' % (self.base, media_guid)

    def download_subtitle(self, subtitle_guid):
        """下载字幕原始字节"""
        return self.call('get', '/v/api/v1/subtitle/dl/%s' % subtitle_guid, raw=True)

    def set_watched(self, item_guid):
        return self.call('post', '/v/api/v1/item/watched', {'item_guid': item_guid})

    def play_record(self, item_guid, media_guid, video_guid, audio_guid,
                    subtitle_guid, play_link, ts, duration):
        """回传播放进度"""
        return self.call('post', '/v/api/v1/play/record', {
            'item_guid': item_guid,
            'media_guid': media_guid,
            'video_guid': video_guid,
            'audio_guid': audio_guid,
            'subtitle_guid': subtitle_guid,
            'play_link': play_link,
            'ts': int(ts),
            'duration': int(duration),
        })
