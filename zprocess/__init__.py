#####################################################################
#                                                                   #
# __init__.py                                                       #
#                                                                   #
# Copyright 2013, Chris Billington                                  #
#                                                                   #
# This file is part of the zprocess project (see                    #
# https://bitbucket.org/cbillington/zprocess) and is licensed under #
# the Simplified BSD License. See the license.txt file in the root  #
# of the project for the full license.                              #
#                                                                   #
#####################################################################

from __future__ import division, unicode_literals, print_function, absolute_import
import os
import sys
import subprocess
import threading
import time
import signal
from socket import gethostbyname
from functools import partial
import traceback

PY2 = sys.version_info[0] == 2
if PY2:
    import cPickle as pickle
    str = unicode
else:
    import pickle
import zmq
import weakref

try:
    from .__version__ import __version__
except ImportError:
    # Version file has not been autogenerated from build process:
    __version__ = None

# For communication between Python 2 and Python 3. Can be set by importing
# code to use a higher protocol in the case that it is known that both peers
# are a high enough version.
PICKLE_PROTOCOL = 2

if 'zprocess' in  os.getenv('COVERAGE_PROCESS_START', ''):
    # We're running with coverage.py, likely running the test suite. Add
    # sigterm handler so that atexit handlers run even when terminated and
    # coverage data is saved:
    def sigterm_handler(_signo, _stack_frame):
        sys.stderr.write('Terminated\n')
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, sigterm_handler)


context = zmq.Context.instance()
# Only the top-level process has a Broker for event passing, which will
# be instantiated when it first makes a subprocess. For the moment we
# assume we are the top-level process, and will set this to False if we
# discover that wer're not (by setup_connection_with_parent() being called):
we_are_the_top_process = True


def raise_exception_in_thread(exc_info):
    """Raises an exception in a thread"""
    def f(exc_info):
        type, value, traceback = exc_info
        # handle python2/3 difference in raising exception        
        if PY2:
            exec('raise type, value, traceback', globals(), locals())
        else:
            raise value.with_traceback(traceback)
    threading.Thread(target=f, args=(exc_info,)).start()


class TimeoutError(zmq.ZMQError):
    pass


def _typecheck_or_convert_data(data, send_type):
    """Utility function to check that messages are the valid type to be sent,
    for the send_type (one of 'pyobj', 'multipart', 'string', or 'raw'). Returns
    converted data or raises TypeError. Only conversion done is to wrap single
    bytes objects into a single-element list for multipart messages. We *do
    not* do auto encoding of strings here. Strings can't be sent by raw and
    multipart sends, so yes, they need to be encoded, but we can't to auto
    *decoding* on the other end, because the data may not represent text - it
    might just be bytes. So we prefer symmetry and so don't encode here."""
    # when not using python objects, a null message should be an empty string:
    if data is None and send_type in ['raw', 'multipart']:
        data = b''
    if send_type == 'multipart' and isinstance(data, bytes):
        # Wrap up a single string into a list so it doesn't get sent
        # as one character per message!
        data = [data]
    # Type error checking:
    if send_type == 'raw':
        if not isinstance(data, bytes):
            msg = 'raw sockets can only send bytes, not {}.'.format(type(data))
            raise TypeError(msg)
    elif send_type == 'string':
        if PY2 and isinstance(data, bytes):
            # Auto convert assuming UTF8:
            data = data.decode('utf8')
        if not isinstance(data, str):
            msg = 'string sockets can only send strings, not {}.'.format(type(data))
            raise TypeError(msg)
    elif send_type == 'multipart':
        if not all(isinstance(part, bytes) for part in data):
            msg = ('multipart sockets can only send an iterable of bytes objects, ' +
                   ' not {}.'.format(type(data)))
            raise TypeError(msg)
    elif send_type != 'pyobj':
        raise ValueError("send_type must be one of 'raw', 'multipart', 'string' or 'pyobj'")
    return data


