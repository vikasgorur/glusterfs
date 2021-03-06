import re
import os
import sys
import stat
import time
import errno
import struct
import select
import socket
import logging
import tempfile
import threading
import time
from ctypes import *
from ctypes.util import find_library
from errno import EEXIST, ENOENT, ENODATA, ENOTDIR, ELOOP, EISDIR

from gconf import gconf
import repce
from repce import RepceServer, RepceClient
from master import GMaster
import syncdutils

UrlRX  = re.compile('\A(\w+)://(.*)')
HostRX = re.compile('[a-z\d](?:[a-z\d.-]*[a-z\d])?', re.I)
UserRX = re.compile("[\w!\#$%&'*+-\/=?^_`{|}~]+")

def sup(x, *a, **kw):
    return getattr(super(type(x), x), sys._getframe(1).f_code.co_name)(*a, **kw)

def desugar(ustr):
    m = re.match('([^:]*):(.*)', ustr)
    if m:
        if not m.groups()[0]:
            return "gluster://localhost" + ustr
        elif '@' in m.groups()[0] or re.search('[:/]', m.groups()[1]):
            return "ssh://" + ustr
        else:
            return "gluster://#{str}"
    else:
        return "file://" + os.path.abspath(ustr)

def parse_url(ustr):
    m = UrlRX.match(ustr)
    if not m:
        ustr = desugar(ustr)
    m = UrlRX.match(ustr)
    if not m:
        raise RuntimeError("malformed url")
    sch, path = m.groups()
    this = sys.modules[__name__]
    if not hasattr(this, sch.upper()):
        raise RuntimeError("unknown url scheme " + sch)
    return getattr(this, sch.upper())(path)


class Xattr(object):

    libc = CDLL(find_library("libc"))

    @classmethod
    def geterrno(cls):
        return c_int.in_dll(cls.libc, 'errno').value

    @classmethod
    def raise_oserr(cls):
        errn = cls.geterrno()
        raise OSError(errn, os.strerror(errn))

    @classmethod
    def _query_xattr(cls, path, siz, syscall, *a):
        if siz:
            buf = create_string_buffer('\0' * siz)
        else:
            buf = None
        ret = getattr(cls.libc, syscall)(*((path,) + a + (buf, siz)))
        if ret == -1:
            cls.raise_oserr()
        if siz:
            return buf.raw[:ret]
        else:
            return ret

    @classmethod
    def lgetxattr(cls, path, attr, siz=0):
        return cls._query_xattr( path, siz, 'lgetxattr', attr)

    @classmethod
    def llistxattr(cls, path, siz=0):
        ret = cls._query_xattr(path, siz, 'llistxattr')
        if isinstance(ret, str):
            ret = ret.split('\0')
        return ret

    @classmethod
    def lsetxattr(cls, path, attr, val):
        ret = cls.libc.lsetxattr(path, attr, val, len(val), 0)
        if ret == -1:
            cls.raise_oserr()

    @classmethod
    def llistxattr_buf(cls, path):
        size = cls.llistxattr(path)
        if size  == -1:
            raise_oserr()
        return cls.llistxattr(path, size)



