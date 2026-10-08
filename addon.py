# -*- coding: utf-8 -*-
"""飞牛影视 Kodi 插件入口

路由说明：
  plugin://plugin.video.fnmedia/                       主菜单
  ?action=browse&guid=xxx&title=yyy                    浏览目录/媒体库
  ?action=play&guid=xxx&source=N                       播放（source 为版本索引，用于蓝光多版本）
  ?action=source&guid=xxx                              弹窗选择版本后播放
  ?action=watched&guid=xxx                             标记为已观看
  ?action=manual                                       手动输入 GUID 浏览
  ?action=relogin                                      重新登录
  ?action=sync                                         请求立即媒体库同步
"""
import sys
from urllib.parse import parse_qs

import xbmcplugin

from resources.lib import util
from resources.lib import browser
from resources.lib import player


def get_params():
    """解析插件 URL 的查询参数"""
    params = {}
    if len(sys.argv) > 2 and sys.argv[2]:
        query = sys.argv[2][1:] if sys.argv[2].startswith('?') else sys.argv[2]
        for key, values in parse_qs(query).items():
            params[key] = values[0]
    return params


def router():
    handle = int(sys.argv[1])
    params = get_params()
    action = params.get('action', '')

    util.log('router action=%s params=%s' % (action, params))

    if action == 'play':
        player.play(handle, params)
    elif action == 'source':
        player.select_source(params)
    elif action == 'watched':
        player.mark_watched(params)
    elif action == 'relogin':
        player.relogin()
    elif action == 'sync':
        browser.sync_action(handle, params)
    elif action == 'filter':
        browser.filter_list(handle, params)
    elif action == 'tv':
        browser.tv_detail(handle, params)
    elif action == 'season':
        browser.season_detail(handle, params)
    elif action == 'browse':
        browser.browse(handle, params)
    elif action == 'genres':
        browser.genres_list(handle, params)
    elif action == 'recent':
        browser.recent(handle, params)
    elif action == 'search':
        browser.search(handle, params)
    elif action == 'genre':
        browser.genre_items(handle, params)
    elif action == 'manual':
        browser.manual_guid(handle)
    else:
        browser.root(handle)


if __name__ == '__main__':
    router()