class ZMQServer(object):

    def __init__(self, port, type='pyobj', bind_address='tcp://0.0.0.0'):
        self.type = type
        self.port = port
        self.bind_address = bind_address
        self.context = zmq.Context()
        
        self.auth = self.setup_auth(self.context)
        
        self.sock = self.context.socket(zmq.REP)
        self.sock.setsockopt(zmq.LINGER, 0)

        self.sock.bind('%s:%s' % (str(self.bind_address), str(self.port)))

        if self.type == 'raw':
            self.send = self.sock.send
            self.recv = self.sock.recv
        elif self.type == 'string':
            self.send = self.sock.send_string
            self.recv = self.sock.recv_string
        elif self.type == 'multipart':
            self.send = self.sock.send_multipart
            self.recv = self.sock.recv_multipart
        elif self.type == 'pyobj':
            self.send = partial(self.sock.send_pyobj, protocol=PICKLE_PROTOCOL)
            self.recv = self.sock.recv_pyobj
        else:
            raise ValueError("invalid protocol %s, must be 'raw', 'string', 'multipart' or 'pyobj'" % str(self.type))
            
        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def setup_auth(self, context):
        """To be overridden by subclasses. Setup your ZMQ Context's
        authentication here."""
        pass
            
    def shutdown_on_interrupt(self):
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            sys.stderr.write('Interrupted, shutting down\n')
        finally:
            self.shutdown()
            
    def mainloop(self):
        while True:
            try:
                request_data = self.recv()
            except zmq.ContextTerminated:
                self.sock.close(linger=0)
                return
            try:
                response_data = self.handler(request_data)
                response_data = _typecheck_or_convert_data(response_data, self.type)
            except Exception:
                # Raise the exception in a separate thread so that the
                # server keeps running:
                exc_info = sys.exc_info()
                raise_exception_in_thread(exc_info)
                exception_string = traceback.format_exc()
                response_data = zmq.ZMQError(
                    'The server had an unhandled exception whilst processing the request:\n%s' % str(exception_string))
                if self.type == 'raw':
                    response_data = str(response_data).encode('utf8')
                elif self.type == 'multipart':
                    response_data = [str(response_data).encode('utf8')]
                elif self.type == 'string':
                    response_data = str(response_data)
            self.send(response_data)

    def shutdown(self):
        self.context.term()
        self.mainloop_thread.join()

    def handler(self, request_data):
        """To be overridden by subclasses. This is an example
        implementation"""
        response = 'This is an example ZMQServer. Your request was %s.' % str(request_data)
        return response


class ZMQGet(object):

    def __init__(self, type='pyobj'):
        self.local = threading.local()
        self.type = type

    def new_socket(self, host, port):
        # Every time the REQ/REP cadence is broken, we need to create
        # and bind a new socket to get it back on track. Also, we have
        # a separate socket for each thread. Also a new socket if there
        # is a different host or port:
        self.local.host = gethostbyname(host)
        self.local.port = int(port)
        context = zmq.Context.instance()
        self.local.sock = context.socket(zmq.REQ)
        self.local.sock.setsockopt(zmq.LINGER, 0)
        self.local.poller = zmq.Poller()
        self.local.poller.register(self.local.sock, zmq.POLLIN)
        self.local.sock.connect('tcp://%s:%d' % (self.local.host, self.local.port))
        # Different send/recv methods depending on the desired protocol:
        if self.type == 'raw':
            self.local.send = self.local.sock.send
            self.local.recv = self.local.sock.recv
        elif self.type == 'string':
            self.local.send = self.local.sock.send_string
            self.local.recv = self.local.sock.recv_string
        elif self.type == 'multipart':
            self.local.send = self.local.sock.send_multipart
            self.local.recv = self.local.sock.recv_multipart
        elif self.type == 'pyobj':
            self.local.send = partial(self.local.sock.send_pyobj, protocol=PICKLE_PROTOCOL)
            self.local.recv = self.local.sock.recv_pyobj
        else:
            raise ValueError("invalid protocol %s, must be 'raw', 'string', 'multipart' or 'pyobj'" % str(self.type))

    def __call__(self, port, host='localhost', data=None, timeout=5):
        """Uses reliable request-reply to send data to a zmq REP socket, and returns the reply"""
        # We cache the socket so as to not exhaust ourselves of tcp
        # ports. However if a different server is in use, we need a new
        # socket. Also if we don't have a socket, we also need a new one:
        if not hasattr(self.local, 'sock') or gethostbyname(host) != self.local.host or int(port) != self.local.port:
            self.new_socket(host, port)
        data = _typecheck_or_convert_data(data, self.type)
        try:
            self.local.send(data, zmq.NOBLOCK)
            events = self.local.poller.poll(timeout * 1000)  # convert timeout to ms
            if events:
                response = self.local.recv()
            else:
                # The server hasn't replied. We don't know what it's doing,
                # so we'd better stop using this socket in case late messages
                # arrive on it in the future:
                raise TimeoutError('No response from server: timed out')
            if isinstance(response, Exception):
                raise response
            else:
                return response
        except:
            # Any exceptions, we want to stop using this socket:
            del self.local.sock
            raise

