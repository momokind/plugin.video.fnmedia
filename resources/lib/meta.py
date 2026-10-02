# -*- coding: utf-8 -*-
"""富元数据：详情图（背景画/标志）/ 演员 / 流派 的获取与落盘缓存

数据源（真机验证）：
  GET  /v/api/v1/item/{guid}          logos/backdrops/posters/genres/original_title...
  POST /v/api/v1/person/list/{guid}   {total, list:[{name, role, job, order, profile_path, biography}]}
  GET  /v/api/v1/tag/genres           [{id, value}]

策略：列表浏览时不阻塞——先用已缓存的详情渲染（海报墙的 fanart/clearlogo、
演员表），再由后台守护线程限量补全缺失条目；下次进入列表即为富显示。
"""
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import xbmc

from resources.lib import util

DETAIL_CACHE_FILE_LEGACY = 'item_detail_cache.json'   # 0.4.1 及以前的单文件缓存
DETAIL_DIR = 'detail'    # 分片存储：detail/<guid>.json（每条目独立文件）
GENRE_CACHE_FILE = 'genre_cache.json'
DETAIL_TTL = 12 * 3600   # 详情缓存 12 小时
GENRE_TTL = 24 * 3600    # 流派映射缓存 24 小时
ENRICH_BATCH = 48        # 每轮并行补全的条数上限（4 线程约 8s/轮）
ENRICH_WORKERS = 4       # 详情+演员两连发按条目并行
DETAIL_MAX_RETRY = 3     # 单条目连续失败 N 次后放弃（防永久坏条目占队）
# 渲染合并实际消费的字段（render_item / play），瘦身存储控制体积
_DETAIL_KEYS = ('original_title', 'release_date', 'air_date', 'genres',
                'production_countries', 'backdrops', 'logos', 'posters')
_PERSON_KEYS = ('name', 'role', 'job', 'order', 'profile_path')

_detail_memo = {}        # guid -> ((mtime_ns, size), entry)：读路径进程内缓存
_legacy_dropped = False
# 旧版"单文件整读改写"（item_detail_cache.json）在多线程/多进程下互相
# 覆盖丢条目（0.4.1 真机实测：1323 条队列跑完只剩 775 条且无报错），
# 0.4.2 起改为每条目一个分片文件，写冲突只剩"同 guid 互盖"（无害）。


def _load(name):
    """读 JSON 缓存文件（进程内带 mtime+size 校验的缓存，见 util.load_json_cached）

    列表渲染对每个条目都会调 get_cached_detail / genre_names——若每次都
    全量读+解析 item_detail_cache.json（4.4MB），50 个条目实测 2.1s+；
    进程内缓存后仅首次解析（实测 38ms），后续全部命中内存。
    写方（本进程 store_detail 或 service 进程预热）落盘会更新 mtime，
    下次读取自动重载，不惧数据过期。"""
    data = util.load_json_cached(name)
    return data if isinstance(data, dict) else {}


def _detail_path(guid):
    return os.path.join(util.addon_data_dir(), DETAIL_DIR, guid + '.json')


def get_cached_detail(guid, ignore_ttl=False):
    """读取详情分片 {ts, detail, persons}（进程内按 mtime 校验缓存）。

    ignore_ttl=True 供媒体库同步等离线场景使用：流派/演员是静态元数据，
    不应因分片超过 12h 而被视为缺失（否则生成的 NFO 永远没有流派演员）。"""
    if not guid:
        return None
    path = _detail_path(guid)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _detail_memo.get(guid)
    if hit and hit[0] == key:
        return hit[1]
    entry = util.load_json(os.path.relpath(path, util.addon_data_dir()))
    if not isinstance(entry, dict):
        return None
    if not ignore_ttl and time.time() - entry.get('ts', 0) >= DETAIL_TTL:
        return None
    _detail_memo[guid] = (key, entry)
    return entry


