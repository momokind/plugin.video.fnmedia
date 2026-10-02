# -*- coding: utf-8 -*-
"""后台服务：监听播放结束，向 NAS 回传播放进度

插件在开始播放时写入 pending.json（含各 guid 与流地址），
本服务常驻运行，确认播放的是本插件的代理流后，在停止/播完时
调用 /v/api/v1/play/record 回传进度，实现多端断点续播。
"""
import os
import sys

# Kodi 以本文件为入口启动时，sys.path 只有 resources/lib，
# 需要把插件根目录加进来才能 import resources.*
_ADDON_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)

import xbmc

from resources.lib import util
from resources.lib.player import PENDING_FILE

MIN_PROGRESS_SECONDS = 5  # 播放不足 5 秒不上报
RESUME_SEEK_THRESHOLD = 10  # 续播点不足 10s 不自动 seek（接近开头，直接从 0 看）


def _norm_stream_url(url):
    """播放 URL 匹配用：去掉 Kodi 管道参数（| 及其后）。

    网盘直链播放地址可能带 |User-Agent=..&Cookie=.. 注入段，不同 Kodi 版本
    的 getPlayingFile() 对管道段的返回不一致（含/不含），统一按去管道后的
    纯 URL 比较，避免直链播放时进度回传/自动续播静默失效。"""
    return (url or '').split('|', 1)[0]


class FnPlayer(xbmc.Player):
    def __init__(self):
        super(FnPlayer, self).__init__()
        self.pending = None        # 当前待上报的播放信息
        self.ours_playing = False  # 是否确认是本插件的流在播放
        self.last_time = 0         # 播放期间周期缓存的进度（停止后 getTime 会抛异常）
        self._resume_seeked = False  # 本曲是否已自动续播 seek（避免重复）

    def _file_url(self):
        try:
            return self.getPlayingFile() or ''
        except Exception:
            return ''

    def onPlayBackStarted(self):
        # self.pending 可能尚未被主循环轮询加载（回调先于 5s tick 触发）→
        # 直接读盘兜底；否则直链秒开时关联不上、停止后不回传进度
        pending = self.pending or util.load_json(PENDING_FILE) or {}
        if pending and _norm_stream_url(self._file_url()) == _norm_stream_url(pending.get('stream_url')):
            self.pending = pending
            self.ours_playing = True
            util.log('检测到飞牛影视播放开始，将跟踪进度')

    def onAVStarted(self):
        """AV 管线就绪（≈首帧）时触发。

        蓝光 ISO（CDVDInputStreamBluray）+ setResolvedUrl 条目，Kodi 不应用
        ListItem 的 resumePoint（从标题 0 起播）。这里在 AV 就绪后自动 seek 到
        pending.seek_to——它来自用户在 Kodi 续播提示框/右键菜单的选择
        （resume=true→服务端位置；false→0 不 seek；未弹框默认服务端位置），
        省去"从 0 起播→用户手动拖进度条"的空跑。
        """
        if self._resume_seeked:
            return
        # pending 由主循环 5s 轮询加载，onAVStarted 触发时可能尚未就绪 → 直接读盘
        pending = self.pending or util.load_json(PENDING_FILE) or {}
        if _norm_stream_url(self._file_url()) != _norm_stream_url(pending.get('stream_url')):
            return  # 不是本插件的流
        # seek_to=0（用户选了从头开始）不 seek；旧版 pending 无此字段时回退 ts0
        seek_to = int(pending.get('seek_to', pending.get('ts0', 0)) or 0)
        if seek_to < RESUME_SEEK_THRESHOLD:
            return  # 续播点接近开头，直接从 0 看
        self._resume_seeked = True  # 先标记，防并发重复
        import threading
        threading.Thread(target=self._seek_resume, args=(seek_to,),
                         daemon=True).start()

    def _seek_resume(self, ts0):
        """onAVStarted 时播放器刚起步，稍等再 seek 更稳；失败重试一次。

        near-check：若 getTime() 已接近 ts0（Kodi 自己 resume 的非蓝光场景），
        不重复 seek。seek 最终失败则放弃（用户仍可手动 seek，不劣于现状）。
        """
        monitor = xbmc.Monitor()
        for delay in (0.8, 1.5):
            monitor.waitForAbort(delay)
            try:
                if not self.isPlaying():
                    return
                cur = self.getTime()
                if abs(cur - ts0) < 5:
                    return  # 已在续播点附近（Kodi 原生 resume），不重复 seek
                self.seekTime(ts0)
                util.log('自动续播 seek 到 %ds（起播位置 %ds）' % (ts0, cur))
                return
            except Exception as e:
                util.log('自动续播 seek 失败重试: %s' % e, xbmc.LOGWARNING)
                continue

    def onPlayBackStopped(self):
        self._resume_seeked = False
        self._report()

    def onPlayBackEnded(self):
        self._resume_seeked = False
        self._report()

    def _report(self):
        if not (self.pending and self.ours_playing):
            return
        pending, self.pending, self.ours_playing = self.pending, None, False

        # 停止/结束后 Kodi 已不在播放，getTime() 会抛异常（且 Kodi 会先写一条
        # error 日志即使被 except 接住）——只在仍在播放时才调 getTime，
        # 否则用主循环周期缓存的 last_time，最后才回退起播位置 ts0。
        position = 0
        try:
            if self.isPlaying():
                position = self.getTime()
        except Exception:
            position = 0
        if position <= 0:
            position = self.last_time or pending.get('ts0', 0)

        if position < MIN_PROGRESS_SECONDS:
            util.delete_file(PENDING_FILE)
            util.debug('播放时间过短（%ds），不上报' % position)
            return

        duration = pending.get('duration') or 0
        if duration and position > duration:
            position = duration

        from resources.lib.fnapi.client import FnClient
        client = FnClient(pending.get('base', ''), verify=pending.get('verify', False))
        client.set_credentials(token=pending.get('token', ''))
        try:
            client.play_record(
                item_guid=pending.get('item_guid', ''),
                media_guid=pending.get('media_guid', ''),
                video_guid=pending.get('video_guid', ''),
                audio_guid=pending.get('audio_guid', ''),
                subtitle_guid=pending.get('subtitle_guid', ''),
                play_link=pending.get('play_link', ''),
                ts=position,
                duration=duration,
            )
            util.log('进度已回传: %ds / %ds' % (position, duration))
            # 播放结束可能改变服务端 watched 状态，清列表 TTL 缓存避免滞后
            client.invalidate_item_list_cache()
            # 接近播完（≥90%）视为已看，原地修正整库描述符（重建整库太贵）
            if duration and position >= duration * 0.9:
                from resources.lib import desc
                desc.patch_watched(pending.get('item_guid', ''))
        except Exception as e:
            util.log('进度回传失败: %s' % e, xbmc.LOGWARNING)
        finally:
            util.delete_file(PENDING_FILE)