# Instantiate our zmq_get functions:
zmq_get = ZMQGet('pyobj')
zmq_get_multipart = ZMQGet('multipart')
zmq_get_string = ZMQGet('string')
zmq_get_raw = ZMQGet('raw')


class ZMQPush(object):

    def __init__(self, type='pyobj'):
        self.local = threading.local()
        self.type = type

    def new_socket(self, host, port):
        # Every time there is an exception, we need to create and
        # bind a new socket. Also, we have a separate socket for each
        # thread. Also a new socket if there is a different host or port:
        self.local.host = gethostbyname(host)
        self.local.port = int(port)
        context = zmq.Context.instance()
        self.local.sock = context.socket(zmq.PUSH)
        self.local.sock.setsockopt(zmq.LINGER, 0)
        self.local.sock.connect('tcp://%s:%d' % (self.local.host, self.local.port))
        # Different send/recv methods depending on the desired protocol:
        if self.type == 'pyobj':
            self.local.send = partial(self.local.sock.send_pyobj, protocol=PICKLE_PROTOCOL)
            self.local.recv = self.local.sock.recv_pyobj
        elif self.type == 'multipart':
            self.local.send = self.local.sock.send_multipart
            self.local.recv = self.local.sock.recv_multipart
        elif self.type == 'raw':
            self.local.send = self.local.sock.send
            self.local.recv = self.local.sock.recv

    def __call__(self, port, host='localhost', data=None, timeout=5):
        # We cache the socket so as to not exhaust ourselves of tcp
        # ports. However if a different server is in use, we need a new
        # socket. Also if we don't have a socket, we also need a new one:
        if not hasattr(self.local, 'sock') or gethostbyname(host) != self.local.host or int(port) != self.local.port:
            self.new_socket(host, port)
        data = _typecheck_or_convert_data(data, self.type)
        try:
            self.local.send(data, zmq.NOBLOCK)
        except:
            # Any exceptions, we want to stop using this socket:
            del self.local.sock
            raise

# Instantiate our zmq_push functions:
zmq_push = ZMQPush('pyobj')
zmq_push_multipart = ZMQPush('multipart')
zmq_push_raw = ZMQPush('raw')


class HeartbeatServer(object):

    """A server which receives messages from clients and echoes them
    back. There is only one server for however many clients there are"""
    instance = None

    def __init__(self):
        self.sock = context.socket(zmq.REP)
        self.port = self.sock.bind_to_random_port('tcp://127.0.0.1')
        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def mainloop(self):
        try:
            zmq.device(zmq.FORWARDER, self.sock, self.sock)
        except Exception:
            # Shutting down:
            return

    @classmethod
    def create_instance(cls):
        if cls.instance is None:
            cls.instance = cls()
        return cls.instance.port


class HeartbeatClient(object):
    instance = None

    def __init__(self, server_port, lock):
        self.lock = lock
        self.sock = context.socket(zmq.REQ)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.connect('tcp://127.0.0.1:{}'.format(server_port))
        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def mainloop(self):
        pid = str(os.getpid()).encode('utf8')
        while True:
            time.sleep(1)
            self.sock.send(pid, zmq.NOBLOCK)
            if not self.sock.poll(1000):
                break
            msg = self.sock.recv()
            if not msg == pid:
                break
        sys.stderr.write('Heartbeat failure\n')
        if self.lock is not None:
            with self.lock:
                os.kill(os.getpid(), signal.SIGTERM)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    @classmethod
    def create_instance(cls, server_port):
        if cls.instance is None:
            lock = threading.Lock()
            cls.instance = cls(server_port, lock)
        return cls.instance.lock


