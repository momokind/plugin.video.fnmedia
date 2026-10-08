# -*- coding: utf-8 -*-
"""媒体库浏览：主菜单、库/类型整库直出、季/集列表

真机验证的类型体系（fnOS 飞牛影视，详见 desc.py）：
  Movie   电影        → 可播放
  TV      剧集        → 可进入（season/list 取季）
  Season  季          → 可进入（episode/list 取集）
  Episode 集          → 可播放
  Video   其他视频    → 可播放（fv_ 文件夹视图体系）

导航链路（全部真机验证通过）：
  根菜单 → mediadb/list 分库
  库/全部电影/全部剧集 → item/list 整库直出（服务端 ancestor_guid 过滤 +
                          page/page_size 分页并行拉全，desc 范围缓存）
  TV     → season/list/{tv_guid}      返回季
  Season → episode/list/{season_guid} 返回集
  其他   → item/list(parent_guid=xxx) fv_ 文件夹视图兜底
"""
from urllib.parse import urlencode, urlparse

import time

import xbmc
import xbmcgui
import xbmcplugin

from resources.lib import util, proxy, meta, desc
from resources.lib.desc import TYPE_PLAYABLE, TYPE_TV
from resources.lib.fnapi.client import ApiError


def plugin_url(params):
    return 'plugin://%s/?%s' % (util.ADDON_ID, urlencode(params))


def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _art(client, raw, width=400):
    """服务端海报路径 → 本地代理 URL（sys/img + Authx 签名，真机验证）"""
    if not raw:
        return None
    absolute = client.image_url(raw, width=width)
    if not absolute:
        return None
    return proxy.image_url(absolute)


def render_item(client, d, versions=None):
    """描述符 → xbmcgui.ListItem（详情缓存命中时附加富元数据）。

    这是所有列表的唯一渲染路径：描述符由 desc.build_item 在取数时构建
    （整库直出时已落盘），这里的富元数据从详情缓存现查——进程内 mtime
    缓存后是纯字典查找，service 预热补全详情后旧描述符自动变富。
    versions 为 desc 版本数缓存（可选）：多版本影片在标题后打"〔N版本〕"，
    列表接口无版本数字段，该缓存由 service 后台 stream_list 扫描填充。
    """
    infos = dict(d.get('infos') or {})
    label = d.get('label', '')
    if versions and not d.get('folder') and d.get('type') in ('Movie', 'Video'):
        entry = versions.get(d.get('guid', ''))
        n = (entry or {}).get('n') or 0
        if n > 1:
            label = '%s〔%d版本〕' % (label, n)
            # 版本明细写进剧情：Kodi 信息页标题取 title 标签（无后缀），
            # 不写剧情的话信息页完全看不到多版本及各版本画质
            lines = ['〔本片有 %d 个版本，右键"选择版本播放"可切换〕' % n]
            for i, vlabel in enumerate((entry.get('v') or [])[:8]):
                lines.append('%d. %s' % (i + 1, vlabel))
            plot = infos.get('plot') or ''
            infos['plot'] = '\n'.join(lines) + (('\n' + plot) if plot else '')
    li = xbmcgui.ListItem(label=label, offscreen=True)
    art = {}
    for key, absolute in (d.get('art') or {}).items():
        u = proxy.image_url(absolute) if absolute else None
        if u:
            art[key] = u

    detail_entry = meta.get_cached_detail(d.get('guid', ''))
    detail = (detail_entry or {}).get('detail') or {}
    persons = (detail_entry or {}).get('persons') or []
    if detail:
        if detail.get('original_title'):
            infos['originaltitle'] = detail['original_title']
        date = detail.get('release_date') or detail.get('air_date') or ''
        if date[:4].isdigit() and not infos.get('year'):
            infos['year'] = int(date[:4])
        genres = meta.genre_names(client, detail.get('genres'))
        if genres:
            infos['genre'] = genres
        directors = meta.crew_names(persons, 'Director')
        if directors:
            infos['director'] = ', '.join(directors[:3])
        writers = meta.crew_names(persons, 'Writer')
        if writers:
            infos['writer'] = ', '.join(writers[:3])
        countries = detail.get('production_countries') or []
        if countries:
            infos['country'] = ', '.join(str(c) for c in countries[:3])
        backdrop = _art(client, detail.get('backdrops'), width=1280)
        if backdrop:
            art.setdefault('fanart', backdrop)
        logo = _art(client, detail.get('logos'), width=800)
        if logo:
            art['clearlogo'] = logo

    util.apply_video_info(li, infos)

    if not d.get('playable'):
        art.setdefault('thumb', 'DefaultFolder.png')
        art.setdefault('icon', 'DefaultFolder.png')
    if art:
        li.setArt(art)

    # 演员表（信息对话框/演员视图）
    if persons:
        cast = meta.build_cast(client, persons)
        if cast:
            util.set_video_cast(li, cast)

    if d.get('playable'):
        li.setProperty('IsPlayable', 'true')
        li.addContextMenuItems([
            ('选择版本播放', 'RunPlugin(%s)' % plugin_url({'action': 'source', 'guid': d['guid']})),
            ('标记为已观看', 'RunPlugin(%s)' % plugin_url({'action': 'watched', 'guid': d['guid']})),
        ])
    return li