class Server(object):

    GX_NSPACE = "trusted.glusterfs"
    NTV_FMTSTR = "!" + "B"*19 + "II"
    FRGN_XTRA_FMT = "I"
    FRGN_FMTSTR = NTV_FMTSTR + FRGN_XTRA_FMT

    @staticmethod
    def entries(path):
        # prevent symlinks being followed
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError(ENOTDIR, os.strerror(ENOTDIR))
        return os.listdir(path)

    @classmethod
    def purge(cls, path, entries=None):
        me_also = entries == None
        if not entries:
            try:
                # if it's a symlink, prevent
                # following it
                try:
                    os.unlink(path)
                    return
                except OSError:
                    ex = sys.exc_info()[1]
                    if ex.errno == EISDIR:
                        entries = os.listdir(path)
                    else:
                        raise
            except OSError:
                ex = sys.exc_info()[1]
                if ex.errno in (ENOTDIR, ENOENT, ELOOP):
                    try:
                        os.unlink(path)
                        return
                    except OSError:
                        ex = sys.exc_info()[1]
                        if ex.errno == ENOENT:
                            return
                        raise
                else:
                    raise
        for e in entries:
            cls.purge(os.path.join(path, e))
        if me_also:
            os.rmdir(path)

    @classmethod
    def _create(cls, path, ctor):
        try:
            ctor(path)
        except OSError:
            ex = sys.exc_info()[1]
            if ex.errno == EEXIST:
                cls.purge(path)
                return ctor(path)
            raise

    @classmethod
    def mkdir(cls, path):
        cls._create(path, os.mkdir)

    @classmethod
    def symlink(cls, lnk, path):
        cls._create(path, lambda p: os.symlink(lnk, p))

    @classmethod
    def xtime(cls, path, uuid):
        try:
            return struct.unpack('!II', Xattr.lgetxattr(path, '.'.join([cls.GX_NSPACE, uuid, 'xtime']), 8))
        except OSError:
            ex = sys.exc_info()[1]
            if ex.errno in (ENOENT, ENODATA, ENOTDIR):
                return ex.errno
            else:
                raise

    @classmethod
    def set_xtime(cls, path, uuid, mark):
        Xattr.lsetxattr(path, '.'.join([cls.GX_NSPACE, uuid, 'xtime']), struct.pack('!II', *mark))

    @staticmethod
    def setattr(path, adct):
        own = adct.get('own')
        if own:
            os.lchown(path, *own)
        mode = adct.get('mode')
        if mode:
            os.chmod(path, stat.S_IMODE(mode))
        times = adct.get('times')
        if times:
            os.utime(path, times)

    @staticmethod
    def pid():
        return os.getpid()

    lastping = 0
    @classmethod
    def ping(cls, dct):
        if dct:
            key = '.'.join([cls.GX_NSPACE, 'volume-mark', dct['uuid']])
            val = struct.pack(cls.FRGN_FMTSTR,
                              *(dct['version']  +
                                tuple(int(x,16) for x in re.findall('(?:[\da-f]){2}', dct['uuid'])) +
                                (dct['retval'],) + dct['volume_mark'][0:2] + (dct['timeout'],)))
            Xattr.lsetxattr('.', key, val)
        else:
            logging.info('no volume-mark, if the behaviour persists have to check if master gsyncd is running')
        cls.lastping += 1
        return cls.lastping

    @staticmethod
    def version():
        return 1.0


class SlaveLocal(object):

    def can_connect_to(self, remote):
        return not remote

    def service_loop(self):
        repce = RepceServer(self.server, sys.stdin, sys.stdout, int(gconf.sync_jobs))
        t = threading.Thread(target=repce.service_loop)
        t.setDaemon(True)
        t.start()
        logging.info("slave listening")
        if gconf.timeout and int(gconf.timeout) > 0:
            while True:
                lp = self.server.lastping
                time.sleep(int(gconf.timeout))
                if lp == self.server.lastping:
                    logging.info("connection inactive for %d seconds, stopping" % int(gconf.timeout))
                    break
        else:
            select.select((), (), ())

