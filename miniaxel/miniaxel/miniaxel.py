#!/usr/bin/env python
# coding=utf-8


import os
import sys
import urllib2
import Queue
from io import BytesIO
from time import time as _time, sleep as _sleep
from threading import Event as _Event
from threading import Lock as _Lock
from socket import timeout as _socket_timeout
from vavava.httputil import HttpUtil, TIMEOUT, DEBUG_LVL, HttpFetcher
from vavava import util
from vavava import threadutil

util.set_default_utf8()
CHARSET = "utf-8"
pjoin = os.path.join
pdirname = os.path.dirname
pabspath = os.path.abspath

class DownloadUrl:

    def __init__(self, url, out, n=1, proxy=None, bar=False, retrans=False,
                 headers=None, archive_callback=None, timeout=TIMEOUT, log=None):
        self.url = url
        self.out = out
        self.n = n
        self.proxy = proxy
        self.progress_bar = None
        if bar:
            self.progress_bar = ProgressBar()
        self.retransmission = retrans
        self.__history_file = None
        self.headers = headers
        self.archive_callback = archive_callback
        self.timeout = timeout
        self.log = log

        self.__subworks = []
        self.__err_ev = _Event()
        self.__initialised_ev = _Event()
        self.__file_mutex = _Lock()
        self.__mutex = _Lock()
        self.__err_ev.clear()
        self.__initialised_ev.clear()

    def __get_data_size(self, url):
        info = HttpUtil().head(url)
        try:
            if info.getheader('Accept-Ranges') == 'bytes':
                return int(info.getheader('Content-Length'))
        except:
            if info is not None:
                self.log.error(str(info.msg.headers))
            return None

    def getSubWorks(self):
        if self.__initialised_ev.is_set():
            raise ValueError('can not reinitialise')
        self.__err_ev.clear()
        cur_size = 0
        size = self.__get_data_size(self.url)
        clip_ranges = self.__div_file(size, self.n)

        if isinstance(self.out, file) or isinstance(self.out, BytesIO):
            self.__fp = self.out
            self.retransmission = False
        elif os.path.exists(self.out):
            self.__fp = open(self.out, 'rb+')
        else:
            self.__fp = open(self.out, 'wb')

        if size and self.retransmission:
            self.__history_file = HistoryFile()
            clip_ranges, cur_size = self.__history_file.mk_clips(self.out, clip_ranges, size)

        # can not retransmission
        if clip_ranges is None or size is None or size == 0:
            clip_ranges = [None]
            size = 0

        if self.progress_bar:
            self.progress_bar.reset(total_size=size, cur_size=cur_size)

        self.__subworks = []
        for clip_range in clip_ranges:
            work = DownloadUrl.HttpDLSubWork(
                url=self.url, fp=self.__fp, range=clip_range,
                file_mutex=self.__file_mutex, parent=self, callback=self.update
            )
            self.__subworks.append(work)

        self.__initialised_ev.set()   # !!!
        return self.__subworks

    def __div_file(self, size, n):
        minsize = 1024
        # if n == 1 or size <= minsize:
        if size <= minsize:
            return None
        clip_size = size/n
        clips = [(i*clip_size, i*clip_size+clip_size-1) for i in xrange(0, n-1)]
        clips.append(((n-1)*clip_size, size-1))
        return clips

    def isInitialised(self):
        return self.__initialised_ev.isSet()

    def isArchived(self):
        if not self.isInitialised():
            return False
        if not self.isErrorHappen():
            for work in self.__subworks:
                if work.isWorking():
                    return False
        return True

    def setError(self):
        self.__err_ev.set()

    def isErrorHappen(self):
        return self.__err_ev.isSet()

    def setStop(self):
        for work in self.__subworks:
            work.setStop()

    def wait(self):
        # ????????
        for work in self.__subworks:
            work.wait()

    def update(self, offset, size):
        if self.progress_bar:
            self.progress_bar.update(size)
        if self.__history_file:
            self.__history_file.update(offset, size=size)

    def display(self, force=False):
        if self.progress_bar:
            self.progress_bar.display(force)

    def cleanUp(self):
        if self.progress_bar:
            self.progress_bar.display(force=True)
        if self.retransmission:
            self.__history_file.clean()
        else:
            # del unfinished file
            if not self.isArchived():
                is_external_file = isinstance(self.out, BytesIO) or isinstance(self.out, file)
                if not is_external_file:
                    if os.path.exists(self.out):
                        if self.__fp and self.__fp.closed:
                            self.__fp.close()
                        os.remove(self.out)
        if self.archive_callback:
            self.archive_callback(self)

    class HttpDLSubWork(threadutil.WorkBase):

        def __init__(self, url, fp, range=None, proxy=None,
                     file_mutex=None, parent=None, callback=None):
            threadutil.WorkBase.__init__(self)
            self.url = url
            self.fp = fp
            self.range = range
            self.proxy = proxy
            self.file_mutex = file_mutex
            self.parent = parent
            self.downloader = None
            self.__stop_ev = _Event()
            self.__working = True
            self.__callback = callback
            self.__http_fetcher = HttpFetcher()

        def isWorking(self):
            return self.__working # and self.__http_fetcher.isFetching()

        def __is_setstop(self):
            return self.__stop_ev.isSet()

        def setStop(self):
            self.__http_fetcher.setStop()       # stop fetch
            self.__stop_ev.set()                # stop re-fetch

        def wait(self, timeout=None):
            import time
            while self.__working:
                time.sleep(0.5)
                if timeout < 0:
                    raise RuntimeError('HttpDLSubWork timeout')
                timeout -= 0.5

        def work(self, this_thread, log):
            # log.debug('[wk] start working, %s', self.url)
            self.__stop_ev.clear()
            self.__working = True
            try:
                self.__work(this_thread, log)
            except:
                raise
            finally:
                self.__working = False

        def __work(self, this_thread, log):

            while not this_thread.isSetToStop() and not self.__is_setstop():
                try:
                    self.__http_fetcher.fetch(self.url, fp=self.fp, range=self.range,
                            file_mutex=self.file_mutex, callback=self.__callback)
                    return
                except _socket_timeout:
                    log.debug('[HttpDLSubWork] timeout  %s', self.url)
                except urllib2.URLError as e:
                    # log.debug('[HttpDLSubWork] Network not work :(')
                    log.exception(e)
                except Exception as e:
                    if self.parent:
                        self.parent.setError()
                    self.is_err_happen = True
                    raise
                _sleep(1)