def store_detail(guid, detail, persons):
    """落盘单条详情分片（每条目独立文件，多线程/多进程写互不覆盖）。

    瘦身：persons 只留渲染消费的字段（演员按 order 取前 40 + 全部导演/
    编剧，丢弃 biography 等大字段），详情只留渲染合并用的键。
    """
    global _legacy_dropped
    slim_detail = {}
    for k in _DETAIL_KEYS:
        v = (detail or {}).get(k)
        if v not in (None, '', []):
            slim_detail[k] = v
    slim_persons = []
    for p in persons or []:
        if p.get('job') not in ('Actor', 'Director', 'Writer') or not p.get('name'):
            continue
        slim_persons.append({k: p.get(k) for k in _PERSON_KEYS})
    slim_persons.sort(key=_person_order)
    actors = [p for p in slim_persons if p['job'] == 'Actor'][:40]
    crew = [p for p in slim_persons if p['job'] != 'Actor']

    if not _legacy_dropped:
        _legacy_dropped = True
        try:
            legacy = util.data_file(DETAIL_CACHE_FILE_LEGACY)
            if os.path.isfile(legacy):
                os.remove(legacy)   # 旧单文件缓存（竞态已弃用），直接回收
        except OSError:
            pass
    try:
        os.makedirs(os.path.join(util.addon_data_dir(), DETAIL_DIR), exist_ok=True)
    except OSError:
        pass
    util.save_json(os.path.join(DETAIL_DIR, guid + '.json'),
                   {'ts': time.time(), 'detail': slim_detail, 'persons': actors + crew})


def fetch_detail(client, guid):
    """同步获取详情+演员表并写缓存（两个数据库查询，真机实测 <200ms）。

    电视剧的演员挂在"季"层级（真机验证 2026-09-06：person/list 对 TV
    与 Episode 层均返回 0 人，Season 层才有）——TV 条目自动从前 3 季
    聚合演员并按姓名去重。"""
    detail = client.call('get', '/v/api/v1/item/%s' % guid) or {}
    persons = []
    try:
        data = client.call('post', '/v/api/v1/person/list/%s' % guid) or {}
        persons = data.get('list') or []
    except Exception as e:
        util.log('person/list 失败 %s: %s' % (guid[:12], e), xbmc.LOGDEBUG)
    if not persons and detail.get('type') == 'TV':
        seen = set()
        try:
            seasons = client.call('get', '/v/api/v1/season/list/%s' % guid) or []
            for s in seasons[:3]:
                data = client.call('post', '/v/api/v1/person/list/%s'
                                   % (s.get('guid') or '')) or {}
                for p in (data.get('list') or []):
                    name = p.get('name')
                    if name and name not in seen:
                        seen.add(name)
                        persons.append(p)
        except Exception as e:
            util.log('剧集演员聚合失败 %s: %s' % (guid[:12], e), xbmc.LOGDEBUG)
    store_detail(guid, detail, persons)
    return detail, persons


ENRICH_QUEUE_FILE = 'enrich_queue.json'


def queue_enrich(guids):
    """把待预热的 guid 追加到队列文件，由常驻 service 进程消费。

    列表浏览时若在插件 invoker 内直接起线程做 fetch_detail，该线程会让
    invoker 多活数秒，而 Kodi 播放器 OpenFile 卡在 invoker 退出上 →
    点播首屏多卡 1-3s。改写队列文件（瞬时），预热挪到 service 进程，
    不卡任何 invoker。
    """
    try:
        seen = set()
        queue = []
        data = util.load_json(ENRICH_QUEUE_FILE)
        if isinstance(data, list):
            for g in data:
                if isinstance(g, str) and g and g not in seen:
                    queue.append(g)
                    seen.add(g)
        for g in guids:
            if isinstance(g, str) and g and g not in seen:
                queue.append(g)
                seen.add(g)
        if len(queue) > 5000:
            queue = queue[-5000:]   # 整库预热一次可排数千 guid（须覆盖全库，勿截断）
        util.save_json(ENRICH_QUEUE_FILE, queue)
    except Exception as e:
        util.log('写入预热队列失败: %s' % e, xbmc.LOGDEBUG)


def background_enrich(client, guids):
    """后台补全缺失的详情缓存（守护线程、顺序、限量、失败即停）

    已弃用：直接在 invoker 内起线程会拖住 invoker 退出、卡住播放器 OpenFile。
    请改用 queue_enrich，由 service 进程消费。保留仅为兼容。
    """
    todo = []
    for guid in guids:
        if guid and get_cached_detail(guid) is None:
            todo.append(guid)
        if len(todo) >= ENRICH_BATCH:
            break
    if not todo:
        return

    def _run():
        for guid in todo:
            try:
                fetch_detail(client, guid)
                time.sleep(0.15)
            except Exception as e:
                util.log('后台补全详情失败 %s: %s' % (guid[:12], e), xbmc.LOGDEBUG)
                break

    threading.Thread(target=_run, daemon=True).start()