def _enrich_loop(monitor):
    """常驻预热：周期消费 enrich_queue.json，补全列表浏览时排队的详情缓存。

    放在 service 进程（而非插件 invoker）内执行——invoker 内的后台线程会让
    invoker 多活数秒，而 Kodi 播放器 OpenFile 卡在 invoker 退出上，导致
    点播首屏多卡 1-3s。挪到常驻 service 后不卡任何 invoker。
    """
    from resources.lib import meta
    cooldown = 0
    while not monitor.waitForAbort(3):
        try:
            if cooldown > 0:
                cooldown -= 1
                continue
            client = util.ensure_client()
            if client.on_token_refresh is None:
                client.on_token_refresh = lambda token: util.set_setting('token', token)
            if not client.token:
                continue   # 未配置，跳过
            done = meta.drain_enrich_queue(client)
            if done == 0:
                cooldown = 2   # 队列空，降频（~9s 轮询一次）
        except Exception as e:
            util.log('预热循环异常: %s' % e, xbmc.LOGDEBUG)
            cooldown = 4


def _prewarm_loop(monitor, player):
    """常驻整库预热：把各媒体库与"全部电影/全部剧集"范围的描述符缓存建好。

    海报墙"整库直出"（browser.filter_list）依赖 desc 范围缓存：进列表命中
    即零网络零解析，只剩 ListItem 构造。开机后台逐库构建（一次 walk 实测
    ~2s），用户进插件时通常已命中——一面墙即整库、无翻页项、零等待。
    播放中让路；desc.prewarm_one 每次只建一个范围，天然错峰不打突刺。
    """
    from resources.lib import desc, meta, libsync
    while not monitor.waitForAbort(20):
        try:
            if player.isPlaying():
                continue
            client = util.ensure_client()
            if client.on_token_refresh is None:
                client.on_token_refresh = lambda token: util.set_setting('token', token)
            if not client.token:
                continue   # 未配置/未登录，跳过（首次浏览登录后 token 会落盘）
            done = desc.prewarm_one(client)
            if done:
                util.log('整库预热完成: %s' % done)
                continue
            # 描述符就绪后补扫版本数（多版本"〔N版本〕"标记的数据源）
            progress = desc.scan_versions_chunk(client)
            if progress:
                util.log('版本扫描: %s' % progress)
                continue
            # 版本扫完后再刷新分类索引（流派 → 条目，本地聚合零 API）
            built = meta.build_genre_index(client)
            if built:
                util.log('分类索引已刷新: %d 个流派' % built)
                continue
            # 原生媒体库同步（strm+nfo → Kodi 原生库，12h TTL）
            status = libsync.sync_library(client)
            if status not in ('fresh', 'disabled'):
                util.log('媒体库同步: %s' % status)
            else:
                monitor.waitForAbort(240)   # 全部新鲜，降频（~4 分钟查一次）
        except Exception as e:
            util.log('整库预热异常: %s' % e, xbmc.LOGDEBUG)
            monitor.waitForAbort(60)