def _list_item(client, it, playable, guessed_type=None, url=None, folder=None):
    """季/集/文件夹视图等小列表沿用入口：现构建描述符再渲染（同一条路径）"""
    d = desc.build_item(client, it, playable=playable, guessed_type=guessed_type,
                        url=url, folder=folder)
    return render_item(client, d)


def _prepare():
    """准备客户端：不再发 user/info 校验往返（每次列表加载 ~115ms）。

    client.call() 自带 401/403 自动重登+重试，等价；token 过期由首个
    API 调用懒触发重登。回调必须在任何 API 调用前就位以持久化新 token。
    """
    client = util.ensure_client()
    client.on_token_refresh = lambda token: util.set_setting('token', token)
    client.on_base_change = lambda base: util.set_setting('server', base)
    proxy.set_client(client)
    return client


def _end(handle, items, content='videos', succeeded=True, title=None):
    xbmcplugin.setContent(handle, content)
    if title:
        xbmcplugin.setPluginCategory(handle, title)
    xbmcplugin.addDirectoryItems(handle, items, totalItems=len(items))
    xbmcplugin.endOfDirectory(handle, succeeded=succeeded, updateListing=False)


def _empty(handle, message):
    li = xbmcgui.ListItem(label=message, offscreen=True)
    li.setArt({'thumb': 'DefaultAddonNone.png', 'icon': 'DefaultAddonNone.png'})
    _end(handle, [('', li, False)])


def _static_item(label, icon):
    li = xbmcgui.ListItem(label=label, offscreen=True)
    li.setArt({'thumb': icon, 'icon': icon})
    return li


# ---------------------------------------------------------------------- 根菜单

