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
  - 剧集结构：剧 → tvshow.nfo；季 → 子目录；集 → SxxEyy.strm + nfo。
    季集数据带 24h 读缓存（libsync_tvwalk.json），同步温和不刷接口。
"""
import os
import re
import time
import xml.sax.saxutils as _sax

import xbmc

from resources.lib import util, desc, meta

LIB_ROOT_NAME = 'library'
MOVIE_DIR = 'movies'
TV_DIR = 'tvshows'
ART_DIR = 'art'
SYNC_TTL = 12 * 3600
TV_WALK_TTL = 24 * 3600
ART_WORKERS = 6
MANIFEST_FILE = 'libsync_manifest.json'
STATE_FILE = 'libsync_state.json'
TV_WALK_FILE = 'libsync_tvwalk.json'


def _log(msg, level=xbmc.LOGINFO):
    util.log('libsync: %s' % msg, level)


def _lib_root():
    return os.path.join(util.addon_data_dir(), LIB_ROOT_NAME)


def _esc(text):
    return _sax.escape(str(text or ''), {'"': '&quot;'})


def _safename(name, guid):
    name = re.sub(r'[\\/:*?"<>|]', ' ', name or '').strip() or '未命名'
    return '%s_%s' % (name[:60].strip(), guid[:8])


def _write(path, content):
    """内容无变化不重写。返回 'new'/'chg'/'same'"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            if f.read() == content:
                return 'same'
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return 'new'
    except OSError as e:
        _log('写文件失败 %s: %s' % (path, e), xbmc.LOGWARNING)
        return 'same'


def _play_url(guid):
    return 'plugin://%s/?action=play&guid=%s' % (util.ADDON_ID, guid)


# ------------------------------------------------------------------ 图片本地化

def _art_path(guid, kind):
    return os.path.join(_lib_root(), ART_DIR, '%s_%s.jpg' % (guid, kind))


def _download_art(client, tasks):
    """并行下载图片素材到本地。tasks = [(本地路径, 绝对URL, 宽度)]。

    已存在且非空的文件跳过（增量）；失败静默跳过（下轮同步重试）。
    返回成功数。"""
    from concurrent.futures import ThreadPoolExecutor
    from resources.lib import proxy
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

    def _one(t):
        path, abs_url, width = t
        abs_url = abs_url if 'width=' in abs_url else client.image_url(abs_url, width=width)
        url = proxy.image_url(abs_url)
        if not url:
            return
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                data = r.read()
            if len(data) > 1024:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(data)
        except Exception as e:
            _log('图片下载失败 %s: %s' % (os.path.basename(path), str(e)[:50]), xbmc.LOGDEBUG)

    with ThreadPoolExecutor(max_workers=ART_WORKERS) as pool:
        done = sum(1 for _ in pool.map(_one, todo))
    return done


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


ACTOR_TOP_N = 12         # 每部影片仅预下载前 N 位主演的头像（信息页展示范围）


def _actor_tasks(client, detail_entry, tasks):
    """详情分片 → 主演头像下载任务（本地路径确定性，去重交给下载跳过）"""
    persons = (detail_entry or {}).get('persons') or []
    actors = [p for p in persons if p.get('job') == 'Actor' and p.get('profile_path')]
    actors.sort(key=meta._person_order)
    for p in actors[:ACTOR_TOP_N]:
        pp = p['profile_path']
        local = meta.local_actor_thumb(pp)
        if local:
            continue
        absu = client.image_url(pp, width=300)
        if absu:
            tasks.append((meta.actor_thumb_path(pp), absu, 300))


# ------------------------------------------------------------------ NFO 构建