class MiniAxelWorkShop(threadutil.ThreadBase):

    def __init__(self, tmin=10, tmax=20, bar=True, retrans=False,
                 debug_lvl=DEBUG_LVL, log=None):
        threadutil.ThreadBase.__init__(self, log=log)
        self.bar = bar
        self.retrans = retrans
        self.__url_work_buffer = Queue.Queue()
        self.__url_works = []
        self.__ws = threadutil.WorkShop(tmin=tmin, tmax=tmax, log=log)

    def addUrl(self, url, out, headers=None, n=5, archive_callback=None):
        urlwk = DownloadUrl(url, out=out, headers=headers,
                    n=n, retrans=self.retrans, bar=self.bar,
                    archive_callback=archive_callback, log=self.log)
        self.__url_work_buffer.put(urlwk)

    def addUrlWorks(self, works):
        for wk in works:
            assert isinstance(wk, DownloadUrl)
            self.__url_work_buffer.put(wk)

    def run(self):
        self.log.debug('[axel] start serving')

        self.__ws.serve()
        while not self.isSetToStop():
            start_at = _time()
            try:
                self.__loop()
            except Exception as e:
                self.log.exception(e)
            finally:
                duration = _time() - start_at
                if duration < 0.5:
                    _sleep(0.5)

        for urlwk in self.__url_works:
            urlwk.setStop()
            urlwk.wait()
            urlwk.cleanUp()
        self.__ws.setShopClose()
        self.__ws.waitShopClose()
        self.log.debug('[axel] stop serving')

    def __loop(self):
        if not self.__url_work_buffer.empty():
            urlwk = self.__url_work_buffer.get()
            self.__ws.addWorks(urlwk.getSubWorks())
            self.__url_works.append(urlwk)
            # self.log.debug('[ws] get a work: %s', urlwk.url)
        for i, urlwk in enumerate(self.__url_works):
            urlwk.display()
            if urlwk.isErrorHappen():
                self.log.debug('[axel] work err: %s', urlwk.url)
                urlwk.setStop()
                urlwk.wait()
            if urlwk.isArchived() or urlwk.isErrorHappen():
                urlwk.cleanUp()
                self.log.debug('[axel] work done: %s', urlwk.url)
                del self.__url_works[i]
                break