def root(handle):
    """根菜单：分库显示（mediadb/list 真机验证）

    每个媒体项的 ancestor_guid 即所属库 guid；item/list 支持
    ancestor_guid 服务端按库过滤（真机验证），进入具体库时整库直出。
    """
    client = _prepare()

    libraries = []
    try:
        libraries = client.mediadb_list()
    except ApiError as e:
        util.log('mediadb/list 失败: %s' % e.message, xbmc.LOGWARNING)

    items = []
    for mdb in libraries:
        # 跳过 IPTV 直播库（内容不是点播媒体）
        if (mdb.get('category') or '').upper() == 'IPTV':
            continue
        title = mdb.get('title') or '未命名库'
        guid = mdb.get('guid')
        if not guid:
            continue
        li = _static_item(title, 'DefaultFolder.png')
        # 库海报（真机验证 sys/img 规则）
        posters = mdb.get('posters') or []
        if posters:
            art_url = _art(client, posters[0])
            if art_url:
                li.setArt({'thumb': art_url, 'poster': art_url, 'icon': art_url})
        items.append((
            plugin_url({'action': 'filter', 'mdb': guid, 'title': title,
                        'dv': desc.data_stamp()}),
            li, True,
        ))

    # 全局入口（dv= 数据版本戳：预热数据更新后 URL 变化，Kodi 目录缓存
    # 随之失效，避免一直展示旧容器——信息页缺演员/版本标记的元凶）
    stamp = desc.data_stamp()
    items.append((plugin_url({'action': 'recent', 'dv': stamp}),
                  _static_item('最近添加', 'DefaultRecentlyAddedMovies.png'), True))
    items.append((plugin_url({'action': 'filter', 'type': 'Movie', 'title': '全部电影', 'dv': stamp}),
                  _static_item('全部电影', 'DefaultMovies.png'), True))
    items.append((plugin_url({'action': 'filter', 'type': 'TV', 'title': '全部剧集', 'dv': stamp}),
                  _static_item('全部剧集', 'DefaultTVShows.png'), True))
    items.append((plugin_url({'action': 'genres', 'dv': stamp}),
                  _static_item('分类浏览', 'DefaultGenre.png'), True))
    items.append((plugin_url({'action': 'search'}),
                  _static_item('搜索', 'DefaultAddonsSearch.png'), True))
    # 媒体库同步状态行（配置后才显示）：最近一次同步概况，点击请求
    # 立即同步（service 常驻循环消费，插队于版本扫描等重活之前执行）
    try:
        from resources.lib import libsync
        sync_label, sync_stamp_ = libsync.sync_status_label()
        if sync_label:
            items.append((plugin_url({'action': 'sync', 'st': sync_stamp_, 'dv': stamp}),
                          _static_item(sync_label, 'DefaultAddonService.png'), True))
    except Exception as e:
        util.log('同步状态行构建失败: %s' % e, xbmc.LOGWARNING)
    items.append((plugin_url({'action': 'relogin'}),
                  _static_item('重新登录（刷新令牌）', 'DefaultUser.png'), True))

    _end(handle, items, content='files', title='飞牛影视')
    # 媒体库同步的扫库在 service 上下文可能不生效，进入根目录时补触发；
    # 首次配置/切换服务器后自动登记一次立即同步请求（service 秒级消费）
    try:
        from resources.lib import libsync
        libsync.run_pending_scan()
        libsync.maybe_request_first_sync()
    except Exception as e:
        util.log('媒体库扫库触发失败: %s' % e, xbmc.LOGWARNING)


def sync_action(handle, params):
    """状态行点击：登记立即同步请求并刷新根菜单。

    重活由 service 常驻循环执行（invoker 内跑会拖住 Kodi 插件进程，
    首轮 40k 图片下载可长达数十分钟）；请求在描述符就绪后插队消费。"""
    from resources.lib import libsync
    if util.get_setting('libsync', 'true') != 'true':
        util.notify('媒体库同步已在设置中停用')
    else:
        libsync.request_sync()
        util.notify('已请求同步，即将在后台执行（右下角显示进度）')
    root(handle)


# ---------------------------------------------------------------------- 库/类型视图（整库直出）

# ------------------------------------------------------------------ 首字母快跳

LETTER_JUMP_MIN = 200     # 墙条数达到该值才预置字母夹，避免小列表噪音


def _letter_of(d):
    """描述符 → 分组键（A-Z / 0-9 / 其它）"""
    return util.pinyin_initial(d.get('label', ''))


def _letter_jump_items(base_params, descs):
    """字母夹行：仅列出墙里实际存在的分组（A-Z / 0-9 / 其它）"""
    from collections import Counter
    counts = Counter(_letter_of(d) for d in descs)
    items = []
    for letter in [chr(c) for c in range(ord('A'), ord('Z') + 1)] + ['0-9', '其它']:
        if counts.get(letter):
            params = dict(base_params)
            params['letter'] = letter
            li = _static_item(letter, 'DefaultFolder.png')
            items.append((plugin_url(params), li, True))
    return items


