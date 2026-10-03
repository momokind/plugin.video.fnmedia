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
  - 扫库触发：JSON-RPC TCP 9090 优先（service 上下文可靠，不依赖 GUI），
    executebuiltin + scan_pending（进插件根目录补扫）兜底。
  - 演员头像 art 修复：Kodi 重扫对 actor art 只补缺不覆盖，旧版导入的
    必失败服务器 URL 记录会一直滞留——同步后直写 MyVideos DB 把已
    本地化的演员头像记录修正/补全为本地路径（幂等）。
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
ART_WORKERS = 12
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

    已存在且非空的文件跳过（增量）；失败静默跳过（下轮同步重试）。
    progress(done, total) 供同步进度显示。返回成功下载数。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
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
        # 直连优先（带 Authx 签名，省去本机代理一跳提速），失败回退代理。
        # 服务端图片按需生成：首次请求可能 404（冷启动，真机实测），整体重试
        direct_headers = {}
        try:
            if client.token and abs_url.startswith(client.base):
                direct_headers = {'Authorization': client.token,
                                  'Authx': client.authx_for(abs_url[len(client.base):])}
        except Exception:
            direct_headers = {}
        import urllib.request
        for attempt in (1, 2, 3):
            data = None
            try:
                req = urllib.request.Request(abs_url, headers=direct_headers)
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = r.read()
            except Exception:
                data = None
            if not data:
                try:
                    with urllib.request.urlopen(url, timeout=25) as r:
                        data = r.read()
                except Exception as e:
                    _log('图片下载失败 %s: %s' % (os.path.basename(path), str(e)[:50]),
                         xbmc.LOGDEBUG)
            if data and len(data) > 1024:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(data)
                return True
            time.sleep(3)
        return False

    ok = 0
    with ThreadPoolExecutor(max_workers=ART_WORKERS) as pool:
        futs = [pool.submit(_one, t) for t in todo]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                if f.result():
                    ok += 1
            except Exception:
                pass
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
            # 集编号稳定化：服务端跨轮返回的 (季,集) 编号可能漂移（真机
            # 实测同一集编号变化 → NFO 内容变 → Kodi 扫库器按（季,集）
            # 匹配不到旧记录插入新行 → 库里重复集），以 guid 为键锁定
            # 首次分配的编号，后续轮次沿用
            old = ((e.get('data') or {}).get('eps') or []) if e else []
            if old:
                locked = {ep.get('guid'): (ep.get('season_number'),
                                           ep.get('episode_number'))
                          for ep in old if ep.get('guid')}
                for ep in eps:
                    if ep.get('guid') in locked:
                        ep['season_number'], ep['episode_number'] = locked[ep['guid']]
            out[g] = {'seasons': seasons, 'eps': eps}
            cache[g] = {'ts': now, 'data': out[g]}
            time.sleep(0.1)   # 温和限速
        except Exception as e:
            _log('剧集结构获取失败 %s…: %s' % (g[:12], e), xbmc.LOGWARNING)
    util.save_json(TV_WALK_FILE, cache)
    return out


