# -*- coding: utf-8 -*-
"""原生媒体库同步：把飞牛影视的影片/剧集以 strm+nfo+本地图片 同步进 Kodi 原生库

设计要点：
  - strm 内容指向本插件（action=play&guid=…），播放时实时换链。115 直链
    有效期仅约 40 分钟，任何把裸直链写进媒体库的方案都会大面积失效，
    "播放时解析"天然免疫——这也是不采用 Jellyfin 直连模式的原因。
  - 元数据（NFO）与图片（海报/背景/清晰标志/单集剧照）全部同步为本地
    文件：NFO 引用本地路径，Kodi 原生库完全离线可用。服务器 sys/img
    需要 Authx 签名，Kodi 直接抓取会失败，因此图片在同步时经本地代理
    下载落盘（并行、增量、已存在跳过）。
  - 幂等增量：内容无变化的文件不重写；manifest 记录本轮文件集，删除
    消失条目并触发 CleanLibrary。
  - 失败防御：剧集结构抓取失败/集数异常为空时沿用旧结构，且当轮跳过
    该剧清理——网络抖动不再把整部剧"清出库"；图片下载连续无响应即
    熔断中止本轮，服务端宕机不再把同步线程挂住数天。
  - 剧集结构：剧 → tvshow.nfo；季 → 子目录；集 → SxxEyy.strm + nfo。
    季集数据带 24h 读缓存（libsync_tvwalk.json），同步温和不刷接口。
  - 扫库触发：JSON-RPC TCP 9090 优先（service 上下文可靠，不依赖 GUI），
    executebuiltin + scan_pending（进插件根目录补扫）兜底。
  - 演员头像 art 修复：Kodi 重扫对 actor art 只补缺不覆盖，旧版导入的
    必失败服务器 URL 记录会一直滞留——同步后直写 MyVideos DB 把已
    本地化的演员头像记录修正/补全为本地路径（幂等）。
"""
import os
import re
import threading
import time
import xml.sax.saxutils as _sax

import xbmc

from resources.lib import util, desc, meta

LIB_ROOT_NAME = 'library'
MOVIE_DIR = 'movies'
TV_DIR = 'tvshows'
ART_DIR = 'art'
SYNC_TTL_DEFAULT = 12      # 默认同步周期（小时，可在设置中调整）
TV_WALK_TTL = 24 * 3600
TV_WALK_PRUNE_AFTER = 14 * 24 * 3600   # 已删剧的走缓存保留宽限（防范围文件临时缺失误删编号锁）
ART_WORKERS = 12
ART_ABORT_STREAK = 10                  # 连续 N 张图片下载完全无响应即熔断本轮
MANIFEST_FILE = 'libsync_manifest.json'
STATE_FILE = 'libsync_state.json'
TV_WALK_FILE = 'libsync_tvwalk.json'
SYNC_REQUEST_FILE = 'libsync_request.json'   # 立即同步请求（手动/首次配置）


def _log(msg, level=xbmc.LOGINFO):
    util.log('libsync: %s' % msg, level)


def _lib_root():
    return os.path.join(util.addon_data_dir(), LIB_ROOT_NAME)


# ------------------------------------------------------------------ 同步请求/状态

def _sync_ttl():
    """同步周期（设置项"同步周期"小时数，默认 12）"""
    try:
        hours = int(util.get_setting('libsync_interval', str(SYNC_TTL_DEFAULT))
                    or SYNC_TTL_DEFAULT)
    except ValueError:
        hours = SYNC_TTL_DEFAULT
    return max(hours, 1) * 3600


def _config_sig():
    """配置指纹：服务器 + 账号。变化意味着换了数据源，需整库重同步。"""
    return '|'.join([util.base_url(), util.get_setting('username') or ''])


def request_sync():
    """登记一次立即同步请求（service 常驻循环消费）。

    已有未消费的请求不覆盖（保留原时间戳，调用方多次触发只算一次）。"""
    if not util.load_json(SYNC_REQUEST_FILE):
        util.save_json(SYNC_REQUEST_FILE, {'ts': time.time()})


def pop_sync_request():
    """取走待处理的同步请求；返回是否存在（service 消费端调用）。"""
    if not util.load_json(SYNC_REQUEST_FILE):
        return False
    util.delete_file(SYNC_REQUEST_FILE)
    return True


def maybe_request_first_sync():
    """首次配置/切换服务器后自动登记一次立即同步（root 进入时调用）。

    以配置指纹记入 state.sig：从未同步过或指纹变化（换了服务器/账号，
    库内容要整体重建）即请求。播放中请求会保留，service 空闲后执行。"""
    if util.get_setting('libsync', 'true') != 'true':
        return
    if not util.get_setting('token'):
        return    # 尚未配置/未登录
    state = util.load_json(STATE_FILE) or {}
    if state.get('ts') and state.get('sig') == _config_sig():
        return
    request_sync()