def _apply_letter(descs, letter):
    if not letter or letter == '全部':
        return descs
    return [d for d in descs if _letter_of(d) == letter]


def filter_list(handle, params):
    """库/全部电影/全部剧集：整库直出——一面墙就是整库，无翻页项

    数据层（真机验证）：item/list 支持 ancestor_guid 服务端按库过滤与
    page/page_size 分页（不带分页参数时服务端默认 page_size=500 会静默
    截断大库，务必走 item_list_walk）。整库并行拉取一次 → desc 描述符
    落盘（30 分钟 TTL）→ 渲染只做 ListItem 构造；TTL 内再次进入零网络
    零解析。service 进程开机后台预热各库范围，日常进入直接命中。
    网络失败回退过期描述符（陈旧总比白屏好）。
    大墙（≥LETTER_JUMP_MIN 条）预置 A-Z/0-9 字母夹快跳（GB2312 区位法
    拼音首字母，零依赖）。
    """
    wanted_mdb = params.get('mdb', '')
    wanted_type = params.get('type', '')
    title = params.get('title', '')
    letter = params.get('letter', '')
    page = max(_to_int(params.get('page')), 1)
    client = _prepare()
    key = desc.scope_key(client, wanted_mdb, wanted_type)

    scope = desc.load_scope(key)
    if scope is None:
        t0 = time.time()
        try:
            desc.build_scope(client, mdb_guid=wanted_mdb, type_filter=wanted_type,
                             title=title)
        except Exception as e:
            scope = desc.load_scope(key, allow_expired=True)
            if scope is None:
                message = e.message if isinstance(e, ApiError) else str(e)
                util.notify('加载失败：%s' % message, error=True)
                _empty(handle, '加载失败：%s' % message)
                return
            util.log('整库拉取失败，回退过期缓存: %s' % e, xbmc.LOGWARNING)
        else:
            scope = desc.load_scope(key) or {'items': []}
            util.log('整库直出 %s：拉取+构建 %.1fs（%d 条）；service 预热后此路径可免'
                     % (title or wanted_mdb[:12] or wanted_type, time.time() - t0,
                        len(scope.get('items') or [])))

    descs = scope.get('items') or []
    content = 'movies' if wanted_type == 'Movie' else ('tvshows' if wanted_type == 'TV' else 'videos')
    versions = desc.load_versions()   # guid→版本数（service 后台扫描，多版本标记）

    # 首字母快跳：仅聚合大墙（全部电影/全部剧集，wanted_type 非空；单个
    # 库的墙保持纯净）预置字母夹行；带 letter 时过滤
    if wanted_type and not letter and len(descs) >= LETTER_JUMP_MIN:
        base = {k: v for k, v in params.items() if k in ('mdb', 'type', 'title')}
        items = _letter_jump_items(base, descs)
        items += [(d.get('url', ''), render_item(client, d, versions), bool(d.get('folder')))
                  for d in descs]
        _end(handle, items, content=content,
             title='%s（选字母快跳）' % (title or '媒体库'))
        return
    if letter:
        before = len(descs)
        descs = _apply_letter(descs, letter)
        title = '%s [%s]（%d）' % (title or '媒体库', letter, before)
        back = dict((k, v) for k, v in params.items() if k in ('mdb', 'type', 'title'))
        back_li = _static_item('← 全部', 'DefaultFolder.png')
        items = [(plugin_url(back), back_li, True)]
        items += [(d.get('url', ''), render_item(client, d, versions), bool(d.get('folder')))
                  for d in descs]
        _end(handle, items, content=content, title=title)
        meta.queue_enrich([d.get('guid') for d in descs])
        return

    # 保底分页：设置"每页条数">0 时切片 + "下一页"条目（默认 0=整库直出）
    page_size = _to_int(util.get_setting('pagesize', '0'))
    next_item = None
    if page_size > 0 and len(descs) > page_size:
        total_pages = (len(descs) + page_size - 1) // page_size
        descs = descs[(page - 1) * page_size: page * page_size]
        if page < total_pages:
            li = xbmcgui.ListItem(label='下一页（%d/%d）' % (page + 1, total_pages),
                                  offscreen=True)
            li.setArt({'thumb': 'DefaultFolder.png', 'icon': 'DefaultFolder.png'})
            next_item = (plugin_url({'action': 'filter', 'mdb': wanted_mdb,
                                     'type': wanted_type, 'title': title,
                                     'page': page + 1}), li, True)

    items = [(d.get('url', ''), render_item(client, d, versions), bool(d.get('folder')))
             for d in descs]
    if next_item:
        items.append(next_item)

    _end(handle, items, content=content, title=title or '媒体库')
    # 后台补全详情（背景画/演员表），下次进入即为富显示
    meta.queue_enrich([d.get('guid') for d in descs])