def run():
    monitor = xbmc.Monitor()
    player = FnPlayer()

    # 常驻详情预热（消费列表浏览排队）——必须在独立线程，不阻塞主循环
    import threading
    threading.Thread(target=_enrich_loop, args=(monitor,), daemon=True).start()
    # 常驻整库预热（构建海报墙描述符缓存）——同样独立线程错峰执行
    threading.Thread(target=_prewarm_loop, args=(monitor, player), daemon=True).start()

    # 启动时若发现遗留的 pending（上次 Kodi 被强制退出），尝试以上次记录的位置上报
    stale = util.load_json(PENDING_FILE)
    if stale:
        util.log('发现未上报的播放记录，尝试补报（ts=%s）' % stale.get('ts0', 0))
        player.pending = stale
        player.ours_playing = False  # 未确认播放，直接以上次位置补报
        position = stale.get('ts0', 0)
        if position >= MIN_PROGRESS_SECONDS:
            from resources.lib.fnapi.client import FnClient
            client = FnClient(stale.get('base', ''), verify=stale.get('verify', False))
            client.set_credentials(token=stale.get('token', ''))
            try:
                client.play_record(
                    item_guid=stale.get('item_guid', ''),
                    media_guid=stale.get('media_guid', ''),
                    video_guid=stale.get('video_guid', ''),
                    audio_guid=stale.get('audio_guid', ''),
                    subtitle_guid=stale.get('subtitle_guid', ''),
                    play_link=stale.get('play_link', ''),
                    ts=position,
                    duration=stale.get('duration', 0),
                )
                util.log('补报完成')
                client.invalidate_item_list_cache()
            except Exception as e:
                util.log('补报失败: %s' % e, xbmc.LOGWARNING)
        util.delete_file(PENDING_FILE)
        player.pending = None

    # 主循环：播放开始时读取 pending.json；播放中周期缓存进度
    while not monitor.waitForAbort(5):
        if player.isPlaying():
            # 周期缓存进度：停止后 getTime() 不可用，_report 用此缓存上报真实位置
            try:
                player.last_time = player.getTime()
            except Exception:
                pass
            # 关联 pending：回调错失时的兜底。0.3.x 起此块被错误缩进在
            # except 内（仅 getTime() 抛异常的起播窗口才执行）——代理时代
            # ISO 打开慢窗口尚存，直链秒开后窗口消失导致回传失效
            if player.pending is None:
                pending = util.load_json(PENDING_FILE)
                if pending:
                    player.pending = pending
                    if not player.last_time:
                        player.last_time = pending.get('ts0', 0) or 0
                    if _norm_stream_url(player._file_url()) == _norm_stream_url(pending.get('stream_url')):
                        player.ours_playing = True
                        util.debug('已关联播放: %s' % pending.get('stream_url'))


if __name__ == '__main__':
    run()