class WriteQueue(object):

    """Provides writing of python objects to the underlying zmq socket,
    with added locking. No reading is supported, once you put an object,
    you can't check what was put or whether the items have been gotten"""

    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()

    def put(self, obj):
        with self.lock:
            self.sock.send_pyobj(obj, protocol=PICKLE_PROTOCOL)


class ReadQueue(object):

    """provides reading and writing methods to the underlying zmq socket,
    with added locking. Actually there are two sockets, one for reading,
    one for writing. The only real use case for writing is when the
    read socket is blocking, but the process at the other end has died,
    and you need to stop the thread that is blocking on the read. So
    you send it a quit signal with put()."""

    def __init__(self, sock, to_self_sock):
        self.sock = sock
        self.to_self_sock = to_self_sock
        self.socklock = threading.Lock()
        self.to_self_sock_lock = threading.Lock()

    def get(self, timeout=None):
        with self.socklock:
            if timeout is not None:
                if not self.sock.poll(timeout*1000):
                    raise TimeoutError
            obj = self.sock.recv_pyobj()
        return obj

    def put(self, obj):
        with self.to_self_sock_lock:
            self.to_self_sock.send_pyobj(obj, protocol=PICKLE_PROTOCOL)


class OutputInterceptor(object):
    threadlocals_by_port = weakref.WeakValueDictionary()

    def __init__(self, port, streamname='stdout'):
        self.streamname = streamname
        self.real_stream = getattr(sys, streamname)
        self.fileno = self.real_stream.fileno
        # All instances with the same port will share a threadlocal object.
        # This way two (or more) instances called from the same thread will be
        # using the same zmq socket, and hence, their messages will arrive in
        # order.
        if port not in self.threadlocals_by_port:
            self.local = threading.local()
            self.threadlocals_by_port[port] = self.local
        self.local = self.threadlocals_by_port[port]
        self.port = port

    def new_socket(self):
        # One socket per thread, so we don't have to acquire a lock
        # to send:
        context = zmq.Context.instance()
        self.local.sock = context.socket(zmq.PUSH)
        self.local.sock.setsockopt(zmq.LINGER, 0)
        self.local.sock.connect('tcp://127.0.0.1:%d' % self.port)

    def connect(self):
        setattr(sys, self.streamname, self)

    def disconnect(self):
        setattr(sys, self.streamname, self.real_stream)

    def write(self, s):
        if not hasattr(self.local, 'sock'):
            self.new_socket()
        if isinstance(s, str):
            s = s.encode('utf8')
        self.local.sock.send_multipart([self.streamname.encode('utf8'), s])

    def close(self):
        self.disconnect()
        self.real_stream.close()

    def flush(self):
        pass

    def isatty(self):
        return False

        
class Broker(object):
    instance = None
     # If instance is None, then these ports are those of a Broker running in a parent process:
    server_ports = None

    def __init__(self):
        self.sub = context.socket(zmq.SUB)
        self.sub.setsockopt(zmq.SUBSCRIBE, b'')
        self.pub = context.socket(zmq.PUB)
        self.sub_port = self.sub.bind_to_random_port('tcp://127.0.0.1')
        self.pub_port = self.pub.bind_to_random_port('tcp://127.0.0.1')
        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()

    def mainloop(self):
        try:
            zmq.device(zmq.FORWARDER, self.sub, self.pub)
        except Exception:
            # Shutting down:
            return

    @classmethod
    def create_instance(cls):
        if we_are_the_top_process and cls.instance is None:
            cls.instance = cls()
            cls.server_ports = cls.instance.sub_port, cls.instance.pub_port
        return cls.server_ports

    @classmethod
    def set_server_ports(cls, sub_port, pub_port):
        cls.server_ports = sub_port, pub_port