# ---------------------------------------------------------------------- 最近添加 / 搜索

def recent(handle, params):
    """最近添加：服务端 create_time DESC（真机验证支持），现拉现渲染。

    动态视图不走描述符缓存（内容随入库变化）；只取一页，零额外成本。
    """
    title = '最近添加'
    client = _prepare()
    try:
        data = client.item_list(ancestor_guid='', page=1, page_size=60,
                                sort_column='create_time', sort_type='DESC')
    except ApiError as e:
        util.notify('加载失败：%s' % e.message, error=True)
        _empty(handle, '加载失败：%s' % e.message)
        return
    entries = [e for e in (data.get('list') or [])
               if e.get('type') in ('Movie', 'TV', 'Video', 'Episode')]
    versions = desc.load_versions()
    items = []
    for e in entries:
        d = desc.build_item(client, e)
        items.append((d['url'], render_item(client, d, versions), d['folder']))
    _end(handle, items, content='videos', title=title)
    meta.queue_enrich([d['guid'] for d in entries])


def search(handle, params):
    """本地搜索：跨所有范围描述符过滤（label/剧名，不区分大小写）。

    零 API——搜索的是已预热的整库描述符；输入框取消则安静返回。
    """
    import re as _re
    kw = (params.get('kw') or '').strip()
    if not kw:
        import xbmcgui as _g
        kw = (_g.Dialog().input('搜索影片/剧集', type=_g.INPUT_ALPHANUM) or '').strip()
        if not kw:
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return
    client = _prepare()
    pattern = _re.compile(_re.escape(kw), _re.IGNORECASE)
    picked = []
    seen = set()
    for d in _all_descriptors(client).values():
        g = d.get('guid', '')
        if g in seen:
            continue
        seen.add(g)
        infos = d.get('infos') or {}
        hay = '%s %s %s' % (d.get('label', ''), infos.get('title', ''),
                            infos.get('tvshowtitle', ''))
        if pattern.search(hay):
            picked.append(d)
    picked.sort(key=lambda d: d.get('label', ''))
    versions = desc.load_versions()
    items = [(d.get('url', ''), render_item(client, d, versions), bool(d.get('folder')))
             for d in picked]
    _end(handle, items, content='videos', title='搜索：%s（%d）' % (kw, len(picked)))
    meta.queue_enrich([d.get('guid') for d in picked])


# ---------------------------------------------------------------------- 分类浏览（分库）

def _all_descriptors(client):
    """汇集所有范围描述符（guid → descriptor），跨库去重（保留先见者）"""
    merged = {}
    for key in desc.scope_keys(client):
        scope = desc.load_scope(key, allow_expired=True) or {}
        for d in scope.get('items') or []:
            g = d.get('guid', '')
            if g and g not in merged:
                merged[g] = d
    return merged


def genres_list(handle, params):
    """分类浏览首页：流派列表（按条目数降序），数据来自本地分类索引"""
    title = '分类浏览'
    client = _prepare()
    # 索引缺失/过期时现场构建（本地聚合，秒级）
    built = meta.build_genre_index(client)
    if built:
        util.log('分类索引构建完成: %d 个流派' % built)
    index = meta.load_genre_index()
    genres = index.get('genres') or {}
    if not genres:
        _empty(handle, '分类索引尚未就绪，稍后再试')
        return
    stamp = desc.data_stamp()
    ranked = sorted(genres.items(), key=lambda kv: -len(kv[1]))
    items = []
    for name, guids in ranked:
        if not guids:
            continue
        li = _static_item('%s（%d）' % (name, len(guids)), 'DefaultGenre.png')
        items.append((
            plugin_url({'action': 'genre', 'genre': name, 'title': name, 'dv': stamp}),
            li, True,
        ))
    _end(handle, items, content='files', title=title)