def _actors_xml(client, persons):
    """演员 XML：头像本地文件优先（同步已下载），未下载回退代理 URL"""
    from resources.lib import meta as _meta
    lines = []
    for p in (persons or [])[:40]:
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
        lines.append('  <rating>%s</rating>' % info['rating'])
    for gname in meta.genre_names(client, det.get('genres')):
        lines.append('  <genre>%s</genre>' % _esc(gname))
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
        lines.append('  <rating>%s</rating>' % info['rating'])
    for gname in meta.genre_names(client, det.get('genres')):
        lines.append('  <genre>%s</genre>' % _esc(gname))
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
    lines.append('  <season>%d</season>' % (ep.get('season_number') or 0))
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
    """剧 → (季, 集) 结构，带 24h 读缓存，温和不刷接口"""
    cache = util.load_json(TV_WALK_FILE) or {}
    now = time.time()
    out = {}
    for g in tv_guids:
        e = cache.get(g)
        if e and now - e.get('ts', 0) < TV_WALK_TTL and not force:
            out[g] = e['data']
            continue
        try:
            seasons = client.season_list(g) or []
            eps = []
            for s in seasons:
                for ep in (client.episode_list(s.get('guid') or '') or []):
                    ep = dict(ep)
                    ep.setdefault('season_number', s.get('season_number'))
                    eps.append(ep)
            out[g] = {'seasons': seasons, 'eps': eps}
            cache[g] = {'ts': now, 'data': out[g]}
            time.sleep(0.1)   # 温和限速
        except Exception as e:
            _log('剧集结构获取失败 %s…: %s' % (g[:12], e), xbmc.LOGWARNING)
    util.save_json(TV_WALK_FILE, cache)
    return out


def _set_content_db(mroot, troot):
    """在 MyVideos DB 的 path 表登记两个库目录并设置内容类型。

    Kodi 的扫库器按 path.strContent 决定导入；'metadata.local' 刮削器
    表示仅用本地 NFO（我们的 NFO 是全量的，扫库零联网）。"""
    import glob as _glob
    import sqlite3
    dbdir = os.path.join(util.translate_path('special://profile'), 'Database')
    dbs = sorted(_glob.glob(os.path.join(dbdir, 'MyVideos*.db')))
    if not dbs:
        _log('未找到 MyVideos 数据库，无法设置内容类型', xbmc.LOGWARNING)
        return
    try:
        conn = sqlite3.connect(dbs[-1], timeout=15)
        for path_, content in ((mroot, 'movies'), (troot, 'tvshows')):
            row = conn.execute('SELECT idPath FROM path WHERE strPath=?',
                               (path_,)).fetchone()
            if row:
                conn.execute('UPDATE path SET strContent=?, strScraper=? WHERE idPath=?',
                             (content, 'metadata.local', row[0]))
            else:
                conn.execute('INSERT INTO path (strPath, strContent, strScraper, '
                             'scanRecursive, useFolderNames, noUpdate, exclude) '
                             'VALUES (?,?,?,1,0,0,0)', (path_, content, 'metadata.local'))
        conn.commit()
        conn.close()
        _log('内容类型已写入 path 表')
    except Exception as e:
        _log('写 path 表失败: %s' % e, xbmc.LOGWARNING)


def run_pending_scan():
    """invoker（GUI 上下文）执行挂起的扫库/清理；无挂起返回 False。"""
    state = util.load_json(STATE_FILE) or {}
    if not (state.get('scan_pending') or state.get('clean_pending')):
        return False
    mroot = os.path.join(_lib_root(), MOVIE_DIR)
    troot = os.path.join(_lib_root(), TV_DIR)
    if state.get('scan_pending'):
        xbmc.executebuiltin('VideoLibrary.Scan(%s)' % mroot)
        xbmc.executebuiltin('VideoLibrary.Scan(%s)' % troot)
    if state.get('clean_pending'):
        xbmc.executebuiltin('VideoLibrary.Clean(false)')
    state['scan_pending'] = 0
    state['clean_pending'] = 0
    util.save_json(STATE_FILE, state)
    _log('已在 invoker 上下文触发扫库')
    return True