class Event(object):

    def __init__(self, event_name, type='wait'):
        # Ensure we have a broker, whether it's in this process or a parent one:
        Broker.create_instance()
        broker_sub_port, broker_pub_port = Broker.server_ports
        self.event_name = event_name
        self.type = type
        if not type in ['wait', 'post', 'both']:
            raise ValueError("type must be 'wait', 'post', or 'both'")
        self.can_wait = self.type in ['wait', 'both']
        self.can_post = self.type in ['post', 'both']
        if self.can_wait:
            self.sub = context.socket(zmq.SUB)
            try:
                self.sub.setsockopt(zmq.HWM, 1000)  # ZMQ v2 only
            except:
                self.sub.set_hwm(1000)  # ZMQ v3+
            self.sub.setsockopt(zmq.SUBSCRIBE, self.event_name)
            self.sub.connect('tcp://127.0.0.1:%s' % broker_pub_port)
            self.poller = zmq.Poller()
            self.poller.register(self.sub, zmq.POLLIN)
            self.sublock = threading.Lock()
        if self.can_post:
            self.pub = context.socket(zmq.PUB)
            self.pub.connect('tcp://127.0.0.1:%s' % broker_sub_port)
            self.publock = threading.Lock()

    def post(self, id, data=None):
        if not self.can_post:
            raise ValueError("Instantiate Event with type='post' or 'both' to be able to post events")
        with self.publock:
            self.pub.send_multipart([self.event_name.encode('utf8'),
                                    str(id).encode('utf8'), pickle.dumps(data)])

    def wait(self, id, timeout=None):
        id = str(id)
        if not self.can_wait:
            raise ValueError("Instantiate Event with type='wait' or 'both' to be able to wait for events")
        # First check through events that are already in the buffer:
        while True:
            with self.sublock:
                events = self.poller.poll(0)
                if not events:
                    break
                event_name, event_id, data = self.sub.recv_multipart()
                event_name = event_name.decode('utf8')
                event_id = event_id.decode('utf8')
                data = pickle.loads(data)
                assert event_name == self.event_name
                if event_id == id:
                    return data
        # Since we might have to make several recv() calls before we get the right
        # id, we must implement our own timeout:
        start_time = time.time()
        while timeout is None or (time.time() < start_time + timeout):
            with self.sublock:
                if timeout is not None:
                    # How long left before the elapsed time is greater than timeout?
                    poll_timeout = max(0, (start_time + timeout - time.time()) * 1000)
                    events = self.poller.poll(poll_timeout)
                    if not events:
                        break
                event_name, event_id, data = self.sub.recv_multipart()
                event_name = event_name.decode('utf8')
                event_id = event_id.decode('utf8')
                data = pickle.loads(data)
                assert event_name == self.event_name
                if event_id == id:
                    return data
        raise TimeoutError('No event received: timed out')


def start_daemon(cmd_args):
    """calls subprocess.Popen configured to detach the subprocess from the
    parent, such that it keeps running even if the parent exits. Returns None.
    Note that the child process will have its current working directory set to
    the value of tempfile.gettempdir(), rather than remaining in the parent's
    working directory. In Windows this prevents it holding a lock on the
    current directory, which would prevent it from being deleted, and the
    behaviour is the same on unix for consistency."""
    import tempfile
    if os.name == 'nt':
        creationflags=0x00000008 # DETACHED_PROCESS from the win32 API
        subprocess.Popen(cmd_args,
                         creationflags=creationflags, stdout=None, stderr=None,
                         close_fds=True, cwd=tempfile.gettempdir())
    else:
        devnull = open(os.devnull,'w')
        if not os.fork():
            os.setsid()
            subprocess.Popen(cmd_args,
                             stdin=devnull, stdout=devnull, stderr=devnull,
                             close_fds=True, cwd=tempfile.gettempdir())
            os._exit(0)