class ProgressBar:

    def __init__(self, size=None):
        self.reset(size, 0)
        self.mutex = _Lock()

    def reset(self, total_size, cur_size):
        self.size = total_size
        if self.size == 0:
            self.size = 1
        self.cur_size = cur_size
        self.last_size = 0
        self.last_updat = self.start = _time()

    def update(self, data_size):
        with self.mutex:
            self.cur_size += data_size

    def display(self, force=False):
        assert self.size is not None
        now = _time()
        duration = now - self.last_updat
        if not force and duration < 1:
            # print '*******%d-%d=%d'%(now, self.last_updat, duration)
            return
        percentage = 10.0*self.cur_size/self.size
        if duration == 0:
            speed = 0
        else:
            speed = (self.cur_size - self.last_size)/duration
        output_format = '\r[%3.1d%% %5.1dk/s][ %5.1ds/%5.1ds] [%dk/%dk]            '
        if speed > 0:
            output = output_format % (percentage*10, speed/1024, now - self.start,
                (self.size-self.cur_size)/speed, self.cur_size/1024, self.size/1024)
        else:
            if self.cur_size == 0:
                expect = 0
            else:
                expect = (self.size-self.cur_size)*(now-self.start)/self.cur_size
            output = output_format % (percentage*10, 0, now - self.start, expect,
                                       self.cur_size/1024, self.size/1024)
        sys.stdout.write(output)
        if percentage == 100:
            sys.stdout.write('\n')
        sys.stdout.flush()
        self.last_updat = now
        self.last_size = self.cur_size
        if force and percentage == 10:
            print ''


class HistoryFile:

    def __init__(self):
        self.mutex = _Lock()
        self.txt = None

    def mk_clips(self, target_file, clips, size):
        """ return clips, current_size, is_retransmission
        """
        self.target_file = os.path.abspath(target_file)
        self.txt = self.target_file + '.txt'
        self.buffered = 0
        cur_size = size
        if os.path.exists(self.txt) and os.path.exists(self.target_file):
            self.clips = []
            with open(self.txt, 'r') as fp:
                for num in fp.read().split('|'):
                    if num.strip() != '':
                        (a, b) = num.split(',')
                        a, b = int(a), int(b)
                        if a <= b:
                            cur_size -= b - a + 1
                            self.clips.append((a, b))
            return self.clips, cur_size
        else:
            if clips is None:
                self.clips = [(0, size - 1)]
            else:
                self.clips = clips
            with open(self.txt, 'w') as fp:
                for clip in self.clips:
                    fp.write('%d,%d|' % clip)
            return clips, 0


    def update(self, offset, size):
        assert size > 0
        assert offset >=0
        with self.mutex:
            self.buffered += size
            for i in xrange(len(self.clips)):
                a, b = self.clips[i]
                if a <= offset <= b:
                    if size <= b - a + 1:
                        self.clips[i] = (a + size, b)
                    else:
                        assert size <= b - a + 1
                    break

    def clean(self):
        with self.mutex:
            if self.txt:
                if os.path.exists(self.txt):
                    os.remove(self.txt)

    def update_file(self, force=False):
        str = ''
        with self.mutex:
            if not force and self.buffered < 1000*512:
                return
            self.buffered = 0
            for (a, b) in self.clips:
                if a < b + 1:
                    str += '%d,%d|' % (a, b)
                else:
                    assert a <= (b + 1)
        with open(self.txt, 'w') as fp:
            fp.write(str)