_fail_streak = [0, 0.0]  # 连续全败轮数, 冷却截止 ts（服务端不可达时退避防刷）
_attempts = {}           # guid -> 已失败次数


def drain_enrich_queue(client, max_items=ENRICH_BATCH):
    """并行消费预热队列：取未缓存 guid，4 线程补详情+演员表。

    整库预热（0.4.0）一次入队上千 guid，原串行 12 条/3s 需 ~15 分钟才能
    铺满一面墙的背景画/演员表——并行后全库 ~3-4 分钟补完。分片存储后
    并发写天然无冲突。失败条目保留在队列重试（WARNING 可见），连续
    DETAIL_MAX_RETRY 次失败才放弃；整轮全败连续 2 轮起指数退避。
    返回本次完成条数。
    """
    if time.time() < _fail_streak[1]:
        return 0
    queue = util.load_json(ENRICH_QUEUE_FILE)
    if not isinstance(queue, list) or not queue:
        return 0
    remaining = list(queue)
    todo = []
    for guid in list(queue):
        if len(todo) >= max_items:
            break
        if not isinstance(guid, str):
            remaining.remove(guid)
            continue
        if get_cached_detail(guid) is not None:
            remaining.remove(guid)   # 已有，清掉
            continue
        todo.append(guid)
    if not todo:
        util.save_json(ENRICH_QUEUE_FILE, remaining)
        return 0

    lock = threading.Lock()
    done = [0]
    failed = [0]

    def _one(guid):
        try:
            fetch_detail(client, guid)
        except Exception as e:
            with lock:
                failed[0] += 1
                n = _attempts.get(guid, 0) + 1
                _attempts[guid] = n
                if n >= DETAIL_MAX_RETRY:
                    if guid in remaining:
                        remaining.remove(guid)
                    util.log('详情预热连续 %d 次失败，放弃 %s…: %s'
                             % (n, guid[:12], e), xbmc.LOGWARNING)
                else:
                    util.log('详情预热失败（保留重试）%s…: %s'
                             % (guid[:12], e), xbmc.LOGWARNING)
            return
        with lock:
            done[0] += 1
            _attempts.pop(guid, None)
            if guid in remaining:
                remaining.remove(guid)

    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        list(pool.map(_one, todo))
    util.save_json(ENRICH_QUEUE_FILE, remaining)
    if done[0] == 0:
        _fail_streak[0] += 1
        if _fail_streak[0] >= 2:
            wait = min(60 * _fail_streak[0], 600)
            _fail_streak[1] = time.time() + wait
            util.log('详情预热连续 %d 轮全部失败，退避 %d 秒'
                     % (_fail_streak[0], wait), xbmc.LOGWARNING)
    else:
        _fail_streak[0] = 0
    return done[0]


GENRE_INDEX_FILE = 'genre_index.json'
GENRE_INDEX_TTL = 12 * 3600
# 服务器 tag/genres 返回英文名，内置常用对照（未收录的原样显示英文）
_GENRE_CN = {
    'Action': '动作', 'Adventure': '冒险', 'Animation': '动画', 'Comedy': '喜剧',
    'Crime': '犯罪', 'Documentary': '纪录', 'Drama': '剧情', 'Family': '家庭',
    'Fantasy': '奇幻', 'History': '历史', 'Horror': '恐怖', 'Music': '音乐',
    'Mystery': '悬疑', 'Romance': '爱情', 'Science Fiction': '科幻',
    'Sci-Fi': '科幻', 'Thriller': '惊悚', 'War': '战争', 'Western': '西部',
    'TV Movie': '电视电影', 'Reality': '真人秀', 'Kids': '儿童', 'News': '新闻',
    'Talk': '脱口秀', 'Short': '短片', 'Musical': '音乐剧', 'Sport': '体育',
    'Sci-Fi & Fantasy': '科幻与奇幻', 'War & Politics': '战争与政治',
    'Action & Adventure': '动作冒险', 'Kids & Family': '儿童与家庭',
    'Talk Show': '脱口秀', 'Documentary & Biography': '纪录与传记',
}