def embed():
    """embeds an IPython qt console in the calling scope.
    Intended for debugging. May cause strange interpreter behaviour."""

    # Imports, including ones we only need in the qtconsole process,
    # so that the user gets errors about them rather than just no qtconsole:

    from IPython.utils.frame import extract_module_locals
    from ipykernel.kernelapp import IPKernelApp
    from IPython.core.interactiveshell import InteractiveShell
    import qtconsole.qtconsoleapp

    import atexit
    import sys

    from zmq.eventloop import ioloop


    def launch_qtconsole():
        subprocess.call([sys.executable, '-c',
                        'from qtconsole.qtconsoleapp import main; main()',
                        '--existing', app.connection_file])
        if not kernel_has_quit.is_set():
            ioloop.IOLoop.instance().stop()

    kernel_has_quit = threading.Event()
    qtconsole_thread = threading.Thread(target=launch_qtconsole)
    qtconsole_thread.daemon = True


    # Hack to prevent the kernel app from disabline SIGINT:
    IPKernelApp.init_signal = lambda self: None

    # Get some interpreter state that will need to be restored after the
    # kernel quits:
    sys_state = sys.stdin, sys.stdout, sys.stderr, sys.displayhook, sys.excepthook
    ps1 = getattr(sys, 'ps1', None)
    ps2 = getattr(sys, 'ps2', None)
    ps3 = getattr(sys, 'ps3', None)

    # Some of the below copied from ipykernel.embed.embed_kernel
    app = IPKernelApp()
    app.initialize([])

    # Remove the exit handler, we'll run it manually rather than at
    # interpreter exit:
    # atexit.unregister(app.kernel.shell.atexit_operations)

    # Undo unnecessary sys module mangling from init_sys_modules.
    # This would not be necessary if we could prevent it
    # in the first place by using a different InteractiveShell
    # subclass, as in the regular embed case.
    main = app.kernel.shell._orig_sys_modules_main_mod
    if main is not None:
        sys.modules[app.kernel.shell._orig_sys_modules_main_name] = main

    # load the calling scope if not given
    (caller_module, caller_locals) = extract_module_locals(1)
    app.kernel.user_module = caller_module
    app.kernel.user_ns = caller_locals
    app.shell.set_completer_frame()

    qtconsole_thread.start()
    try:
        app.start()
    finally:
        sys.stdin, sys.stdout, sys.stderr, sys.displayhook, sys.excepthook = sys_state
        if ps1 is not None:
            sys.ps1 = ps1
        else:
            del sys.ps1
        if ps2 is not None:
            sys.ps2 = ps2
        else:
            del sys.ps2
        if ps3 is not None:
            sys.ps3 = ps3
        else:
            del sys.ps3
        kernel_has_quit.set()