def sync_status_label():
    """root 状态行文案。返回 (label, 缓存戳)；未配置/未开启返回 (None, 0)。

    缓存戳随同步状态变化（st= 进 root URL），Kodi 目录缓存随之失效，
    下次进入即显示最新概况。"""
    if util.get_setting('libsync', 'true') != 'true':
        return None, 0
    if not util.get_setting('token'):
        return None, 0
    if util.load_json(SYNC_REQUEST_FILE):
        return '媒体库同步：已排队，即将开始…', int(time.time() // 30)
    state = util.load_json(STATE_FILE) or {}
    ts = state.get('ts') or 0
    if not ts:
        return '媒体库同步：尚未同步（点击立即同步）', 0
    ago = int((time.time() - ts) // 60)
    if ago < 1:
        when = '刚刚'
    elif ago < 60:
        when = '%d 分钟前' % ago
    elif ago < 48 * 60:
        when = '%d 小时前' % (ago // 60)
    else:
        when = '%d 天前' % (ago // (24 * 60))
    return ('媒体库同步：%s · 电影 %d / 剧集 %d（点击立即同步）'
            % (when, state.get('movies') or 0, state.get('tv') or 0)), int(ts)


def _esc(text):
    return _sax.escape(str(text or ''), {'"': '&quot;'})


def _safename(name, guid):
    name = re.sub(r'[\\/:*?"<>|]', ' ', name or '').strip() or '未命名'
    return '%s_%s' % (name[:60].strip(), guid[:8])


def _season_no(ep):
    """季号：缺失/非法按 1；0（特集）保留为 0。目录/文件名/NFO 三处
    一致取值，特集归入 Kodi Specials——此前文件名 max(sn,1) 并入第 1
    季而 NFO 写 0，特集既错位又可能与正片撞集号。"""
    try:
        sn = int(ep.get('season_number'))
    except (TypeError, ValueError):
        return 1
    return sn if sn > 0 else 0


def _write(path, content):
    """内容无变化不重写。返回 'new'/'chg'/'same'/'err'"""
    existed = False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            existed = True
            if f.read() == content:
                return 'same'
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return 'chg' if existed else 'new'
    except OSError as e:
        _log('写文件失败 %s: %s' % (path, e), xbmc.LOGWARNING)
        return 'err'


def _play_url(guid):
    return 'plugin://%s/?action=play&guid=%s' % (util.ADDON_ID, guid)


# ------------------------------------------------------------------ 同步进度

class _SyncProgress(object):
    """同步进度显示：DialogProgressBG 角标进度（服务进程可用，不打断 GUI）。

    覆盖 收集清单 → 剧集结构 → 写入文件 → 下载图片 → 登记扫库 五个阶段；
    任一异常也要 close，避免角标残留。"""

    def __init__(self, message=''):
        self.dlg = None
        try:
            import xbmcgui
            self.dlg = xbmcgui.DialogProgressBG()
            self.dlg.create('飞牛影视 媒体库同步', message)
        except Exception:
            self.dlg = None

    def update(self, percent, message=''):
        if self.dlg:
            try:
                self.dlg.update(max(0, min(100, int(percent))), message=message)
            except Exception:
                pass

    def close(self):
        if self.dlg:
            try:
                self.dlg.close()
            except Exception:
                pass
            self.dlg = None


# ------------------------------------------------------------------ 图片本地化

def _download_art(client, tasks, progress=None):
    """并行下载图片素材到本地。tasks = [(本地路径, 绝对URL, 宽度)]。

    已存在且非空的文件跳过（增量）；404 等服务端有响应的失败保留 3 次
    重试（服务端图片按需生成，冷启动首访可能 404，真机实测）；完全无
    响应（连接失败/超时）连续 ART_ABORT_STREAK 张即熔断中止本轮——
    否则服务端宕机时每张要烧 3×(25s×2) 超时，数万张任务能把同步
    线程挂住数天。播放中自动暂停让路（代理与本进程同住，12 线程下载
    的 GIL 争用曾把代理 HEAD 拖到 20s 超时，真机日志 Timeout 28）。
    progress(done, total) 供同步进度显示。返回成功下载数。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from resources.lib import proxy
    import urllib.error
    import urllib.request
    todo = []
    for path, abs_url, width in tasks:
        if not abs_url:
            continue
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 1024:
                continue
        except OSError:
            pass
        todo.append((path, abs_url, width))
    if not todo:
        return 0
    os.makedirs(os.path.dirname(todo[0][0]), exist_ok=True)

    abort = threading.Event()
    _paused_announced = [False]   # 让路状态只播报一次（多线程竞争重复无害）

    def _yield_if_playing():
        """播放中让路（阻塞至播放结束）。每个下载线程独立轮询播放状态、
        不依赖完成回调——0.7.7 的 Event 版由主循环在完成回调里清除暂停，
        全部线程暂停后不再有完成事件，“播放结束”永远判不到，同步卡死
        在下载阶段（真机实测 47% 一动不动直到重启）。"""
        while not abort.is_set():
            try:
                playing = xbmc.getCondVisibility('Player.Playing')
            except Exception:
                playing = False
            if not playing:
                if _paused_announced[0]:
                    _paused_announced[0] = False
                    _log('播放结束，图片下载继续')
                return
            if not _paused_announced[0]:
                _paused_announced[0] = True
                _log('检测到播放，图片下载暂停让路')
            time.sleep(2)

    def _one(t):
        """下载单张：'ok' 成功 / 'miss' 失败但服务端有响应 / 'dead' 完全无响应"""
        if abort.is_set():
            return 'dead'
        path, abs_url, width = t
        abs_url = abs_url if 'width=' in abs_url else client.image_url(abs_url, width=width)
        url = proxy.image_url(abs_url)
        # 直连优先（带 Authx 签名，省去本机代理一跳提速），失败回退代理。
        # 服务端图片按需生成：首次请求可能 404（冷启动，真机实测），整体重试
        direct_headers = {}
        try:
            if client.token and abs_url.startswith(client.base):
                direct_headers = {'Authorization': client.token,
                                  'Authx': client.authx_for(abs_url[len(client.base):])}
        except Exception:
            direct_headers = {}
        alive = False   # 任一次拿到 HTTP 状态码（含 404）即视为服务端存活
        for attempt in (1, 2, 3):
            if abort.is_set():
                break
            _yield_if_playing()   # 播放中让路（代理同进程，避免 GIL 争用拖慢取流）
            data = None
            try:
                req = urllib.request.Request(abs_url, headers=direct_headers)
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = r.read()
                alive = True
            except urllib.error.HTTPError:
                alive = True    # 404 等：服务端在，图片可能仍在按需生成
            except Exception:
                pass
            if not data:
                try:
                    with urllib.request.urlopen(url, timeout=25) as r:
                        data = r.read()
                    alive = True
                except urllib.error.HTTPError as e:
                    alive = True
                    _log('图片下载 HTTP %d: %s' % (e.code, os.path.basename(path)),
                         xbmc.LOGDEBUG)
                except Exception as e:
                    _log('图片下载失败 %s: %s' % (os.path.basename(path), str(e)[:50]),
                         xbmc.LOGDEBUG)
            if data and len(data) > 1024:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(data)
                return 'ok'
            if attempt < 3:
                time.sleep(3)
        return 'miss' if alive else 'dead'

    ok = dead_streak = 0
    with ThreadPoolExecutor(max_workers=ART_WORKERS) as pool:
        futs = [pool.submit(_one, t) for t in todo]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                res = f.result()
            except Exception:
                res = 'miss'
            if res == 'ok':
                ok += 1
                dead_streak = 0
            elif res == 'dead':
                dead_streak += 1
                if dead_streak >= ART_ABORT_STREAK and not abort.is_set():
                    abort.set()
                    _log('连续 %d 张图片下载完全无响应，中止本轮（服务端不可达？下轮同步重试）'
                         % dead_streak, xbmc.LOGWARNING)
            else:
                dead_streak = 0    # 服务端有响应（404 按需生成属正常），不熔断
            if progress and (i % 50 == 0 or i == len(todo)):
                progress(i, len(todo))
    return ok


def _art_tasks_for(client, guid, d, det, tasks):
    """条目 → 素材下载任务 (本地路径, 绝对URL, 宽度)（poster/fanart/clearlogo）"""
    art = d.get('art') or {}
    if art.get('poster'):
        tasks.append((_art_path(guid, 'poster'), art['poster'], 400))
    fanart = det.get('backdrops') or art.get('fanart')
    if fanart:
        tasks.append((_art_path(guid, 'fanart'), fanart, 1280))
    if det.get('logos'):
        tasks.append((_art_path(guid, 'clearlogo'), det['logos'], 800))


ACTOR_TOP_N = 40         # 每部影片预下载主演头像数（与 NFO 演员上限一致，全部本地化）


def _actor_tasks(client, detail_entry, tasks):
    """详情分片 → 主演头像下载任务（本地路径确定性，去重交给下载跳过）。
    返回是否有排队——排队者本轮下载完成后需重建 NFO（代理 URL → 本地）。"""
    persons = (detail_entry or {}).get('persons') or []
    actors = [p for p in persons if p.get('job') == 'Actor' and p.get('profile_path')]
    actors.sort(key=meta._person_order)
    queued = False
    for p in actors[:ACTOR_TOP_N]:
        pp = p['profile_path']
        local = meta.local_actor_thumb(pp)
        if local:
            continue
        absu = client.image_url(pp, width=300)
        if absu:
            tasks.append((meta.actor_thumb_path(pp), absu, 300))
            queued = True
    return queued


# ------------------------------------------------------------------ NFO 构建

def _actors_xml(client, persons):
    """演员 XML：头像本地文件优先（同步已下载），未下载回退代理 URL"""
    from resources.lib import meta as _meta
    lines = []
    for p in (persons or [])[:ACTOR_TOP_N]:
        if p.get('job') != 'Actor' or not p.get('name'):
            continue
        lines.append('  <actor>')
        lines.append('    <name>%s</name>' % _esc(p['name']))
        if p.get('role'):
            lines.append('    <role>%s</role>' % _esc(p['role']))
        if p.get('profile_path'):
            local = _meta.local_actor_thumb(p['profile_path'])
            if not local:
                from resources.lib import proxy
                absu = client.image_url(p['profile_path'], width=300)
                if absu:
                    local = proxy.image_url(absu)
            if local:
                lines.append('    <thumb>%s</thumb>' % _esc(local))
        lines.append('  </actor>')
    return '\n'.join(lines)


def _movie_nfo(client, d, guid, art_paths):
    detail_entry = meta.get_cached_detail(guid, ignore_ttl=True)
    det = (detail_entry or {}).get('detail') or {}
    persons = (detail_entry or {}).get('persons') or []
    info = d.get('infos') or {}
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', '<movie>']
    lines.append('  <title>%s</title>' % _esc(d.get('label', '')))
    if det.get('original_title'):
        lines.append('  <originaltitle>%s</originaltitle>' % _esc(det['original_title']))
    if info.get('year'):
        lines.append('  <year>%d</year>' % info['year'])
    if info.get('rating'):
        lines.append('  <ratings><rating name="default" max="10" default="true">'
                     '<value>%s</value></rating></ratings>' % info['rating'])
    for gname in meta.genre_names(client, det.get('genres')):
        lines.append('  <genre>%s</genre>' % _esc(gname))
    # 导演/编剧/国家：详情缓存里本就有（浏览器路径也在用），NFO 一并
    # 落盘——原生库信息页与按导演筛选才有数据
    for name in meta.crew_names(persons, 'Director')[:5]:
        lines.append('  <director>%s</director>' % _esc(name))
    for name in meta.crew_names(persons, 'Writer')[:5]:
        lines.append('  <writer>%s</writer>' % _esc(name))
    for country in (det.get('production_countries') or [])[:3]:
        lines.append('  <country>%s</country>' % _esc(country))
    if info.get('plot'):
        lines.append('  <plot>%s</plot>' % _esc(info['plot']))
    if info.get('premiered'):
        lines.append('  <premiered>%s</premiered>' % info['premiered'])
    if info.get('duration'):
        lines.append('  <runtime>%d</runtime>' % (info['duration'] // 60))
    if det.get('imdb_id'):
        lines.append('  <uniqueid type="imdb" default="true">%s</uniqueid>' % _esc(det['imdb_id']))
    trim_id = str(det.get('trim_id') or '')
    if trim_id.isdigit():
        lines.append('  <uniqueid type="tmdb">%s</uniqueid>' % trim_id)
    lines.append('  <uniqueid type="fnmedia">%s</uniqueid>' % _esc(guid))
    for kind, aspect in (('poster', None), ('fanart', 'fanart'), ('clearlogo', 'clearlogo')):
        p = art_paths.get(kind)
        if p:
            attr = ' aspect="%s"' % aspect if aspect else ''
            lines.append('  <thumb%s>%s</thumb>' % (attr, _esc(p)))
    actors = _actors_xml(client, persons)
    if actors:
        lines.append(actors)
    if info.get('playcount'):
        lines.append('  <playcount>1</playcount>')
    lines.append('</movie>')
    return '\n'.join(lines)


def _tvshow_nfo(client, d, art_paths):
    detail_entry = meta.get_cached_detail(d.get('guid', ''), ignore_ttl=True)
    det = (detail_entry or {}).get('detail') or {}
    persons = (detail_entry or {}).get('persons') or []
    info = d.get('infos') or {}
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', '<tvshow>']
    lines.append('  <title>%s</title>' % _esc(d.get('label', '').split('（')[0]))
    if det.get('original_title'):
        lines.append('  <originaltitle>%s</originaltitle>' % _esc(det['original_title']))
    if info.get('year'):
        lines.append('  <year>%d</year>' % info['year'])
    if info.get('rating'):
        lines.append('  <ratings><rating name="default" max="10" default="true">'
                     '<value>%s</value></rating></ratings>' % info['rating'])
    for gname in meta.genre_names(client, det.get('genres')):
        lines.append('  <genre>%s</genre>' % _esc(gname))
    for name in meta.crew_names(persons, 'Director')[:5]:
        lines.append('  <director>%s</director>' % _esc(name))
    for name in meta.crew_names(persons, 'Writer')[:5]:
        lines.append('  <writer>%s</writer>' % _esc(name))
    for country in (det.get('production_countries') or [])[:3]:
        lines.append('  <country>%s</country>' % _esc(country))
    if det.get('overview'):
        lines.append('  <plot>%s</plot>' % _esc(det['overview']))
    elif info.get('plot'):
        lines.append('  <plot>%s</plot>' % _esc(info['plot']))
    if info.get('premiered'):
        lines.append('  <premiered>%s</premiered>' % info['premiered'])
    if det.get('imdb_id'):
        lines.append('  <uniqueid type="imdb" default="true">%s</uniqueid>' % _esc(det['imdb_id']))
    lines.append('  <uniqueid type="fnmedia">%s</uniqueid>' % _esc(d.get('guid', '')))
    for kind, aspect in (('poster', None), ('fanart', 'fanart'), ('clearlogo', 'clearlogo')):
        p = art_paths.get(kind)
        if p:
            attr = ' aspect="%s"' % aspect if aspect else ''
            lines.append('  <thumb%s>%s</thumb>' % (attr, _esc(p)))
    actors = _actors_xml(client, persons)
    if actors:
        lines.append(actors)
    lines.append('</tvshow>')
    return '\n'.join(lines)


def _episode_nfo(ep, still_path):
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', '<episodedetails>']
    title = ep.get('title') or ''
    lines.append('  <title>%s</title>' % _esc(title or '第 %d 集' % (ep.get('episode_number') or 0)))
    lines.append('  <season>%d</season>' % _season_no(ep))
    lines.append('  <episode>%d</episode>' % (ep.get('episode_number') or 0))
    if ep.get('tv_title'):
        lines.append('  <showtitle>%s</showtitle>' % _esc(ep['tv_title']))
    if ep.get('overview'):
        lines.append('  <plot>%s</plot>' % _esc(ep['overview']))
    if ep.get('air_date'):
        lines.append('  <aired>%s</aired>' % ep['air_date'])
    if still_path:
        lines.append('  <thumb>%s</thumb>' % _esc(still_path))
    if int(ep.get('watched') or 0):
        lines.append('  <playcount>1</playcount>')
    wts = int(ep.get('watched_ts') or ep.get('ts') or 0)
    if wts > 60:
        lines.append('  <resume><position>%d</position></resume>' % wts)
    lines.append('</episodedetails>')
    return '\n'.join(lines)


def _tv_walk(client, tv_guids, force=False):
    """剧 → (季, 集) 结构，带 24h 读缓存，温和不刷接口。

    失败防御（瞬时故障不伤库）：
      - 单剧抓取异常：有旧缓存则沿用旧结构（不刷新 ts，下轮重试），
        无旧数据才计入失败集；
      - 新抓结果集数为空而旧缓存非空：疑似服务端半途异常，同样沿用旧
        数据——空结果若落缓存会封死 24h，且当轮清理会把该剧文件当
        "已消失"误删。
    返回 (数据映射, 失败且无旧数据可回退的 guid 集)。"""
    cache = util.load_json(TV_WALK_FILE) or {}
    now = time.time()
    out = {}
    failed = set()
    for g in tv_guids:
        e = cache.get(g)
        old_data = (e.get('data') or {}) if e else {}
        if e and now - e.get('ts', 0) < TV_WALK_TTL and not force:
            out[g] = old_data
            continue
        try:
            seasons = client.season_list(g) or []
            eps = []
            for s in seasons:
                for ep in (client.episode_list(s.get('guid') or '') or []):
                    ep = dict(ep)
                    ep.setdefault('season_number', s.get('season_number'))
                    eps.append(ep)
            if not eps and old_data.get('eps'):
                _log('剧 %s… 本轮集数为空（服务端异常？），沿用旧结构 %d 集'
                     % (g[:12], len(old_data['eps'])), xbmc.LOGWARNING)
                out[g] = old_data    # 不写缓存：下轮仍会重取
                continue
            # 集编号稳定化：服务端跨轮返回的 (季,集) 编号可能漂移（真机
            # 实测同一集编号变化 → NFO 内容变 → Kodi 扫库器按（季,集）
            # 匹配不到旧记录插入新行 → 库里重复集），以 guid 为键锁定
            # 首次分配的编号，后续轮次沿用
            old_eps = old_data.get('eps') or []
            if old_eps:
                locked = {ep.get('guid'): (ep.get('season_number'),
                                           ep.get('episode_number'))
                          for ep in old_eps if ep.get('guid')}
                for ep in eps:
                    if ep.get('guid') in locked:
                        ep['season_number'], ep['episode_number'] = locked[ep['guid']]
            out[g] = {'seasons': seasons, 'eps': eps}
            cache[g] = {'ts': now, 'data': out[g]}
            time.sleep(0.1)   # 温和限速
        except Exception as ex:
            if old_data.get('eps'):
                _log('剧 %s… 结构获取失败，沿用旧缓存（下轮重试）: %s'
                     % (g[:12], ex), xbmc.LOGWARNING)
                out[g] = old_data    # 不刷新 ts：下轮重试
            else:
                _log('剧 %s… 结构获取失败且无旧数据，本轮跳过: %s'
                     % (g[:12], ex), xbmc.LOGWARNING)
                failed.add(g)
    # 瘦身：已不在库里的剧保留 TV_WALK_PRUNE_AFTER 宽限后丢弃（编号锁随
    # 之失效属可接受——剧已删）；宽限期防范围文件临时缺失时误清编号锁
    guids = set(tv_guids)
    cache = {g: e for g, e in cache.items()
             if g in guids or now - e.get('ts', 0) < TV_WALK_PRUNE_AFTER}
    util.save_json(TV_WALK_FILE, cache)
    return out, failed


def _videos_dbs():
    """MyVideos 库路径按版本号升序（调用方取 [-1] 即最新）。纯字典序会把
    MyVideos9.db 排到 MyVideos119.db 之后选错库。"""
    import glob as _glob
    import re as _re
    dbdir = os.path.join(util.translate_path('special://profile'), 'Database')

    def _ver(p):
        m = _re.search(r'MyVideos(\d+)', os.path.basename(p))
        return int(m.group(1)) if m else 0
    return sorted(_glob.glob(os.path.join(dbdir, 'MyVideos*.db')), key=_ver)


def _set_content_db(path_, content):
    """在 MyVideos DB 的 path 表登记一个库目录并设置内容类型。

    Kodi 的扫库器按 path.strContent 决定导入；'metadata.local' 刮削器
    表示仅用本地 NFO（我们的 NFO 是全量的，扫库零联网）。
    0.7.x 两处实测 bug（真机 21.3）：①旧签名 (mroot, troot) 与调用方
    (路径, 类型) 错配，把 tvshows 根写成 movies 并留下 'movies'/'tvshows'
    字面量垃圾行；②strPath 缺尾斜杠与扫库器归一化路径不匹配，Scan 1ms
    空跑整库不入库。本版按单行登记 + 尾斜杠 + 旧行原位迁移 + 垃圾行清理。"""
    import sqlite3
    dbs = _videos_dbs()
    if not dbs:
        _log('未找到 MyVideos 数据库，无法设置内容类型', xbmc.LOGWARNING)
        return
    if not path_.endswith('/'):
        path_ += '/'
    try:
        # timeout 3s：与 Kodi 主进程（写观看状态/刷新列表）锁竞争时写不进
        # 就放弃，本写操作幂等、下轮同步会重写，避免跨进程互等 15s 长锁
        conn = sqlite3.connect(dbs[-1], timeout=3)
        row = conn.execute('SELECT idPath FROM path WHERE strPath IN (?, ?)',
                           (path_, path_[:-1])).fetchone()
        if row:
            conn.execute('UPDATE path SET strPath=?, strContent=?, strScraper=? '
                         'WHERE idPath=?',
                         (path_, content, 'metadata.local', row[0]))
        else:
            conn.execute('INSERT INTO path (strPath, strContent, strScraper, '
                         'scanRecursive, useFolderNames, noUpdate, exclude) '
                         'VALUES (?,?,?,1,0,0,0)', (path_, content, 'metadata.local'))
        # 0.7.x 签名错配遗留的字面量垃圾行
        conn.execute("DELETE FROM path WHERE strPath IN ('movies', 'tvshows')")
        conn.commit()
        conn.close()
        _log('内容类型已写入 path 表: %s -> %s' % (path_, content))
    except Exception as e:
        _log('写 path 表失败: %s' % e, xbmc.LOGWARNING)


def _jsonrpc(method, params=None):
    """调用 Kodi JSON-RPC（TCP 127.0.0.1:9090，Kodi 默认开启、不依赖
    webserver 设置）。service 上下文里 executebuiltin 真机可能不生效，
    JSON-RPC 是可靠通道；连接失败/超时返回 None，调用方回退内建。"""
    import json as _json
    import socket
    try:
        with socket.create_connection(('127.0.0.1', 9090), timeout=5) as s:
            s.settimeout(5)
            s.sendall(_json.dumps({'jsonrpc': '2.0', 'method': method,
                                   'params': params or {}, 'id': 1}).encode('utf-8'))
            buf = b''
            dec = _json.JSONDecoder()
            while True:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    return None
                if not chunk:
                    return None
                buf += chunk
                # TCP 流上可能混入无 id 的推送通知，逐个解析出本次响应
                data = buf.decode('utf-8', 'replace')
                idx, n = 0, len(data)
                while idx < n:
                    if data[idx] in ' \t\r\n':
                        idx += 1
                        continue
                    try:
                        obj, end = dec.raw_decode(data, idx)
                    except ValueError:
                        break   # 半条消息，继续 recv
                    idx = end
                    if isinstance(obj, dict) and obj.get('id') == 1:
                        return obj
    except OSError:
        return None


def _trigger_library_scan(mroot, troot, clean=False):
    """触发扫库/清理。两个根共用父目录（library/），一次 Scan 递归扫完
    （movies/tvshows 子目录各自的 path 表内容类型分别生效）——此前对
    两个根各发一次 Scan，第二次请求撞上第一次扫描进行中会被 Kodi 静默
    丢弃（JSON-RPC 仍返回 OK 但事件流只有一次 OnScanStarted，实测复现）：
    电影先扫正常入库、剧集扫描永远不执行，"电视剧空库"的首因。
    Scan 走 JSON-RPC TCP 9090（service 上下文可靠，真机 21.3 实测无弹
    窗）；失败回退 executebuiltin（GUI 上下文可靠），调用方应保留
    scan_pending 由 run_pending_scan() 兜底。
    Clean 不走 JSON-RPC：21.3 实测 displayDialog 参数被拒、无参调用会弹
    确认框并阻塞 JSON-RPC 线程（连 Ping 都不响应）——只走 executebuiltin
    （带 false 参数无弹窗），clean_pending 始终保留兜底。"""
    # 目录参数必须带尾斜杠：真机 21.3 实测不带斜杠时 Scan 1ms 空跑
    parent = os.path.dirname(mroot.rstrip('/')) or mroot
    directory = parent if parent.endswith('/') else parent + '/'
    resp = _jsonrpc('VideoLibrary.Scan', {'directory': directory})
    ok = isinstance(resp, dict) and resp.get('result') == 'OK'
    if not ok:
        xbmc.executebuiltin('VideoLibrary.Scan(%s)' % directory)
    if clean:
        xbmc.executebuiltin('VideoLibrary.Clean(false)')
    return ok


def _repair_actor_art(actor_local):
    """演员头像 art 修复：Kodi 重扫对演员头像只补缺失、不覆盖已有记录，
    NFO 本地化后旧记录不会自己刷新。直写 MyVideos DB：把"已有本地头像
    文件"的演员的 art 记录与本地路径对齐（http / 旧命名一律修正）、
    缺失的补上（幂等，每轮同步执行）。
    兼容两种库表结构：Kodi 19 的 actors(idActor, strActor) 与
    Kodi 20/21 的 actor(actor_id, name, art_urls)——新版（真机 21.3
    实测）还把原始 <thumb> 元素存进 actor.art_urls，需一并修正，
    否则惰性加载会用旧 URL 重下。
    actor_local = {演员名: 本地路径}。返回修复条数。"""
    if not actor_local:
        return 0
    import sqlite3
    dbs = _videos_dbs()
    if not dbs:
        return 0
    try:
        conn = sqlite3.connect(dbs[-1], timeout=3)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='actors'").fetchone():
            atbl, aid, aname = 'actors', 'idActor', 'strActor'
        else:
            atbl, aid, aname = 'actor', 'actor_id', 'name'
        cols = [c[1] for c in conn.execute('PRAGMA table_info(%s)' % atbl)]

        conn.execute('CREATE TEMP TABLE IF NOT EXISTS _fn_actors('
                     'name TEXT PRIMARY KEY, path TEXT)')
        conn.execute('DELETE FROM _fn_actors')
        conn.executemany('INSERT OR IGNORE INTO _fn_actors(name, path) VALUES (?,?)',
                         list(actor_local.items()))
        n = 0
        # URL 与本地文件不一致的（http、旧 .jpg 命名等）→ 修正为当前本地
        # 路径。SQL 用 + 拼接表/列名：相邻字面量先合并再 % 格式化会把
        # LIKE 'http%' 的 % 当格式符，是上一版的静默失败根因
        cur = conn.execute(
            "SELECT f.path, art.art_id FROM art "
            "JOIN " + atbl + " a ON a." + aid + "=art.media_id "
            "JOIN _fn_actors f ON a." + aname + "=f.name "
            "WHERE art.media_type='actor' AND art.type='thumb' "
            "  AND art.url <> f.path")
        rows = cur.fetchall()
        if rows:
            conn.executemany('UPDATE art SET url=? WHERE art_id=?', rows)
            n += len(rows)
        # 缺失记录补上
        cur = conn.execute(
            "INSERT INTO art (media_id, media_type, type, url) "
            "SELECT a." + aid + ", 'actor', 'thumb', f.path "
            "FROM _fn_actors f JOIN " + atbl + " a ON a." + aname + "=f.name "
            "WHERE a." + aid + " NOT IN (SELECT media_id FROM art "
            "                            WHERE media_type='actor' AND type='thumb')")
        n += cur.rowcount
        if 'art_urls' in cols:
            # Kodi 20/21 惰性加载源：与本地路径不一致的一并归一
            cur = conn.execute(
                "SELECT a." + aid + ", a.art_urls, f.path FROM " + atbl + " a "
                "JOIN _fn_actors f ON a." + aname + "=f.name")
            urls = []
            for aid_, au, path in cur.fetchall():
                want = '<thumb>%s</thumb>' % path
                if (au or '') != want:
                    urls.append((want, aid_))
            if urls:
                conn.executemany('UPDATE ' + atbl + ' SET art_urls=? WHERE '
                                 + aid + '=?', urls)
                n += len(urls)
        conn.commit()
        conn.close()
        return n
    except Exception as e:
        _log('演员头像 art 修复失败: %s' % e, xbmc.LOGWARNING)
        return 0


def _is_webp(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(12)
        return head[:4] == b'RIFF' and head[8:12] == b'WEBP'
    except OSError:
        return False


def _art_path(guid, kind):
    """条目图片本地路径。内容与后缀必须一致（webp 内容 .webp / jpeg
    内容 .jpg，Kodi 按扩展名选解码器）；未下载时预期新下载为 webp。"""
    base = os.path.join(_lib_root(), ART_DIR, '%s_%s' % (guid, kind))
    for ext in ('.webp', '.jpg'):
        if os.path.isfile(base + ext):
            return base + ext
    return base + '.webp'


def _migrate_art_files(root):
    """library/art 内 webp 内容的 .jpg → .webp（只改后缀，内容零改动）。
    与 _migrate_actor_thumbs 同理：扩展名必须与内容一致，否则 Kodi
    按 .jpg 走 mjpeg 解码器必失败（海报/背景/剧照显示缺失）。
    幂等。返回重命名数。"""
    n = 0
    if not os.path.isdir(root):
        return n    # 目录尚不存在（全新安装首轮同步）：无旧文件可迁移
    try:
        for fn in os.listdir(root):
            if not fn.endswith('.jpg'):
                continue
            p = os.path.join(root, fn)
            if _is_webp(p):
                os.replace(p, p[:-4] + '.webp')
                n += 1
    except OSError as e:
        _log('图片重命名迁移失败: %s' % e, xbmc.LOGWARNING)
    return n


def _migrate_art_urls(root):
    """art 表里指向旧 .jpg 命名的 URL 对齐到重命名后的 .webp（幂等）。
    覆盖 movie/tvshow/episode 的全部图片类型；image://video@ 提取缩略图
    等非本目录 URL 不受影响。返回更新条数；异常路径返回 None（调用方
    以"非 None"判定迁移已完成，避免失败被误标记后永远不再重试）。"""
    import sqlite3
    dbs = _videos_dbs()
    if not dbs:
        return None
    pairs = []
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.endswith('.webp'):
                    old = os.path.join(dirpath, fn[:-5] + '.jpg')
                    if not os.path.exists(old):
                        pairs.append((os.path.join(dirpath, fn), old))
    except OSError:
        return None
    if not pairs:
        return 0
    try:
        conn = sqlite3.connect(dbs[-1], timeout=3)
        cur = conn.executemany('UPDATE art SET url=? WHERE url=?', pairs)
        n = cur.rowcount
        conn.commit()
        conn.close()
        return n
    except Exception as e:
        _log('图片 URL 对齐失败: %s' % e, xbmc.LOGWARNING)
        return None


def _migrate_actor_thumbs(actor_dir):
    """演员头像 .jpg（webp 内容）→ .webp，只改后缀、内容零改动。

    Kodi 图片加载按扩展名选解码器：.jpg → mjpeg，解 webp 数据必失败
    （真机 21.3 实测原生库信息页演员头像全剪影）；服务端图片源文件
    本身就是 webp，与内容一致的后缀才能走 webp 解码器。幂等。
    返回重命名数。"""
    n = 0
    if not os.path.isdir(actor_dir):
        return n    # 目录尚不存在（全新安装首轮同步）：无旧文件可迁移
    try:
        for fn in os.listdir(actor_dir):
            if not fn.endswith('.jpg'):
                continue
            p = os.path.join(actor_dir, fn)
            if _is_webp(p):
                os.replace(p, p[:-4] + '.webp')
                n += 1
    except OSError as e:
        _log('演员头像重命名迁移失败: %s' % e, xbmc.LOGWARNING)
    return n


def _collect_actor_local(detail_entry, out):
    """汇总已有本地头像文件的演员：{名字: 本地路径}（art 修复的匹配集）"""
    for p in (detail_entry or {}).get('persons') or []:
        if p.get('job') != 'Actor' or not p.get('name') or not p.get('profile_path'):
            continue
        if p['name'] in out:
            continue
        local = meta.local_actor_thumb(p['profile_path'])
        if local:
            out[p['name']] = local


def run_pending_scan():
    """invoker（GUI 上下文）执行挂起的扫库/清理；无挂起返回 False。"""
    state = util.load_json(STATE_FILE) or {}
    if not (state.get('scan_pending') or state.get('clean_pending')):
        return False
    mroot = os.path.join(_lib_root(), MOVIE_DIR)
    troot = os.path.join(_lib_root(), TV_DIR)
    _trigger_library_scan(mroot, troot, clean=bool(state.get('clean_pending')))
    state['scan_pending'] = 0
    state['clean_pending'] = 0
    util.save_json(STATE_FILE, state)
    _log('已触发挂起的扫库')
    return True


def sync_library(client, force=False):
    """同步入口（service 周期调用）。返回状态字符串用于日志。

    全程带 DialogProgressBG 角标进度（Kodi 右下角），不打断 GUI 操作。"""
    if util.get_setting('libsync', 'true') != 'true':
        return 'disabled'
    state = util.load_json(STATE_FILE) or {}
    if not force and time.time() - state.get('ts', 0) < _sync_ttl():
        return 'fresh'

    prog = _SyncProgress('正在收集媒体清单…')
    try:
        movies = []
        seen = set()
        tvs = []
        seen_t = set()
        for key in desc.scope_keys(client):
            scope = desc.load_scope(key, allow_expired=True) or {}
            for d in scope.get('items') or []:
                g = d.get('guid', '')
                if not g:
                    continue
                if d.get('type') == 'Movie' and g not in seen:
                    seen.add(g)
                    movies.append(d)
                elif d.get('type') == 'TV' and g not in seen_t:
                    seen_t.add(g)
                    tvs.append(d)
        if not movies and not tvs:
            return 'empty'   # 描述符未就绪，下轮再试
        prog.update(3, '清单完成：电影 %d 部 / 剧集 %d 部' % (len(movies), len(tvs)))

        root = _lib_root()
        mroot = os.path.join(root, MOVIE_DIR)
        troot = os.path.join(root, TV_DIR)
        # 旧 .jpg 命名（webp 内容）迁移为 .webp（只改后缀，内容零改动）。
        # 一次完成后记入 state 不再重跑：迁移完成后每轮仍会重建数万
        # pairs 并向 Kodi 库发同量级 no-op UPDATE，纯空转
        if not state.get('art_migrated'):
            _migrate_actor_thumbs(os.path.join(util.addon_data_dir(), meta.ACTOR_ART_DIR))
            _migrate_art_files(os.path.join(root, ART_DIR))
            if _migrate_art_urls(os.path.join(root, ART_DIR)) is not None:
                state['art_migrated'] = 1
        manifest = {}
        art_tasks = []
        # (是电影, 目录, 描述符, guid, art_paths)：头像本轮排队下载的条目，
        # 下载完成后重建 NFO（<thumb> 代理 URL → 本地路径）
        thumb_pending = []
        actor_local = {}   # 演员名 → 本地头像路径（art 修复用）
        new_f = chg_f = 0

        # 剧集结构（24h 读缓存，温和限速；瞬时失败沿用旧结构防误删）
        prog.update(5, '获取 %d 部剧集的季/集结构…' % len(tvs))
        tv_data, tv_failed = _tv_walk(client, [d['guid'] for d in tvs])
        n_eps = sum(len((tv_data.get(x['guid']) or {}).get('eps') or []) for x in tvs)
        total_items = len(movies) + n_eps
        done_items = [0]

        def _item_progress(stage_msg):
            if total_items:
                done_items[0] += 1
                prog.update(8 + 37.0 * done_items[0] / total_items, stage_msg)

        # 电影：strm + nfo（引用本地图片路径，稍后并行下载）
        for mi, d in enumerate(movies, 1):
            g = d['guid']
            base = os.path.join(mroot, _safename(d.get('label', ''), g))
            r1 = _write(base + '.strm', _play_url(g))
            det_entry = meta.get_cached_detail(g, ignore_ttl=True)
            det = (det_entry or {}).get('detail') or {}
            art_paths = {'poster': _art_path(g, 'poster'), 'fanart': _art_path(g, 'fanart'),
                         'clearlogo': _art_path(g, 'clearlogo')}
            r2 = _write(base + '.nfo', _movie_nfo(client, d, g, art_paths))
            _art_tasks_for(client, g, d, det, art_tasks)
            if _actor_tasks(client, det_entry, art_tasks):
                thumb_pending.append((True, base, d, g, art_paths))
            _collect_actor_local(det_entry, actor_local)
            manifest[os.path.relpath(base + '.strm', root)] = 1
            manifest[os.path.relpath(base + '.nfo', root)] = 1
            for kind in ('poster', 'fanart', 'clearlogo'):
                manifest[os.path.relpath(_art_path(g, kind), root)] = 1
            new_f += (r1 == 'new') + (r2 == 'new')
            chg_f += (r1 == 'chg') + (r2 == 'chg')
            _item_progress('电影 %d/%d · %s' % (mi, len(movies), d.get('label', '')[:16]))

        # 剧集：剧 → tvshow.nfo + 季目录 → 集 strm/nfo + 剧照
        for si, d in enumerate(tvs, 1):
            g = d['guid']
            data = tv_data.get(g)
            if not data or not data.get('eps'):
                continue
            sdir = os.path.join(troot, _safename(d.get('label', '').split('（')[0], g))
            det_entry = meta.get_cached_detail(g, ignore_ttl=True)
            det = (det_entry or {}).get('detail') or {}
            art_paths = {'poster': _art_path(g, 'poster'), 'fanart': _art_path(g, 'fanart'),
                         'clearlogo': _art_path(g, 'clearlogo')}
            r0 = _write(os.path.join(sdir, 'tvshow.nfo'), _tvshow_nfo(client, d, art_paths))
            _art_tasks_for(client, g, d, det, art_tasks)
            if _actor_tasks(client, det_entry, art_tasks):
                thumb_pending.append((False, sdir, d, g, art_paths))
            _collect_actor_local(det_entry, actor_local)
            manifest[os.path.relpath(os.path.join(sdir, 'tvshow.nfo'), root)] = 1
            for kind in ('poster', 'fanart', 'clearlogo'):
                manifest[os.path.relpath(_art_path(g, kind), root)] = 1
            new_f += (r0 == 'new')
            chg_f += (r0 == 'chg')
            for ep in data['eps']:
                sn = _season_no(ep)
                en = int(ep.get('episode_number') or 0)
                if not en:
                    continue
                eg = ep['guid']
                edir = os.path.join(sdir, 'Season %d' % sn)
                base = os.path.join(edir, 'S%02dE%02d_%s' % (sn, en, eg[:8]))
                r1 = _write(base + '.strm', _play_url(eg))
                still = _art_path(eg, 'still')
                # 集条目的剧照在 poster 字段（1920x1080 webp），API 无
                # still_path 字段（真机实测），此前任务从未生成、剧照全缺
                sp = ep.get('still_path') or ep.get('poster')
                if sp:
                    art_tasks.append((still, sp, 640))
                r2 = _write(base + '.nfo', _episode_nfo(ep, still))
                manifest[os.path.relpath(base + '.strm', root)] = 1
                manifest[os.path.relpath(base + '.nfo', root)] = 1
                manifest[os.path.relpath(_art_path(eg, 'still'), root)] = 1
                new_f += (r1 == 'new') + (r2 == 'new')
                chg_f += (r1 == 'chg') + (r2 == 'chg')
                _item_progress('剧集 %d/%d · %s S%02dE%02d' % (
                    si, len(tvs), d.get('label', '')[:12], sn, en))

        # 清理已消失的条目（含随之空掉的目录）。tv_failed 非空 = 有剧本轮
        # 连旧结构都没有（无法证明其文件"该删"），剧集树整体跳过清理——
        # 宁可晚一轮清理，不可把网络抖动当成条目消失（防整剧被清出库）
        prog.update(45, '清理已消失条目…')
        skip_tv = bool(tv_failed)
        if skip_tv:
            _log('本轮 %d 部剧结构获取失败，跳过剧集树清理' % len(tv_failed),
                 xbmc.LOGWARNING)
        removed = 0
        try:
            for dirpath, _dirs, files in os.walk(root, topdown=False):
                for fn in files:
                    rel = os.path.relpath(os.path.join(dirpath, fn), root)
                    if rel in manifest:
                        continue
                    if skip_tv and rel.startswith(TV_DIR + os.sep):
                        continue
                    os.remove(os.path.join(dirpath, fn))
                    removed += 1
                if dirpath != root and not os.listdir(dirpath):
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass
        except OSError as e:
            _log('清理失败: %s' % e, xbmc.LOGWARNING)

        # 并行下载图片素材（首轮约 4 万张，后续增量跳过）
        prog.update(47, '下载图片素材…')

        def _art_progress(done, total):
            prog.update(47 + 48.0 * done / total, '下载图片 %d/%d' % (done, total))

        got = _download_art(client, art_tasks, progress=_art_progress)

        # 本轮新落盘的头像：重建对应 NFO（<thumb> 代理 URL → 本地路径）
        # 并补收 art 修复匹配集——首轮同步即导入本地路径，不必等第二轮
        # 自愈（_write 内容比对幂等，仅头像确实落盘的条目会重写）
        if thumb_pending:
            prog.update(94, '回写本地头像路径…')
            for is_movie, base, d, g, art_paths in thumb_pending:
                _collect_actor_local(meta.get_cached_detail(g, ignore_ttl=True),
                                     actor_local)
                nfo = _movie_nfo(client, d, g, art_paths) if is_movie \
                    else _tvshow_nfo(client, d, art_paths)
                _write((base + '.nfo') if is_movie
                       else os.path.join(base, 'tvshow.nfo'), nfo)

        # 演员头像 art 修复（幂等，先于扫库）：信息页下次打开即取到本地头像，
        # 不依赖 Kodi 重扫是否会覆盖旧记录
        prog.update(96, '登记媒体库并触发扫库…')
        fixed = _repair_actor_art(actor_local)

        # 内容类型：直接写 MyVideos DB 的 path 表。Content.SetContent 内建
        # 在 service 上下文不生效（真机 0.7.0 实测：扫库器按 path.strContent
        # 决定导入与否，未设置的目录整目录跳过）。
        _set_content_db(mroot, 'movies')
        _set_content_db(troot, 'tvshows')
        state = {'ts': time.time(), 'movies': len(movies), 'tv': len(tvs),
                 'files': len(manifest), 'art': got, 'scan_pending': 1,
                 'art_migrated': 1 if state.get('art_migrated') else 0,
                 'sig': _config_sig()}
        if removed:
            state['clean_pending'] = 1
        util.save_json(STATE_FILE, state)
        # 扫库：JSON-RPC（service 上下文可靠）优先；失败回退 executebuiltin，
        # 并保留 scan_pending 由 run_pending_scan()（进插件根目录时）兜底。
        # Clean 始终保留 clean_pending 兜底（builtin 在 service 上下文不可靠，
        # JSON-RPC Clean 会弹确认框阻塞）
        if _trigger_library_scan(mroot, troot, clean=bool(removed)):
            state['scan_pending'] = 0
            util.save_json(STATE_FILE, state)

        summary = '电影 %d 部、剧集 %d 部，文件 %d（新增 %d/更新 %d/清理 %d），图片 %d' % (
            len(movies), len(tvs), len(manifest), new_f, chg_f, removed, got)
        if fixed:
            summary += '，演员头像修复 %d 条' % fixed
        _log('同步完成: %s' % summary)
        # 完成角标通知（service 进程可用；同步本就避开播放窗口，不打扰）
        try:
            import xbmcgui
            xbmcgui.Dialog().notification(
                '飞牛影视 媒体库同步',
                '同步完成：电影 %d · 剧集 %d%s' % (len(movies), len(tvs),
                                                 ('（新图片 %d 张）' % got) if got else ''))
        except Exception:
            pass
        return summary
    finally:
        prog.close()