def _genre_map(client=None):
    """流派 ID→名称映射（24h 缓存）。键统一转字符串——JSON 往返会把
    int 键变 str，旧代码用 int 键查导致流派永远显示不出来。"""
    cache = _load(GENRE_CACHE_FILE)
    mapping = {str(k): v for k, v in (cache.get('map') or {}).items()}
    if mapping and time.time() - cache.get('ts', 0) < GENRE_TTL:
        return mapping
    if client is None:
        return mapping
    try:
        tags = client.call('get', '/v/api/v1/tag/genres') or []
        mapping = {str(t.get('id')): t.get('value') for t in tags if t.get('id') is not None}
        util.save_json(GENRE_CACHE_FILE, {'ts': time.time(), 'map': mapping})
    except Exception as e:
        util.log('tag/genres 失败: %s' % e, xbmc.LOGDEBUG)
    return mapping


def genre_names(client, genre_ids):
    """流派 id 列表 -> 中文名列表（映射表 24h 磁盘缓存，未知项显示英文）"""
    if not genre_ids:
        return []
    mapping = _genre_map(client)
    names = []
    for gid in genre_ids:
        raw = mapping.get(str(gid))
        if raw:
            names.append(_GENRE_CN.get(raw, raw))
    return names


def load_genre_index():
    """流派 → guid 列表 的分类索引（build_genre_index 产出）"""
    data = util.load_json_cached(GENRE_INDEX_FILE)
    return data if isinstance(data, dict) else {}


def build_genre_index(client=None, force=False):
    """聚合详情分片的流派字段，构建"流派 -> 条目 guid"分类索引。

    供"分类浏览"分库与 service 周期刷新使用；索引未过期返回 0。
    纯本地聚合（读分片 + 流派映射），无逐条目 API 调用。
    """
    if not force:
        idx = load_genre_index()
        if idx.get('genres') and time.time() - idx.get('ts', 0) < GENRE_INDEX_TTL:
            return 0
    mapping = _genre_map(client)
    if not mapping:
        util.log('分类索引缺少流派映射，跳过本次构建', xbmc.LOGDEBUG)
        return 0
    import glob as _glob
    index = {}
    for p in _glob.glob(os.path.join(util.addon_data_dir(), DETAIL_DIR, '*.json')):
        guid = os.path.splitext(os.path.basename(p))[0]
        try:
            entry = util.load_json(os.path.relpath(p, util.addon_data_dir()))
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        gids = (entry.get('detail') or {}).get('genres') or []
        names = []
        for gid in gids:
            raw = mapping.get(str(gid))
            if raw:
                names.append(_GENRE_CN.get(raw, raw))
        for name in names:
            index.setdefault(name, []).append(guid)
    util.save_json(GENRE_INDEX_FILE, {'ts': time.time(), 'genres': index})
    return len(index)


def _person_order(p):
    """演员排序键：order=0 是合法值（一番主演），不能用 `or 999` 兜底"""
    order = p.get('order')
    return order if isinstance(order, int) else 999


ACTOR_ART_DIR = os.path.join('art', 'actors')


def actor_thumb_path(profile_path):
    """演员头像本地路径（md5(profile_path) 命名，确定性）"""
    if not profile_path:
        return None
    key = hashlib.md5(profile_path.encode('utf-8')).hexdigest()[:16]
    return os.path.join(util.addon_data_dir(), ACTOR_ART_DIR, key + '.jpg')


def local_actor_thumb(profile_path):
    """已下载的演员头像本地路径；未下载返回 None"""
    p = actor_thumb_path(profile_path)
    try:
        return p if os.path.isfile(p) and os.path.getsize(p) > 512 else None
    except OSError:
        return None


def build_cast(client, persons, limit=40):
    """person/list 结果 -> Kodi setCast 结构（演员在前，含头像）"""
    cast = []
    for p in sorted(persons, key=_person_order):
        if p.get('job') != 'Actor' or not p.get('name'):
            continue
        entry = {
            'name': p['name'],
            'role': p.get('role') or '',
            'order': int(p.get('order') or 0),
        }
        avatar = p.get('profile_path')
        if avatar:
            # 本地优先（同步预下载），未命中回退本地代理 URL（Kodi 会缓存）
            local = local_actor_thumb(avatar)
            if local:
                entry['thumbnail'] = local
            else:
                from resources.lib import proxy
                absolute = client.image_url(avatar, width=300)
                if absolute:
                    entry['thumbnail'] = proxy.image_url(absolute)
        cast.append(entry)
        if len(cast) >= limit:
            break
    return cast


def crew_names(persons, job):
    """取指定职位（Director/Writer）的人名列表"""
    return [p.get('name') for p in persons
            if p.get('job') == job and p.get('name')]