class Process(object):

    """A class providing similar functionality to multiprocessing.Process,
    but using zmq for communication and creating processes in a fresh
    environment rather than by forking (or imitation forking as in
    Windows). Do not override its methods other than run()."""

    def __init__(self, output_redirection_port=0, instantiation_is_in_subprocess=False):
        if not instantiation_is_in_subprocess:
            self._output_redirection_port = output_redirection_port

    def start(self, *args, **kwargs):
        """Call in the parent process to start a subprocess. Passes args and kwargs to the run() method"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'process_class_wrapper.py')
        self.to_child, self.from_child, self.child = subprocess_with_queues(path, self._output_redirection_port)
        # Get the file that the class definition is in (not this file you're reading now, rather that of the subclass):
        module_file = os.path.abspath(sys.modules[self.__module__].__file__)
        basepath, extension = os.path.splitext(module_file)
        if extension == '.pyc':
            module_file = basepath + '.py'
        if not os.path.exists(module_file):
            # Nope? How about this extension then?
            module_file = basepath + '.pyw'
        if not os.path.exists(module_file):
            # Still no? Well I can't really work out what the extension is then, can I?
            raise NotImplementedError("Can't find module file, what's going on, does it have an unusual extension?")
        # Send it to the child process so it can execute it in __main__, otherwise class definitions from
        # the users __main__ module will not be unpickleable. Note that though executed in __main__, the code's
        # __name__ will not be __main__, and so any main block won't execute, which is good!
        self.to_child.put([self.__module__, module_file, sys.path])
        self.to_child.put(self.__class__)
        self.to_child.put([args, kwargs])
        return self.to_child, self.from_child

    def _run(self, to_parent, from_parent, kill_lock):
        """Called in the child process to set up the connection with the parent"""
        self.to_parent = to_parent
        self.from_parent = from_parent
        self.kill_lock = kill_lock
        args, kwargs = from_parent.get()
        self.run(*args, **kwargs)

    def terminate(self):
        try:
            self.child.terminate()
        except WindowsError if os.name == 'nt' else None:
            pass  # process is already dead

    def run(self, *args, **kwargs):
        """The method that gets called in the subprocess. To be overridden by subclasses"""
        pass


def subprocess_with_queues(path, output_redirection_port=0):

    to_child = context.socket(zmq.PUSH)
    from_child = context.socket(zmq.PULL)
    to_self = context.socket(zmq.PUSH)

    port_from_child = from_child.bind_to_random_port('tcp://127.0.0.1')
    to_self.connect('tcp://127.0.0.1:%s' % port_from_child)
    broker_sub_port, broker_pub_port = Broker.create_instance()
    heartbeat_server_port = HeartbeatServer.create_instance()
    # If a custom process identifier has been set in zlock, ensure the child inherits it:
    zlock_process_identifier_prefix = ''
    if 'zlock' in sys.modules:
        zlock_process_identifier_prefix = sys.modules['zlock'].process_identifier_prefix
    child = subprocess.Popen([sys.executable, '-u', path, str(port_from_child),
                              str(heartbeat_server_port), str(output_redirection_port),
                              str(broker_sub_port), str(broker_pub_port),
                              zlock_process_identifier_prefix])
    # The child has 15 seconds to connect to us:
    events = from_child.poll(15000)
    if not events:
        raise RuntimeError('child process did not connect within the timeout.')
    port_to_child = from_child.recv().decode('utf8')
    to_child.connect('tcp://127.0.0.1:%s' % port_to_child)

    to_child = WriteQueue(to_child)
    from_child = ReadQueue(from_child, to_self)

    return to_child, from_child, child


def setup_connection_with_parent(lock=False):
    global we_are_the_top_process
    we_are_the_top_process = False
    port_to_parent = int(sys.argv[1])
    port_to_heartbeat_server = int(sys.argv[2])
    output_redirection_port = int(sys.argv[3])
    broker_sub_port = int(sys.argv[4])
    broker_pub_port = int(sys.argv[5])
    zlock_process_identifier_prefix = sys.argv[6]
    # If a custom process identifier has been set in zlock, ensure we inherit it:
    if zlock_process_identifier_prefix:
        import zprocess.locking
        # Append '-sub' to indicate we're a subprocess, if it's not already there
        if not zlock_process_identifier_prefix.endswith('sub'):
            zlock_process_identifier_prefix += 'sub'
        # Only set it if the user has not already set it to something in this process:
        if not zprocess.locking.process_identifier_prefix:
            zprocess.locking.set_client_process_name(zlock_process_identifier_prefix)
    to_parent = context.socket(zmq.PUSH)
    from_parent = context.socket(zmq.PULL)
    to_self = context.socket(zmq.PUSH)

    port_from_parent = from_parent.bind_to_random_port('tcp://127.0.0.1')
    to_self.connect('tcp://127.0.0.1:%s' % port_from_parent)

    to_parent.connect("tcp://127.0.0.1:%s" % port_to_parent)
    to_parent.send(str(port_from_parent).encode('utf8'))

    from_parent = ReadQueue(from_parent, to_self)
    to_parent = WriteQueue(to_parent)

    if output_redirection_port:  # zero indicates no output redirection
        stdout = OutputInterceptor(output_redirection_port)
        stderr = OutputInterceptor(output_redirection_port, 'stderr')
        stdout.connect()
        stderr.connect()
    #sys.stdin = StdInHook()

    kill_lock = HeartbeatClient.create_instance(port_to_heartbeat_server)
    Broker.set_server_ports(broker_sub_port, broker_pub_port)
    if lock:
        return to_parent, from_parent, kill_lock
    else:
        return to_parent, from_parent