def genre_items(handle, params):
    """单个流派的全部影片（跨库聚合、按 guid 去重、整库直出、字母快跳）"""
    name = params.get('genre', '')
    title = params.get('title') or name
    letter = params.get('letter', '')
    page = max(_to_int(params.get('page')), 1)
    client = _prepare()
    guids = set((meta.load_genre_index().get('genres') or {}).get(name) or [])
    if not guids:
        _empty(handle, '该分类暂无条目')
        return

    picked = [d for g, d in _all_descriptors(client).items() if g in guids]
    picked.sort(key=lambda d: d.get('label', ''))
    versions = desc.load_versions()

    # 首字母快跳（与整库墙同规则）
    if not letter and len(picked) >= LETTER_JUMP_MIN:
        base = {k: v for k, v in params.items() if k in ('genre', 'title', 'dv')}
        items = _letter_jump_items(base, picked)
        items += [(d.get('url', ''), render_item(client, d, versions), bool(d.get('folder')))
                  for d in picked]
        _end(handle, items, content='videos', title='%s（选字母快跳）' % title)
        return
    if letter:
        before = len(picked)
        picked = _apply_letter(picked, letter)
        title = '%s [%s]（%d）' % (title, letter, before)
        back = {k: v for k, v in params.items() if k in ('genre', 'title', 'dv')}
        back_li = _static_item('← 全部', 'DefaultFolder.png')
        items = [(plugin_url(back), back_li, True)]
        items += [(d.get('url', ''), render_item(client, d, versions), bool(d.get('folder')))
                  for d in picked]
        _end(handle, items, content='videos', title=title)
        meta.queue_enrich([d.get('guid') for d in picked])
        return

    page_size = _to_int(util.get_setting('pagesize', '0'))
    next_item = None
    if page_size > 0 and len(picked) > page_size:
        total_pages = (len(picked) + page_size - 1) // page_size
        picked = picked[(page - 1) * page_size: page * page_size]
        if page < total_pages:
            li = xbmcgui.ListItem(label='下一页（%d/%d）' % (page + 1, total_pages),
                                  offscreen=True)
            li.setArt({'thumb': 'DefaultFolder.png', 'icon': 'DefaultFolder.png'})
            next_item = (plugin_url({'action': 'genre', 'genre': name, 'title': title,
                                     'page': page + 1, 'dv': params.get('dv', '')}),
                         li, True)

    items = [(d.get('url', ''), render_item(client, d, versions), bool(d.get('folder')))
             for d in picked]
    if next_item:
        items.append(next_item)
    _end(handle, items, content='videos', title=title)
    meta.queue_enrich([d.get('guid') for d in picked])


# ---------------------------------------------------------------------- TV / 季 / 集

def tv_detail(handle, params):
    """剧集 → 季列表（season/list 真机验证）"""
    guid = params.get('guid', '')
    title = params.get('title', '')
    client = _prepare()

    try:
        seasons = client.season_list(guid)
    except ApiError as e:
        util.notify('加载季列表失败：%s' % e.message, error=True)
        _empty(handle, '加载失败：%s' % e.message)
        return

    if not seasons:
        _empty(handle, '该剧没有季信息')
        return

    items = []
    for s in seasons:
        season_url = plugin_url({'action': 'season', 'guid': s['guid'],
                                 'title': s.get('title', ''), 'tv': title})
        items.append((
            season_url,
            _list_item(client, s, playable=False, guessed_type='Season',
                       url=season_url, folder=True),
            True,
        ))
    _end(handle, items, content='tvshows', title=title or '剧集')
    meta.queue_enrich([s.get("guid") for s in seasons])