def _set_content_db(path_, content):
    """在 MyVideos DB 的 path 表登记一个库目录并设置内容类型。

    Kodi 的扫库器按 path.strContent 决定导入；'metadata.local' 刮削器
    表示仅用本地 NFO（我们的 NFO 是全量的，扫库零联网）。
    0.7.x 两处实测 bug（真机 21.3）：①旧签名 (mroot, troot) 与调用方
    (路径, 类型) 错配，把 tvshows 根写成 movies 并留下 'movies'/'tvshows'
    字面量垃圾行；②strPath 缺尾斜杠与扫库器归一化路径不匹配，Scan 1ms
    空跑整库不入库。本版按单行登记 + 尾斜杠 + 旧行原位迁移 + 垃圾行清理。"""
    import glob as _glob
    import sqlite3
    dbdir = os.path.join(util.translate_path('special://profile'), 'Database')
    dbs = sorted(_glob.glob(os.path.join(dbdir, 'MyVideos*.db')))
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
    """触发扫库/清理。Scan 走 JSON-RPC TCP 9090（service 上下文可靠，
    真机 21.3 实测无弹窗）；失败回退 executebuiltin（GUI 上下文可靠），
    调用方应保留 scan_pending 由 run_pending_scan() 兜底。
    Clean 不走 JSON-RPC：21.3 实测 displayDialog 参数被拒、无参调用会弹
    确认框并阻塞 JSON-RPC 线程（连 Ping 都不响应）——只走 executebuiltin
    （带 false 参数无弹窗），clean_pending 始终保留兜底。"""
    ok = True
    for root in (mroot, troot):
        # 目录参数必须带尾斜杠：真机 21.3 实测不带斜杠时 Scan 1ms 空跑
        root = root if root.endswith('/') else root + '/'
        resp = _jsonrpc('VideoLibrary.Scan', {'directory': root})
        if not (isinstance(resp, dict) and resp.get('result') == 'OK'):
            ok = False
    if not ok:
        for root in (mroot, troot):
            xbmc.executebuiltin('VideoLibrary.Scan(%s)' % root)
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
    import glob as _glob
    import sqlite3
    dbdir = os.path.join(util.translate_path('special://profile'), 'Database')
    dbs = sorted(_glob.glob(os.path.join(dbdir, 'MyVideos*.db')))
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
    等非本目录 URL 不受影响。返回更新条数。"""
    import glob as _glob
    import sqlite3
    dbdir = os.path.join(util.translate_path('special://profile'), 'Database')
    dbs = sorted(_glob.glob(os.path.join(dbdir, 'MyVideos*.db')))
    if not dbs:
        return 0
    pairs = []
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.endswith('.webp'):
                    old = os.path.join(dirpath, fn[:-5] + '.jpg')
                    if not os.path.exists(old):
                        pairs.append((os.path.join(dirpath, fn), old))
    except OSError:
        return 0
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
        return 0


def _migrate_actor_thumbs(actor_dir):
    """演员头像 .jpg（webp 内容）→ .webp，只改后缀、内容零改动。

    Kodi 图片加载按扩展名选解码器：.jpg → mjpeg，解 webp 数据必失败
    （真机 21.3 实测原生库信息页演员头像全剪影）；服务端图片源文件
    本身就是 webp，与内容一致的后缀才能走 webp 解码器。幂等。
    返回重命名数。"""
    n = 0
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
    if not force and time.time() - state.get('ts', 0) < SYNC_TTL:
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
        # 旧 .jpg 命名（webp 内容）迁移为 .webp（只改后缀，内容零改动）
        _migrate_actor_thumbs(os.path.join(util.addon_data_dir(), meta.ACTOR_ART_DIR))
        _migrate_art_files(os.path.join(root, ART_DIR))
        _migrate_art_urls(os.path.join(root, ART_DIR))
        manifest = {}
        art_tasks = []
        actor_local = {}   # 演员名 → 本地头像路径（art 修复用）
        new_f = chg_f = 0

        # 剧集结构（24h 读缓存，温和限速）
        prog.update(5, '获取 %d 部剧集的季/集结构…' % len(tvs))
        tv_data = _tv_walk(client, [d['guid'] for d in tvs])
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
            _actor_tasks(client, det_entry, art_tasks)
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
            _actor_tasks(client, det_entry, art_tasks)
            _collect_actor_local(det_entry, actor_local)
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

        # 清理已消失的条目
        prog.update(45, '清理已消失条目…')
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

        # 并行下载图片素材（首轮约 4 万张，后续增量跳过）
        prog.update(47, '下载图片素材…')

        def _art_progress(done, total):
            prog.update(47 + 48.0 * done / total, '下载图片 %d/%d' % (done, total))

        got = _download_art(client, art_tasks, progress=_art_progress)

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
                 'files': len(manifest), 'art': got, 'scan_pending': 1}
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
        return summary
    finally:
        prog.close()