class SlaveRemote(object):

    def connect_remote(self, rargs=[], **opts):
        slave = opts.get('slave', self.url)
        ix, ox = os.pipe()
        iy, oy = os.pipe()
        pid = os.fork()
        if not pid:
            os.close(ox)
            os.dup2(ix, sys.stdin.fileno())
            os.close(iy)
            os.dup2(oy, sys.stdout.fileno())
            argv = rargs + gconf.remote_gsyncd.split() + ['-N', '--listen', '--timeout', str(gconf.timeout), slave]
            os.execvp(argv[0], argv)
        os.close(ix)
        os.close(oy)
        return self.start_fd_client(iy, ox, **opts)

    def start_fd_client(self, i, o, **opts):
        self.server = RepceClient(i, o)
        rv = self.server.__version__()
        exrv = {'proto': repce.repce_version, 'object': Server.version()}
        da0 = (rv, exrv)
        da1 = ({}, {})
        for i in range(2):
            for k, v in da0[i].iteritems():
                da1[i][k] = int(v)
        if da1[0] != da1[1]:
            raise RuntimeError("RePCe major version mismatch: local %s, remote %s" % (exrv, rv))

    def rsync(self, files, *args):
        if not files:
            raise RuntimeError("no files to sync")
        logging.debug("files: " + ", ".join(files))
        argv = gconf.rsync_command.split() + gconf.rsync_extra.split() + ['-aR'] + files + list(args)
        return os.spawnvp(os.P_WAIT, argv[0], argv) == 0


class AbstractUrl(object):

    def __init__(self, path, pattern):
        m = re.search(pattern, path)
        if not m:
            raise RuntimeError("malformed path")
        self.path = path
        return m.groups()

    def scheme(self):
        return type(self).__name__.lower()

    def canonical_path(self):
        return self.path

    def get_url(self, canonical=False, escaped=False):
        if canonical:
            pa = self.canonical_path()
        else:
            pa = self.path
        u = "://".join((self.scheme(), pa))
        if escaped:
            u = syncdutils.escape(u)
        return u

    @property
    def url(self):
        return self.get_url()


  ### Concrete resource classes ###


class FILE(AbstractUrl, SlaveLocal, SlaveRemote):

    class FILEServer(Server):
        pass

    server = FILEServer

    def __init__(self, path):
        sup(self, path, '^/')

    def connect(self):
        os.chdir(self.path)

    def rsync(self, files):
        return sup(self, files, self.path)


class GLUSTER(AbstractUrl, SlaveLocal, SlaveRemote):

    class GLUSTERServer(Server):

        forgn_mark_size = struct.calcsize(Server.FRGN_FMTSTR)
        nativ_mark_size = struct.calcsize(Server.NTV_FMTSTR)

        @classmethod
        def attr_unpack_dict(cls, xattr, extra_fields = ''):
            fmt_string = cls.NTV_FMTSTR + extra_fields
            buf = Xattr.lgetxattr('.', xattr, struct.calcsize(fmt_string))
            vm = struct.unpack(fmt_string, buf)
            logging.info("str: %s" % `vm`)
            m = re.match('(.{8})(.{4})(.{4})(.{4})(.{12})', "".join(['%02x' % x for x in vm[2:18]]))
            uuid = '-'.join(m.groups())
            volinfo = {  'version': vm[0:2],
                         'uuid'   : uuid,
                         'retval' : vm[18],
                         'volume_mark': vm[18:20],
                      }
            logging.info("volinfo: %s" % `volinfo`)
            if extra_fields:
                return volinfo, vm[-len(extra_fields):]
            else:
                return volinfo

        @classmethod
        def foreign_marks(cls):
            dict_list = []
            xattr_list = Xattr.llistxattr_buf('.')
            for ele in xattr_list:
                if (ele.find('trusted.glusterfs.volume-mark') != -1):
                    #buf = Xattr.lgetxattr('.', ele, cls.forgn_mark_size)
                    d, x = cls.attr_unpack_dict(ele, cls.FRGN_XTRA_FMT)
                    d['timeout'] = x[0]
                    dict_list.append(d)
            return dict_list

        @classmethod
        def native_mark(cls):
            try:
                return cls.attr_unpack_dict('.'.join([cls.GX_NSPACE, 'volume-mark']))
            except OSError:
                ex = sys.exc_info()[1]
                if ex.errno == ENODATA:
                    logging.warn("volume-mark not found")
                    return
                else:
                    raise RuntimeError("master is corrupt")

    server = GLUSTERServer

    def __init__(self, path):
        self.host, self.volume = sup(self, path, '^(%s):(.+)' % HostRX.pattern)

    def canonical_path(self):
        return ':'.join([socket.gethostbyname(self.host), self.volume])

    def can_connect_to(self, remote):
        return True

    def connect(self):
        d = tempfile.mkdtemp()
        try:
            argv = [gconf.gluster_command] + \
                    (gconf.gluster_log_level and ['-L', gconf.gluster_log_level] or []) + \
                    ['-l', gconf.gluster_log_file, '-s', self.host,
                     '--volfile-id', self.volume, '--client-pid=-1', d]
            if os.spawnvp(os.P_WAIT, argv[0], argv):
                raise RuntimeError("command failed: " + " ".join(argv))
            logging.debug('auxiliary glusterfs mount in place')
            os.chdir(d)
            argv = ['umount', '-l', d]
            if os.spawnvp(os.P_WAIT, argv[0], argv):
                raise RuntimeError("command failed: " + " ".join(argv))
        finally:
            try:
                os.rmdir(d)
            except:
                logging.warn('stale mount possibly left behind on ' + d)
        logging.debug('auxiliary glusterfs mount prepared')

    def connect_remote(self, *a, **kw):
        sup(self, *a, **kw)
        self.slavedir = "/proc/%d/cwd" % self.server.pid()

    def service_loop(self, *args):
        if args:
            GMaster(self, args[0]).crawl()
        else:
            sup(self, *args)

    def rsync(self, files):
        return sup(self, files, self.slavedir)