def sync_library(client, force=False):
    """同步入口（service 周期调用）。返回状态字符串用于日志。"""
    if util.get_setting('libsync', 'true') != 'true':
        return 'disabled'
    state = util.load_json(STATE_FILE) or {}
    if not force and time.time() - state.get('ts', 0) < SYNC_TTL:
        return 'fresh'

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

    root = _lib_root()
    mroot = os.path.join(root, MOVIE_DIR)
    troot = os.path.join(root, TV_DIR)
    manifest = {}
    art_tasks = []
    new_f = chg_f = 0

    # 电影：strm + nfo（引用本地图片路径，稍后并行下载）
    for d in movies:
        g = d['guid']
        base = os.path.join(mroot, _safename(d.get('label', ''), g))
        r1 = _write(base + '.strm', _play_url(g))
        det_entry = meta.get_cached_detail(g, ignore_ttl=True)
        det = (det_entry or {}).get('detail') or {}
        art_paths = {'poster': _art_path(g, 'poster'), 'fanart': _art_path(g, 'fanart'),
                     'clearlogo': _art_path(g, 'clearlogo')}
        r2 = _write(base + '.nfo', _movie_nfo(client, d, g, art_paths))
        _art_tasks_for(client, g, d, det, art_tasks)
        _actor_tasks(client, det_entry, art_tasks)
        manifest[os.path.relpath(base + '.strm', root)] = 1
        manifest[os.path.relpath(base + '.nfo', root)] = 1
        for kind in ('poster', 'fanart', 'clearlogo'):
            manifest[os.path.relpath(_art_path(g, kind), root)] = 1
        new_f += (r1 == 'new') + (r2 == 'new')
        chg_f += (r1 == 'chg') + (r2 == 'chg')

    # 剧集：剧 → tvshow.nfo + 季目录 → 集 strm/nfo + 剧照
    tv_data = _tv_walk(client, [d['guid'] for d in tvs])
    for d in tvs:
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
        _actor_tasks(client, det_entry, art_tasks)
        manifest[os.path.relpath(os.path.join(sdir, 'tvshow.nfo'), root)] = 1
        for kind in ('poster', 'fanart', 'clearlogo'):
            manifest[os.path.relpath(_art_path(g, kind), root)] = 1
        new_f += (r0 == 'new')
        chg_f += (r0 == 'chg')
        for ep in data['eps']:
            sn = int(ep.get('season_number') or 0)
            en = int(ep.get('episode_number') or 0)
            if not en:
                continue
            eg = ep['guid']
            edir = os.path.join(sdir, 'Season %d' % max(sn, 1))
            base = os.path.join(edir, 'S%02dE%02d_%s' % (max(sn, 1), en, eg[:8]))
            r1 = _write(base + '.strm', _play_url(eg))
            still = _art_path(eg, 'still')
            if ep.get('still_path'):
                art_tasks.append((eg, 'still', ep['still_path'], 640))
            r2 = _write(base + '.nfo', _episode_nfo(ep, still))
            manifest[os.path.relpath(base + '.strm', root)] = 1
            manifest[os.path.relpath(base + '.nfo', root)] = 1
            manifest[os.path.relpath(_art_path(eg, 'still'), root)] = 1
            new_f += (r1 == 'new') + (r2 == 'new')
            chg_f += (r1 == 'chg') + (r2 == 'chg')

    # 清理已消失的条目
    removed = 0
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                if rel not in manifest:
                    os.remove(os.path.join(dirpath, fn))
                    removed += 1
    except OSError as e:
        _log('清理失败: %s' % e, xbmc.LOGWARNING)

    # 并行下载图片素材（首轮约 4000 张，后续增量跳过）
    got = _download_art(client, art_tasks)

    # 内容类型：直接写 MyVideos DB 的 path 表。Content.SetContent 内建
    # 在 service 上下文不生效（真机 0.7.0 实测：扫库器按 path.strContent
    # 决定导入与否，未设置的目录整目录跳过）。
    _set_content_db(mroot, 'movies')
    _set_content_db(troot, 'tvshows')
    state = {'ts': time.time(), 'movies': len(movies), 'tv': len(tvs),
             'files': len(manifest), 'art': got, 'scan_pending': 1}
    if removed:
        state['clean_pending'] = 1
    util.save_json(STATE_FILE, state)
    # service 上下文的内建调用真机实测可能不生效，扫描交由
    # run_pending_scan() 在用户进入插件根目录时（invoker/GUI 上下文）执行
    xbmc.executebuiltin('VideoLibrary.Scan(%s)' % mroot)
    xbmc.executebuiltin('VideoLibrary.Scan(%s)' % troot)
    if removed:
        xbmc.executebuiltin('VideoLibrary.Clean(false)')

    summary = '电影 %d 部、剧集 %d 部，文件 %d（新增 %d/更新 %d/清理 %d），图片 %d' % (
        len(movies), len(tvs), len(manifest), new_f, chg_f, removed, got)
    _log('同步完成: %s' % summary)
    return summary