def season_detail(handle, params):
    """季 → 集列表（episode/list 真机验证）"""
    guid = params.get('guid', '')
    title = params.get('title', '')
    tv_title = params.get('tv', '')
    client = _prepare()

    try:
        episodes = client.episode_list(guid)
    except ApiError as e:
        util.notify('加载集列表失败：%s' % e.message, error=True)
        _empty(handle, '加载失败：%s' % e.message)
        return

    if not episodes:
        _empty(handle, '该季没有剧集')
        return

    items = []
    for ep in episodes:
        ep = dict(ep)
        if tv_title and not ep.get('tv_title'):
            ep['tv_title'] = tv_title
        play_url = plugin_url({'action': 'play', 'guid': ep['guid']})
        items.append((
            play_url,
            _list_item(client, ep, playable=True, guessed_type='Episode',
                       url=play_url, folder=False),
            False,
        ))
    _end(handle, items, content='episodes', title='%s %s' % (tv_title, title) if tv_title else title)
    meta.queue_enrich([ep.get("guid") for ep in episodes])


# ---------------------------------------------------------------------- 通用浏览（兜底）

def browse(handle, params):
    """通用 GUID 浏览：item/list 优先，空则尝试 episode/list（fv_ 文件夹视图/兜底）

    文件夹视图子项天然少量（真机实测顶级也仅几十条），保持全量拉取；
    服务端对非空 parent_guid 忽略分页参数，无需 walk。
    """
    guid = params.get('guid', '')
    title = params.get('title', '')
    client = _prepare()

    sort_type = util.get_setting('sortorder', 'ASC') or 'ASC'

    try:
        data = client.item_list_cached(parent_guid=guid, exclude_folder=0, sort_type=sort_type)
    except ApiError as e:
        util.notify('加载失败：%s' % e.message, error=True)
        _empty(handle, '加载失败：%s' % e.message)
        return

    entries = data.get('list') or []

    if not entries:
        try:
            episodes = client.episode_list(guid)
        except ApiError:
            episodes = []
        if episodes:
            items = [(
                plugin_url({'action': 'play', 'guid': ep['guid']}),
                _list_item(client, ep, playable=True, guessed_type='Episode',
                           url=plugin_url({'action': 'play', 'guid': ep['guid']}),
                           folder=False),
                False,
            ) for ep in episodes]
            _end(handle, items, content='episodes', title=title or '剧集')
            return
        _empty(handle, '该目录为空')
        return

    items = []
    for it in entries:
        d = desc.build_item(client, it)
        items.append((d['url'], render_item(client, d), d['folder']))

    content = 'videos'
    if entries and all(e.get('type') == 'Episode' for e in entries):
        content = 'episodes'
    _end(handle, items, content=content, title=title or (data.get('mdb_name') or ''))


# ---------------------------------------------------------------------- 手动 GUID

def manual_guid(handle):
    text = xbmcgui.Dialog().input('输入 GUID 或粘贴 Web 链接', type=xbmcgui.INPUT_ALPHANUM)
    if not text:
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    guid = _extract_guid(text)
    if not guid:
        util.notify('未能从输入中识别出 GUID', error=True)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    url = plugin_url({'action': 'browse', 'guid': guid, 'title': 'GUID %s' % guid[:12]})
    xbmcplugin.endOfDirectory(handle, succeeded=True, updateListing=False)
    xbmc.executebuiltin('Container.Update(%s)' % url)


def _extract_guid(text):
    text = (text or '').strip().rstrip('/')
    if not text:
        return ''
    if '/' not in text and '?' not in text:
        return text
    parsed = urlparse(text if text.startswith('http') else 'http://x/' + text.lstrip('/'))
    segments = [s for s in parsed.path.split('/') if s]
    if segments:
        return segments[-1]
    from urllib.parse import parse_qs
    qs = parse_qs(parsed.query)
    if qs.get('guid'):
        return qs['guid'][0]
    return ''