class SSH(AbstractUrl, SlaveRemote):

    def __init__(self, path):
        self.remote_addr, inner_url = sup(self, path,
                                          '^((?:%s@)?%s):(.+)' % tuple([ r.pattern for r in (UserRX, HostRX) ]))
        self.inner_rsc = parse_url(inner_url)

    def canonical_path(self):
        m = re.match('([^@]+)@(.+)', self.remote_addr)
        if m:
            u, h = m.groups()
        else:
            u, h = os.getlogin(), self.remote_addr
        remote_addr = '@'.join([u, socket.gethostbyname(h)])
        return ':'.join([remote_addr, self.inner_rsc.get_url(canonical=True)])

    def can_connect_to(self, remote):
        return False

    def start_fd_client(self, *a, **opts):
        if opts.get('deferred'):
            return a
        sup(self, *a)
        ityp = type(self.inner_rsc)
        if ityp == FILE:
            slavepath = self.inner_rsc.path
        elif ityp == GLUSTER:
            slavepath = "/proc/%d/cwd" % self.server.pid()
        else:
            raise NotImplementedError
        self.slaveurl = ':'.join([self.remote_addr, slavepath])

    def connect_remote(self, go_daemon=None):
        if go_daemon == 'done':
            return self.start_fd_client(*self.fd_pair)
        gconf.setup_ssh_ctl(tempfile.mkdtemp())
        deferred = go_daemon == 'postconn'
        ret = sup(self, gconf.ssh_command.split() + gconf.ssh_ctl_args + [self.remote_addr], slave=self.inner_rsc.url, deferred=deferred)
        if deferred:
            # send a message to peer so that we can wait for
            # the answer from which we know connection is
            # established and we can proceed with daemonization
            # (doing that too early robs the ssh passwd prompt...)
            # However, we'd better not start the RepceClient
            # before daemonization (that's not preserved properly
            # in daemon), we just do a an ad-hoc linear put/get.
            i, o = ret
            inf = os.fdopen(i)
            repce.send(o, None, '__repce_version__')
            select.select((inf,), (), ())
            repce.recv(inf)
            # hack hack hack: store a global reference to the file
            # to save it from getting GC'd which implies closing it
            gconf.permanent_handles.append(inf)
            self.fd_pair = (i, o)
            return 'should'

    def rsync(self, files):
        return sup(self, files, '-ze', " ".join(gconf.ssh_command.split() + gconf.ssh_ctl_args), self.slaveurl)
