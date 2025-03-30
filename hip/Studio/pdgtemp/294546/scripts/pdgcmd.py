#
# Copyright (c) <2020> Side Effects Software Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# NAME:	        pdgcmd.py ( Python )
#
# COMMENTS:     Utility methods for jobs that need to report back to PDG.
#               Not dependent on Houdini install.
#

from __future__ import print_function, absolute_import, unicode_literals

import importlib
import json
import locale
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import traceback
import time

from datetime import datetime
from io import TextIOWrapper, BytesIO
from random import uniform

# Maximum number of retries for callbacks that get ECONNREFUSED
max_retries = int(os.environ.get('PDG_RPC_RETRIES', 4))
# seconds of timeout on making socket connections for RPC
rpc_timeout = int(os.environ.get('PDG_RPC_TIMEOUT', 4))
# maximum seconds to wait between retries after a timeout
rpc_backoff = int(os.environ.get('PDG_RPC_MAX_BACKOFF', 4))
# Rpc back end can be overriden with environment
rpc_delegate_from_env = os.environ.get('PDG_RPC_DELEGATE', None)
# Seconds delay between batch item checks for readiness to cook
batch_poll_delay = float(os.environ.get('PDG_BATCH_POLL_DELAY', 1.0))
# Should we call release_job_slots and acquire_job_slots when batch polling?
# If the scheduler does not support this, we should avoid the RPC overhead
release_job_slots_on_poll = int(os.environ.get('PDG_RELEASE_SLOT_ON_POLL', 1))
# Should we always ignore RPC failures
ignore_rpc_errors = int(os.environ.get('PDG_RPC_IGNORE_ERRORS', 0))
# Maximum number of RPC failures before stopping further RPC attempts
max_rpc_errors = int(os.environ.get('PDG_RPC_MAX_ERRORS', 5))
# Maximum output array file size before splitting reporting into separate
# RPC calls
max_rpc_array_size = int(os.environ.get('PDG_RPC_MAX_ARRAY_SIZE', 2000))

# how much logging
verbose = int(os.environ.get('PDG_VERBOSE', 0))
LogDefault, LogVerbose = 0, 1

# Base class for implemeting a long-running service
class BaseService(object):
    def __init__(self, client_name, listener_name, address, port, timeout, log_file):
        self.client_name = client_name
        self.listener_name = listener_name

        self.address = address
        self.port = port
        self.timeout = timeout

        self.log_file = log_file

        self.should_stop = False

    def reset_service(self):
        printlog("Resetting service...")

    def stop_service(self):
        printlog("Stopping service...")
        self.should_stop = True

    def is_alive(self):
        return True

    def start(self):
        try:
            if self.log_file == 'stdout':
                log_path = 'stdout'
            elif self.log_file:
                log_path = os.path.expandvars(self.log_file)

                if not os.path.exists(log_path):
                    try:
                        os.makedirs(os.path.dirname(log_path))
                    except OSError:
                        if not os.path.isdir(os.path.dirname(log_path)):
                            log_path = 'stdout'
            else:
                log_path = ''

            if log_path == 'stdout':
                pass
            elif log_path == '':
                sys.stdout = open(os.devnull, 'w')
            else:
                sys.stdout = open(os.path.expandvars(log_path), 'w')

            from pdgutils import PDGNetMQRelay, PDGNetLogLevel
            from pdgutils import mqSetLogLevel

            try:
                from pdgnetrpc import PDGNetRPCMessage
            except ImportError:
                from pdgjob.pdgnetrpc import PDGNetRPCMessage

            printlog("Starting up service...")

            mqSetLogLevel(PDGNetLogLevel.PDGN_LOG_ERROR)
            self.client = PDGNetMQRelay(self)
            self.client.connectToMQServer(
                self.address, self.port, self.client_name,
                50, self.timeout, 1000, self.timeout/1000.0)

            if self.listener_name:
                ready_message = PDGNetRPCMessage(self.address, self.port,
                    self.listener_name, False, 'serviceClientReady',
                    self.timeout)
                ready_message({'name' : self.client_name})

            printlog("Service client ready.")
            return True
        except:
            self.client.disconnectFromMQServer()
            printlog(traceback.format_exc())
            return False

    def run(self):
        try:
            while True:
                time.sleep(0.1)

                if not self.client.isConnected():
                    return 0

                if self.should_stop:
                    self.client.disconnectFromMQServer()
                    printlog("Service client stopped.")
                    return 0
        except:
            self.client.disconnectFromMQServer()
            printlog(traceback.format_exc())
            return 1

# Utility for redirecting logs to a string buffer
class RedirectBuffer(object):
    def __init__(self, source, tee=True):
        self._buffer = ""
        self._source = source
        self._tee = tee

    def __enter__(self):
        sys.stdout = self
        return self

    def __exit__(self, type, value, traceback):
        sys.stdout = self._source

    def write(self, text):
        self._buffer += str(text)
        if self._tee:
            self._source.write(text)

    def flush(self):
        if self._tee:
            self._source.flush()

    def buffer(self):
        return self._buffer

# Utility for capturing output files from standard output
class StdOutReporter(TextIOWrapper):
    def __init__(self, log_format=''):
        TextIOWrapper.__init__(self, BytesIO(), errors='replace')
        self.term = sys.stdout
        sys.stdout = self

        if log_format:
            self.format = log_format
        else:
            self.format = r'OUTPUT_FILE:(.*);(.*)'

    def reportAndClose(self, work_item=None):
        self.seek(0)
        while True:
            line = self.readline()
            if not line:
                break
            match = re.search(self.format, line)
            if match:
                output_file = match.group(1).strip()
                output_tag = match.group(2).strip()
                if work_item:
                    work_item.addOutputFile(output_file, output_tag)
                else:
                    addOutputFile(output_file, result_data_tag=output_tag,
                        to_stdout=False, raise_exc=False)
        self.close()
        sys.stdout = self.term
        self.term = None

    def write(self, s):
        self.term.write(s)
        self.term.flush()
        try:
            return super(StdOutReporter, self).write(s)
        except TypeError:
            # redirect encoded byte strings directly to buffer
            return super(StdOutReporter, self).buffer.write(s)

# timestamps in output can be disabled
disable_timestamps = int(os.environ.get('PDG_DISABLE_TIMESTAMPS', 0))

# Disable RPCs if set with env, or if there is no PDG_RESULT_SERVER
if 'PDG_RESULT_SERVER' not in os.environ:
    disable_rpc = True
else:
    disable_rpc = int(os.environ.get('PDG_DISABLE_RPC', 0)) > 0
result_server_addr = None

rpc_delegate = None
rpc_num_errors = 0

try:
    theJobid = os.environ[os.environ['PDG_JOBID_VAR']]
except:
    theJobid = ''

if not disable_rpc:
    try:
        import xmlrpclib # python 2.x
    except ImportError:
        import xmlrpc.client as xmlrpclib

# list of (src_path, dest_path) for the local pathmap, created on demand
thePathMaps = None
# Passing $PDG_DELOCALIZE=0 turns off delocalization of paths - this is the
# substitution of __PDG_DIR__.
theDelocalizePaths = os.environ.get('PDG_DELOCALIZE', '1') != '0'

def decode_str(s):
    if sys.version_info.major >= 3:
        return s
    return s.decode(sys.getfilesystemencoding())

def is_str(s):
    if sys.version_info.major >= 3:
        str_type = str
    else:
        str_type = unicode
    return isinstance(s, (bytes, bytearray, str_type))

def setVerbosity(level):
    """
    Sets the global verbosity level
    """
    global verbose
    verbose = level

def printlog(msg, verbosity=LogDefault, timestamp=True, prefix='', end='\n'):
    """
    Print a log message, verbosity > 0 means only print when PDG_VERBOSE
    is set to a higher value
    """
    if verbosity > verbose:
        return

    # choose the encoding expected by stdout
    try:
        stdoutenc = sys.stdout.encoding
    except AttributeError:
        stdoutenc = None
    if not stdoutenc:
        try:
            stdoutenc = locale.getpreferredencoding()
            if not stdoutenc:
                stdoutenc = 'ascii'
        except:
            stdoutenc = 'ascii'

    msg_bytes = b''
    if (not disable_timestamps) and timestamp:
        now = datetime.now()
        now_s = now.strftime('%H:%M:%S.%f')[:-3]
        msg_bytes = ('[' + now_s + '] ').encode(stdoutenc)

    if prefix:
        msg_bytes += prefix.encode(stdoutenc, 'replace')
        msg_bytes += ': '.encode(stdoutenc)

    # If the msg has no encode method, assume that it's already a bytes
    # object instead of a string
    try:
        msg_bytes += msg.encode(stdoutenc, 'replace')
    except AttributeError:
        msg_bytes += msg

    if end:
        msg_bytes += end.encode(stdoutenc)

    try:
        # write bytes to underlying buffer if it exists
        sys.stdout.buffer.write(msg_bytes)
    except (TypeError, AttributeError):
        sys.stdout.write(msg_bytes.decode(stdoutenc, 'replace'))
    sys.stdout.flush()

def setResultServerAddr(force_server_addr):
    """
    Set server address for result callback.
    """
    global result_server_addr, disable_rpc, xmlrpclib
    result_server_addr = force_server_addr

    if result_server_addr:
        # Re-enable RPC calls unless disabled by env flag
        disable_rpc = int(os.environ.get('PDG_DISABLE_RPC', 0)) > 0

        if not disable_rpc:
            try:
                import xmlrpclib # python 2.x
            except ImportError:
                import xmlrpc.client as xmlrpclib

def getResultServerAddr():
    """
    Return the result server address (or proxy object).
    """
    global result_server_addr
    if result_server_addr:
        return result_server_addr
    elif 'PDG_RESULT_SERVER' in os.environ:
        return os.environ['PDG_RESULT_SERVER']
    else:
        return None

def getSplitServerHostPort(server_addr):
    """
    Returns 'url', 'port' from given 'url:port' format.
    """
    vals = server_addr.split(':')
    if len(vals) >= 2:
        return vals[0], int(vals[1])
    else:
        raise RuntimeError("Invalid result (MQ) server address {0}".format(server_addr))

result_client_id = None

def setResultClientId(client_id):
    """
    Set the client ID for sending RPCs.
    """
    global result_client_id
    result_client_id = client_id

def getResultClientId():
    """
    Get the client ID for sending RPCs.
    """
    if result_client_id:
        return result_client_id
    elif 'PDG_RESULT_CLIENT_ID' in os.environ:
        return os.environ['PDG_RESULT_CLIENT_ID']
    return ''

def setRpcDelegate(delegate_obj):
    """
    Set the RPc Delegate object, which is a factory for RpcProxy objects
    Passing None will re-query the environment, Passing a string will 
    try to import a module of the given name
    """
    global rpc_delegate
    if is_str(delegate_obj):
        # Import the given name
        rpc_delegate = importlib.import_module(delegate_obj)
    elif delegate_obj is not None:
        rpc_delegate = delegate_obj
    else:
        # PDGNet is a special case
        use_pdgutilset = os.environ.get('PDG_JOBUSE_PDGNET', '0') == '1'
        if use_pdgutilset:
            # import from houdini package and fall back on copied script
            try:
                rpc_delegate = importlib.import_module("pdgjob.pdgnetrpc")
            except ImportError:
                rpc_delegate = importlib.import_module("pdgnetrpc")
        else:
            # default is XMLRPC
            rpc_delegate = XMLRpcDelegate()
    #printlog('RPC Delegate: {}'.format(rpc_delegate))

class RpcError(Exception):
    """
    Exception thrown when an RPC communication error occurs
    """
    pass

class XMLRpcProxy():
    """
    Implementation of an RPC Proxy Object that uses XMLRPC.
    Implementations must define the following methods:

    call(self, fn_name, *args, **kwargs) -> None
    commitMulti(self, name): -> object
    """
    def __init__(self, server_proxy, raise_exc):
        self._s = server_proxy
        self._raise_exc = raise_exc

    def call(self, fn_name, *args, **kwargs):
        # perform the RPC.  In the case of multi-call it will just register the
        # call which gets later sent by commitMulti, by invoking __call__ on the
        # xmlrpclib.MultiCall
        fn_attr = getattr(self._s, fn_name)
        return self._invokeXMLRpcFn(fn_attr, fn_name, *args)

    def commitMulti(self, name):
        self._invokeXMLRpcFn(self._s, name)

    def _invokeXMLRpcFn(self, fn, fn_name, *args):
        # call the given function and retry if the connection is refused
        if disable_rpc:
            return

        global rpc_num_errors
        if rpc_num_errors >= max_rpc_errors:
            return

        try_count = max_retries + 1
        max_sleep = rpc_backoff
        try:
            socket.setdefaulttimeout(rpc_timeout)
            while try_count > 0:
                try:
                    return fn(*args)
                except socket.timeout:
                    # We can't safely retry in this case because we risk duplicating
                    # the action.  FIXME: If we had a transaction ID we could handle
                    # this on the server side.
                    printlog('Timed out waiting for server to complete action.'
                             ' You can increase the timeout by setting $PDG_RPC_TIMEOUT')
                    break
                except socket.error as e:
                    try_number = max_retries + 2 - try_count
                    if e.errno == 10061:
                        if try_count > 1:
                            backoff = uniform(max_sleep/(try_count-1), max_sleep/try_count)
                            printlog(
                                'Connection refused. Retry with back off: {:.1f}s {}/{}'.format(
                                    backoff, try_number, max_retries))
                            time.sleep(backoff)
                        continue
                    if try_count > 1:
                        printlog('Socket Error: {}. Retry {}/{}'.format(
                            e, try_number, max_retries))
                    else:
                        printlog('Socket Error: {}'.format(e))
                except xmlrpclib.Fault as err:
                    try_number = max_retries + 2 - try_count
                    printlog('Failed RPC {} with Fault: {}. Retry {}/{}'.format(
                        fn_name, err, try_number, max_retries))
                finally:
                    try_count -= 1
        except:
            if self._raise_exc:
                traceback.print_exc()
        finally:
            socket.setdefaulttimeout(None)

        rpc_num_errors += 1
        msg = 'Failed RPC to {}: {} {} [Error {}/{}]'.format(
            getResultServerAddr(),  fn_name, args, rpc_num_errors, max_rpc_errors)
        printlog(msg)

        if rpc_num_errors >= max_rpc_errors:
            printlog('Maximum RPC error count reached -- RPC has been disabled')

        if self._raise_exc:
            raise RpcError(msg)

class XMLRpcDelegate:
    """
    RPC Delegate for XMLRPC-based callbacks
    """
    def createRpcProxy(self, host, port, client_id, get_reply, multi, raise_exc=True):
        s = xmlrpclib.ServerProxy('http://{}:{}'.format(host, port))
        if multi:
            s = xmlrpclib.MultiCall(s)
        return XMLRpcProxy(s, raise_exc)

def _invokeRpc(get_reply, fn_name, *args, **kwargs):
    # Invoke a single RPC function call and return the result
    # optional keyword argument server_addr to override the default
    if verbose >= LogVerbose:
        start = time.time()

    server = kwargs.get('server_addr', None)
    raise_exc = kwargs.get('raise_exc', True)

    if ignore_rpc_errors:
        raise_exc = False

    s = _getRPCProxy(get_reply, False, server, raise_exc)
    result = s.call(fn_name, *args)
    if verbose >= LogVerbose:
        elapsed = time.time() - start
        printlog('RPC {} took {:.2f} ms'.format(fn_name, elapsed * 1000.0))
    return result

def _invokeMultiRpc(proxy, name):
    if verbose >= LogVerbose:
        start = time.time()
    proxy.commitMulti(name)
    if verbose >= LogVerbose:
        elapsed = time.time() - start
        printlog('RPC {} took {:.2f} ms'.format(name, elapsed * 1000.0))

def _getRPCProxy(get_reply, multi=False, server_addr=None, raise_exc=True):
    """
    Returns an RPC proxy object
    """
    global rpc_delegate

    if not server_addr:
        server_addr = getResultServerAddr()

    # Check for runtime overriding of the ServerProxy, which is done with 
    # setResultServerAddr()
    if not is_str(server_addr):
        return server_addr

    host, port = getSplitServerHostPort(server_addr)
    client_id = getResultClientId()

    try:
        return rpc_delegate.createRpcProxy(
            host, port, client_id, get_reply, multi, raise_exc)
    except:
        return rpc_delegate.createRpcProxy(
            host, port, client_id, get_reply, multi)

# set Rpc Deletage from environment when module loads
setRpcDelegate(rpc_delegate_from_env)

#
# Path Utilities

def delocalizePath(local_path):
    """
    Delocalize the given path to be rooted at __PDG_DIR__
    Requires PDG_DIR env var to be present
    """
    # de-localize the result_data path if possible
    # we do this by replacing the file prefix if it matches our expected env var
    
    # don't delocalize non-strings
    if sys.version_info.major >= 3:
        if not isinstance(local_path, str):
            return local_path
    else:
        if not isinstance(local_path, (unicode, str)):
            return local_path

    deloc_path = local_path
    if theDelocalizePaths:
        pdg_dir = os.environ.get('PDG_DIR', None)
        if not pdg_dir:
            return deloc_path
        
        pdg_dir_local = decode_str(pdg_dir)
        # our env var value might be in terms of another env var - so expand again
        pdg_dir_local = os.path.expandvars(pdg_dir_local)
        # normalize path to forward slashes
        pdg_dir_local = pdg_dir_local.replace('\\', '/')
        deloc_path = local_path.replace('\\', '/')
        # ensure pdg_dir_local does not have a trailing slash
        pdg_dir_local = pdg_dir_local.rstrip('/')
        ix = 0
        l_target = len(pdg_dir_local)
        while True:
            ix = deloc_path.find(pdg_dir_local, ix)
            if ix < 0:
                break
            # if the match isn't at the start, check that it has preceeding space 
            # and check that the character after the match is a space or seperator
            end_ix = ix + l_target
            if ix == 0 or (deloc_path[ix-1].isspace() or 
                            (deloc_path[ix-1] in (';',':'))):
                if end_ix >= len(deloc_path) or (
                    deloc_path[end_ix].isspace() or
                    (deloc_path[end_ix] in ('/', ';', ':'))):
                    deloc_path = deloc_path[:ix] + '__PDG_DIR__' + deloc_path[ix + l_target:]
            ix = end_ix
    return deloc_path

# Makes a directory if it does not exist, and is made to be safe against
# directory creation happening concurrent while we're attemtping to make it
def makeDirSafe(local_path):
    if not local_path:
        return

    try:
        os.makedirs(local_path)
    except OSError:
        if not os.path.isdir(local_path):
            raise

def _substitute_scheduler_vars(data):
    for var in ('PDG_DIR', 'PDG_ITEM_NAME', 'PDG_TEMP', 'PDG_RESULT_SERVER',
                'PDG_INDEX', 'PDG_SCRIPTDIR', 'PDG_ITEM_ID'):
        varsym = '__' + var + '__'
        if varsym in data:
            try:
                val = decode_str(os.environ[var])
                data = data.replace(varsym, val)
            except KeyError:
                pass

    # Attempt to replace PDG_HFS token with a real HFS path. This makes it
    # possible to define attributes and files that contain an HFS path that
    # wont resolve until it runs on a farm machine, and will therefore use
    # that machines local HFS path instead of the one from the submitter. This
    # even works if the farm machines HFS path is not on the shared drive,
    # for example.
    #
    # We have to try both HFS and PDG_HFS since non-Houdini jobs will stash
    # a custom HFS path into the $PDG_HFS variable in the farm wrapper script.
    try:
        if 'HFS' in os.environ:
            data = data.replace('__PDG_HFS__', os.environ['HFS'])
        elif 'PDG_HFS' in os.environ:
            data = data.replace('__PDG_HFS__', os.environ['PDG_HFS'])
    except:
        pass

    return data

def _applyPathMapForZone(loc_path, path_map):
    # Apply path mapping rules to the given path.  Supports
    # chaining of rules by applying mapping rules repeatedly until
    # there are no more matches
    changed = True
    while changed:
        changed = False
        for zonepath in path_map:
            if zonepath[2]:
                if zonepath[1] in loc_path:
                    continue

            if zonepath[3] == 1:
                if not loc_path.startswith(zonepath[0]):
                    continue
            elif zonepath[3] == 2:
                if not loc_path.endswith(zonepath[0]):
                    continue

            loc_path_new = loc_path.replace(zonepath[0], zonepath[1])
            changed |= (loc_path != loc_path_new)
            loc_path = loc_path_new
    return loc_path

def _applyPathMapping(loc_path):
    # Apply '*' zone maps
    loc_path = _applyPathMapForZone(loc_path, thePathMaps[0])
    # Apply local zone maps
    loc_path = _applyPathMapForZone(loc_path, thePathMaps[1])
    return loc_path

def localizePath(deloc_path):
    """
    Localize the given path.  This means replace any __PDG* tokens and
    expand env vars with the values in the current environment.  Also
    applies path mapping if PDG_PATHMAP is present.
    """
    global thePathMaps
    loc_path = _substitute_scheduler_vars(deloc_path)
    loc_path = os.path.expandvars(loc_path)
    # support env vars defined as other env vars
    loc_path = os.path.expandvars(loc_path)
    loc_path = loc_path.replace("\\", "/")
    if thePathMaps is None:
        thePathMaps = _buildPathMap()
    loc_path = _applyPathMapping(loc_path)

    # Expand variables one more time for variables introduced by path maps.
    loc_path = os.path.expandvars(loc_path)

    return loc_path

def _buildPathMap():
    # Returns a pair of path mappings: [['*' zone mappings], [localzone mappings]]
    zonepaths = [[], []]
    pathmap = os.environ.get('PDG_PATHMAP', '')
    if not pathmap:
        return zonepaths
    try:
        pathmap = json.loads(pathmap)
    except ValueError as e:
        raise type(e)(str(e) + ': While parsing $PDG_PATHMAP')

    myzone = os.environ.get('PDG_PATHMAP_ZONE', '')
    if not myzone:
        if sys.platform.lower() == 'win32':
            myzone = 'WIN'
        elif sys.platform.lower() == 'darwin':
            myzone = 'MAC'
        elif sys.platform.lower().startswith('linux'):
            myzone = 'LINUX'
        else:
            printlog('Warning: Unsupported platform {} for Path Map'.format(
                sys.platform))
            return zonepaths

    def load_pathmap_for_zone(zone):
        paths = pathmap['paths']
        for e in paths:
            for from_path, v in e.items():
                e_zone = v['zone']
                if e_zone == zone or e_zone == '*':
                    to_path  = v['path']
                    from_path = from_path.replace('\\', '/')
                    to_path = to_path.replace('\\', '/')
                    match_type = v.get('matchtype', 0)

                    if from_path.endswith('/') and not to_path.endswith('/'):
                        to_path += '/'
                    is_subpath = to_path.startswith(from_path)
                    if e_zone == '*':
                        zonepaths[0].append((from_path, to_path, is_subpath, match_type))
                    else:
                        zonepaths[1].append((from_path, to_path, is_subpath, match_type))
        nmaps = len(zonepaths[0]) + len(zonepaths[1])
        if nmaps:
            printlog('PDG: Pathmap Zone {} with {} mappings for this zone.'.format(
                myzone, len(zonepaths[0]) + len(zonepaths[1])))
        return nmaps
    if not load_pathmap_for_zone(myzone):
        if myzone != 'WIN':
            # Backwards compatibility - POSIX has become MAC+LINUX
            nmaps = load_pathmap_for_zone('POSIX')
            if nmaps:
                printlog('Warning: "POSIX" Path Map Zone is deprecated'
                        ', please use "{}"'.format(myzone))
    return zonepaths

# Callback Helper Functions.
# These functions are used in task code to report status and results
# to the PDG callback server
#
def _getClientHost():
    try:
        net_hostname = (socket.gethostname() 
            if sys.platform == "darwin" else socket.getfqdn())
        ip_addr = socket.gethostbyname(net_hostname)
    except:
        try:
            net_hostname = platform.node()
            ip_addr = socket.gethostbyname(net_hostname)
        except:
            net_hostname = socket.gethostname()
            ip_addr = socket.gethostbyname(net_hostname)
    return (net_hostname, ip_addr)

def verboseAnnounce():
    # Print verbose job information, called by job wrapper script
    if verbose < LogVerbose:
        return
    myhost, myip = _getClientHost()
    client_id = getResultClientId()
    printlog('PDG Client is {} [{}] [{}]'.format(myhost, myip, client_id))
    server_addr = getResultServerAddr()
    if not server_addr:
        return
    host, port = getSplitServerHostPort(server_addr)
    try:
        ip = socket.gethostbyname(host)
        printlog('PDG Result Server is {}:{} [{}]'.format(host, port, ip))
    except Exception as e:
        printlog('PDG Result Server is {}:{} but FAILED to resolve:\n{}'.format(
            host, port, str(e))) 

def _checkItemIdArg(workitem_id):
    # API functions previously took item_name to identify the work item, we now
    # take work item id.
    import numbers
    if (workitem_id is None) or isinstance(workitem_id, numbers.Integral):
        return
    printlog('Passing PDG_ITEM_NAME as workitem_id for RPC functions is '
             'deprecated, please pass PDG_ITEM_ID int')

def getItemIdFromEnv():
    """
    Retrieve work_item.id from the job environment or None.
    """
    try:
        item_id = int(os.environ['PDG_ITEM_ID'])
    except KeyError:
        # Deprecated - use work_item.name
        item_id = os.environ.get('PDG_ITEM_NAME', None)
    return item_id

def waitUntilReady(workitem_id, subindex, server_addr=None, raise_exc=True):
    """
    Blocks until a batch sub item can begin cooking.

    subindex: the index of the batch item within it's batch.
    server_addr: the result server address, defaulting to
                 the value of $PDG_RESULT_SERVER
    raise_exc: whether or not RPC exceptions should be re-raised after logging
    """
    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)

    # we release our slots until we are ready to continue
    if release_job_slots_on_poll:
        _invokeRpc(False, 'release_job_slots', workitem_id, theJobid,
            server_addr=server_addr, raise_exc=raise_exc)
    while True:
        r = _invokeRpc(True, 'check_ready_batch', workitem_id, subindex,
            server_addr=server_addr, raise_exc=raise_exc)
        if r:
            enum_val = int(r)
            if enum_val == 1:
                break
            elif enum_val == 2:
                raise RuntimeError('Failed Dependency!')
        time.sleep(batch_poll_delay)
    if release_job_slots_on_poll:
        _invokeRpc(False, 'acquire_job_slots', workitem_id, theJobid,
            server_addr=server_addr, raise_exc=raise_exc)

def checkReady(workitem_id, subindex, server_addr=None, raise_exc=True):
    """
    Returns True if the work item is ready, False if it is not, and throws
    an exception if the work item is in an error state
    """
    if disable_rpc:
        return False

    _checkItemIdArg(workitem_id)
    r = _invokeRpc(True, 'check_ready_batch', workitem_id, subindex,
        server_addr=server_addr, raise_exc=raise_exc)
    if r:
        enum_val = int(r)
        if enum_val == 1:
            return True
        elif enum_val == 2:
            raise RuntimeError('Failed Dependency!')

    return False

def appendSubItem(workitem_id, server_addr=None, raise_exc=True):
    """
    Adds a sub item to the end of a running batch
    """
    if disable_rpc:
        return False

    _checkItemIdArg(workitem_id)
    r = _invokeRpc(True, 'add_sub_item_batch', workitem_id,
        server_addr=server_addr, raise_exc=raise_exc)
    if r:
        return int(r)
    return -1

def execBatchPoll(item_name, subindex, server_addr=None):
    printlog('execBatchPoll is deprecated, please use waitUntilReady instead.')
    return waitUntilReady(item_name, subindex, server_addr)

def getWorkItemJSON(workitem_id, subindex, server_addr=None, raise_exc=True):
    """
    Returns a string containing the serialized json for the given
    work item.
    subindex: the index of the batch item within it's batch.
    server_addr: the result server address, defaulting to
                 the value of $PDG_RESULT_SERVER
    raise_exc: whether or not RPC exceptions should be re-raised after logging
    """
    if disable_rpc:
        return ''
    _checkItemIdArg(workitem_id)

    return _invokeRpc(True, 'get_workitem_json', workitem_id, subindex,
        server_addr=server_addr, raise_exc=raise_exc)

def workItemSuccess(workitem_id, subindex=-1, server_addr=None, to_stdout=True, raise_exc=True):
    """
    Reports that the given item has succeeded.

    subindex:    the index of the batch item within it's batch.
    server_addr: the result server address, defaulting to
                 the value of $PDG_RESULT_SERVER
    to_stdout:   also emit a status message to stdout
    raise_exc:   whether or not RPC exceptions should be re-raised after logging
    """
    if to_stdout:
        printlog("PDG_SUCCESS: {};{};{}".format(workitem_id, subindex, 0))
    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)

    if subindex < 0:
        _invokeRpc(False, "success", workitem_id, 0, theJobid,
            server_addr=server_addr, raise_exc=raise_exc)
    else:
        _invokeRpc(False, "success_batch", workitem_id, subindex, 0, theJobid,
            server_addr=server_addr, raise_exc=raise_exc)

def execBatchSuccess(item_name, subindex, server_addr=None, to_stdout=True):
    printlog('execBatchSuccess is deprecated, please use workItemSuccess instead.')
    return workItemSuccess(item_name, subindex, server_addr, to_stdout)

def workItemFailed(workitem_id, server_addr=None, to_stdout=True, raise_exc=True):
    """
    Report when an item has failed.

    workitem_id: id of the associated work item
    server_addr: callback server in format 'IP:PORT', or emptry string to ignore
    to_stdout: also emit status messages to stdout
    raise_exc: whether or not RPC exceptions should be re-raised after logging
    
    Note: Batch subitems not supported.  Failure of a batch subitem will 
    automatically result in the failure of the batch item.
    """
    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)
    _invokeRpc(False, "failed", workitem_id, theJobid,
        server_addr=server_addr, raise_exc=raise_exc)

def execItemFailed(item_name, server_addr=None, to_stdout=True):
    printlog('execItemFailed is deprecated, please use workItemFailed instead.')
    return workItemFailed(item_name, server_addr, to_stdout)

def workItemStartCook(workitem_id=None, subindex=-1, server_addr=None, to_stdout=True, raise_exc=True):
    """
    Reports than a work item has started cooking.
    """
    if not workitem_id:
        workitem_id = getItemIdFromEnv()
    
    if to_stdout:
        printlog("PDG_START: {};{}".format(workitem_id, subindex))

    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)

    if subindex >= 0:
        _invokeRpc(False, "start_cook_batch", workitem_id, subindex, theJobid,
            server_addr=server_addr, raise_exc=raise_exc)
    else:
        _invokeRpc(False, "start_cook", workitem_id, theJobid,
            server_addr=server_addr, raise_exc=raise_exc)

def execStartCook(item_name=None, subindex=-1, server_addr=None, to_stdout=True):
    printlog('execStartCook is deprecated, please use workItemStartCook instead.')
    return workItemStartCook(item_name, subindex, server_addr, to_stdout)

def workItemSetCustomState(custom_state, workitem_id=None, subindex=-1, server_addr=None, to_stdout=True, raise_exc=True):
    """
    Sets a custom state string on the work item
    """
    if not workitem_id:
        workitem_id = getItemIdFromEnv()
    
    if to_stdout:
        printlog("PDG_SET_CUSTOM_STATE: {};{};{}".format(workitem_id, subindex, custom_state))

    if disable_rpc:
        return

    _checkItemIdArg(workitem_id)
    _invokeRpc(False, "set_custom_state", workitem_id, subindex, custom_state, theJobid,
        server_addr=server_addr, raise_exc=raise_exc)

def workItemSetCookPercent(cook_percent, workitem_id=None, subindex=-1, server_addr=None, to_stdout=True, raise_exc=True):
    """
    Sets a cook percentage indicator on the work item
    """
    if not workitem_id:
        workitem_id = getItemIdFromEnv()
    
    if to_stdout:
        printlog("onWorkItemSetCustomState: {};{};{}".format(workitem_id, subindex, cook_percent))

    if disable_rpc:
        return

    _checkItemIdArg(workitem_id)
    _invokeRpc(False, "set_cook_percent", workitem_id, subindex, cook_percent, theJobid,
        server_addr=server_addr, raise_exc=raise_exc)

def workItemAppendLog(log_data, log_type=3, workitem_id=None, subindex=-1, server_addr=None, to_stdout=True, raise_exc=True):
    """
    Report log data back to PDG for the work item
    """
    if not workitem_id:
        workitem_id = getItemIdFromEnv()
    
    if to_stdout:
        printlog("PDG_APPEND_LOG: {};{};{}".format(workitem_id, subindex, len(log_data)))

    if disable_rpc:
        return

    _checkItemIdArg(workitem_id)
    _invokeRpc(False, "append_log", workitem_id, subindex, log_data, int(log_type), theJobid,
        server_addr=server_addr, raise_exc=raise_exc)

def workItemCancelled(workitem_id, server_addr=None, raise_exc=True):
    """
    Report when a work item has been explicitly cancelled.

    workitem_id: id of the associated workitem
    server_addr: callback server in format 'IP:PORT', or emptry string to ignore
    raise_exc: whether or not RPC exceptions should be re-raised after logging
    
    Note: Batch subitems can not be cancelled, cancel the batch itself instead.
    """
    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)
    
    _invokeRpc(False, "cancelled", workitem_id, theJobid,
        server_addr=server_addr, raise_exc=raise_exc)

def _decodeValForPrint(val):
    if not is_str(val):
        return val

    if type(val) is bytearray:
        return '(bytearray length {})'.format(len(val))

    if sys.version_info.major >= 3:
        str_type = str
        if type(val) is str and len(val) > 260:
            return val[0:90] + '...({} bytes)'.format(len(val))
    else:
        str_type = unicode
    try:
        if len(val) > 260:
            decodedval = str_type(val[0:90], 'utf8', 'replace') +\
                '...(' + str_type(len(val)) + ' bytes)'
        else:
            decodedval = str_type(val, 'utf8', 'replace')
    except TypeError:
        return val
    return decodedval

def reportResultData(result_data, workitem_id=None, server_addr=None,
                     result_data_tag="", subindex=-1, and_success=False, to_stdout=True,
                     raise_exc=True, duration=0.0, hash_code=0, batch_size=50):
    """
    Deprecated in favor of addOutputFile
    """

    addOutputFile(result_data, workitem_id=workitem_id, server_addr=server_addr,
            result_data_tag=result_data_tag, subindex=subindex,
            and_success=and_success, to_stdout=to_stdout, raise_exc=raise_exc,
            duration=duration, hash_code=hash_code, batch_size=batch_size)

def addOutputFile(result_data, workitem_id=None, server_addr=None,
                  result_data_tag="", subindex=-1, and_success=False, to_stdout=True,
                  raise_exc=True, duration=0.0, hash_code=0, batch_size=50):
    """
    Reports an output file to PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    result_data:    output file path - treated as bytes if result_data_tag is passed
    result_data_tag: tag to categorize result.  Eg: 'file/geo'
                    Default is empty which means attempt to categorize using file extension.
    subindex:       The batch subindex if this is a batch item.
    and_success:    If True, report success in addition to result_data
    to_stdout:      also emit status messages to stdout
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    duration:       [Unused] cook time of the item in seconds, only report with and_success
    hash_code:      int that can be used to check if this file has changed, usually this is the modify-time of the file.
    batch_size:     Maximum number of results to send per multicall invoke
    """
    if not isinstance(result_data, (list, tuple)):
        all_result_data_list = [result_data]
    else:
        all_result_data_list = result_data
    n_results = len(all_result_data_list)

    if not all_result_data_list:
        raise TypeError("result_data is invalid")

    if not is_str(all_result_data_list[0]):
        raise TypeError("result_data must be string-like or a list of string-like")

    if not workitem_id:
        workitem_id = getItemIdFromEnv()

    if sys.version_info.major >= 3:
        str_type = str
    else:
        str_type = unicode
        
    def send_results(result_data_list):
        if not disable_rpc:
            proxy = _getRPCProxy(False, True, server_addr, raise_exc)

        for result_data_elem in result_data_list:
            # de-localize the result_data path if possible
            # we do this by replacing the file prefix if it matches our expected env var
            result_data_elem = delocalizePath(result_data_elem)

            if to_stdout:
                result_data_elem_print = _decodeValForPrint(result_data_elem)
                printlog('PDG_RESULT: {};{};{};{};{}'.format(workitem_id, subindex,
                    result_data_elem_print, result_data_tag, hash_code))
                if and_success:
                    printlog("PDG_SUCCESS: {};{};{}".format(workitem_id, subindex, duration))

            if not disable_rpc:
                if isinstance(result_data_elem, str_type):
                    # convert unicode to raw bytes, to be encoded with base64
                    result_data_elem = result_data_elem.encode('utf8')

                if and_success:
                    if subindex >= 0:
                        proxy.call('success_and_result_batch', workitem_id,
                            xmlrpclib.Binary(result_data_elem),
                            result_data_tag, subindex, hash_code, duration, theJobid)
                    else:
                        proxy.call('success_and_result', workitem_id,
                            xmlrpclib.Binary(result_data_elem),
                            result_data_tag, hash_code, duration, theJobid)
                else:
                    if subindex >= 0:
                        proxy.call('result_batch', workitem_id,
                            xmlrpclib.Binary(result_data_elem),
                            result_data_tag, subindex, hash_code, theJobid)
                    else:
                        proxy.call('result', workitem_id,
                            xmlrpclib.Binary(result_data_elem),
                            result_data_tag, hash_code, theJobid)
        if not disable_rpc:
            _invokeMultiRpc(proxy, 'addOutputFile')

    # The multicall RPC may not support unlimited payload size,
    # so to be safe we chunk it up into batches
    if n_results <= batch_size:
        send_results(all_result_data_list)
    else:
        chunks = [all_result_data_list[i:i+batch_size] for i in \
            range(0, n_results, batch_size)]
        for chunk in chunks:
            send_results(chunk)

def addOutputFiles(output_file_array, workitem_id=None, server_addr=None,
                   output_file_tag="", subindex=-1, to_stdout=True,
                   raise_exc=True, duration=0.0, hash_codes=[]):
    """
    Reports an output file to PDG via the callback server.

    workitem_id:        id of the associated workitem (default $PDG_ITEM_ID)
    server_addr:        callback server in format 'IP:PORT'
                        (default $PDG_RESULT_SERVER)
                        if there is no env var it will default to stdout
                        reporting only.
    output_file_array:  output file paths
    output_file_tag:    array of tag(s) to categorize files,  Eg: 'file/geo'
                        Default is empty which means attempt to categorize using
                        file extension.
    subindex:           The batch subindex if this is a batch item.
    to_stdout:          also emit status messages to stdout
    raise_exc:          whether or not RPC exceptions should be re-raised after
                        logging
    hash_codes:         int array that correpsonds to the output files
                        used to check if this file has changed, usually this is
                        the modify-time of the file.
    """
    if disable_rpc:
        return

    if not output_file_array:
        raise TypeError("output_file_array is invalid")

    if not is_str(output_file_array[0]):
        raise TypeError("output_file_array must bea list of string-like")

    if not workitem_id:
        workitem_id = getItemIdFromEnv()

    if sys.version_info.major >= 3:
        str_type = str
    else:
        str_type = unicode
       
    if to_stdout:
        printlog('PDG_RESULT_ARRAY: {};{};[{} files];[{} tag(s)]'.format(
            workitem_id, subindex, len(output_file_array),
            1 if is_str(output_file_tag) else len(output_file_tag)))

    transmit_files = []
    transmit_tags = []

    for i, output_file in enumerate(output_file_array):
        output_file = delocalizePath(output_file)
        if not output_file_tag:
            tag = ""
        elif is_str(output_file_tag):
            tag = output_file_tag
        else:
            tag = output_file_tag[i] if i < len(output_file_tag) else ""

        if to_stdout:
            output_file_print = _decodeValForPrint(output_file)
            hash_code = hash_codes[i] if i < len(hash_codes) else 0
            printlog('PDG_RESULT: {};{};{};{};{}'.format(workitem_id, subindex,
                output_file_print, tag, hash_code),
                verbosity=LogVerbose)

        if isinstance(output_file, str_type):
            # convert unicode to raw bytes, to be encoded with base64
            output_file = output_file.encode('utf8')

        transmit_files.append(xmlrpclib.Binary(output_file))
        transmit_tags.append(tag)

    proxy = _getRPCProxy(False, True, server_addr, raise_exc)

    def send_files(files, tags, hashes):
        if subindex >= 0:
            _invokeRpc(False, "result_array_batch", workitem_id, subindex,
                files, tags, hashes,
                theJobid, server_addr=server_addr, raise_exc=raise_exc)
        else:
            _invokeRpc(False, "result_array", workitem_id,
                files, tags, hashes,
                theJobid, server_addr=server_addr, raise_exc=raise_exc)

    # Limit the maximum number of files per RPC invocation
    if len(transmit_files) <= max_rpc_array_size:
        send_files(transmit_files, transmit_tags, hash_codes)
    else:
        for i in range(0, len(transmit_files), max_rpc_array_size):
            files = transmit_files[i:i+max_rpc_array_size]
            tags = transmit_tags[i:i+max_rpc_array_size]
            hashes = hash_codes[i:i+max_rpc_array_size]
            send_files(files, tags, hashes)

def writeAttribute(attr_name, attr_value, item_name=None, server_addr=None, raise_exc=True):
    """
    [Deprecated]
    Writes attribute data back into a work item in PDG via the callback server.

    item_name:      name of the associated workitem (default $PDG_ITEM_NAME)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     single value or array of string/float/int data
    """
    printlog("writeAttribute is deprecated, please use the set*Attrib functions")

    if not isinstance(attr_value, (list, tuple)):
        attr_value_list = [attr_value]
    else:
        attr_value_list = attr_value

    if not attr_value_list:
        raise TypeError("attr_value is invalid")

    if not is_str(attr_value_list[0]) and not isinstance(attr_value_list[0], (int, float)):
        raise TypeError("result_data must be string, int or float (array)")

    if not item_name:
        item_name = os.environ['PDG_ITEM_NAME']

    printlog("PDG_RESULT_ATTR: {};{};{}".format(item_name, attr_name, attr_value_list))

    if disable_rpc:
        return

    _invokeRpc(False, "write_attr", item_name, attr_name, attr_value_list, theJobid,
        server_addr=server_addr, raise_exc=raise_exc)

def _setAttrHelper(attr_name, attr_value, fname, workitem_id, subindex, server_addr, raise_exc):
    if not workitem_id:
        workitem_id = getItemIdFromEnv()

    if type(attr_value) is list:
        attr_value_print = [_decodeValForPrint(v) for v in attr_value]
    else:
        attr_value_print = _decodeValForPrint(attr_value)
    printlog("PDG_{}: {};{};{}".format(fname.upper().replace(' ', ''), workitem_id,
        attr_name, attr_value_print))

    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)

    _invokeRpc(False, fname, workitem_id, subindex, attr_name, attr_value,
        theJobid, server_addr=server_addr, raise_exc=raise_exc)

def _setAttrIndexHelper(attr_name, attr_value, attr_index, fname, workitem_id, subindex, server_addr, raise_exc):
    if not workitem_id:
        workitem_id = getItemIdFromEnv()

    attr_value_print = _decodeValForPrint(attr_value)
    printlog("PDG_{}: {};{};{}".format(fname.upper().replace(' ', ''), workitem_id,
        attr_name, attr_value_print))

    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)

    _invokeRpc(False, fname, workitem_id, subindex, attr_name, attr_value, attr_index,
        theJobid, server_addr=server_addr, raise_exc=raise_exc)

def setStringAttribArray(attr_name, attr_value, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     array of strings
    """
    _setAttrHelper(attr_name, attr_value, "set_string_attrib_array", workitem_id, subindex, server_addr, raise_exc)

def setIntAttribArray(attr_name, attr_value, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     array of integers
    """
    _setAttrHelper(attr_name, attr_value, "set_int_attrib_array", workitem_id, subindex, server_addr, raise_exc)

def setFloatAttribArray(attr_name, attr_value, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     array of floats
    """
    _setAttrHelper(attr_name, attr_value, "set_float_attrib_array", workitem_id, subindex, server_addr, raise_exc)

def setFileAttribArray(attr_name, attr_value, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     array of pdgjson.File objects
    """
    _setAttrHelper(attr_name, attr_value, "set_file_attrib_array", workitem_id, subindex, server_addr, raise_exc)

def setDictAttribArray(attr_name, attr_value, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     array of dictionary string representations
    """
    _setAttrHelper(attr_name, attr_value, "set_dict_attrib_array", workitem_id, subindex, server_addr, raise_exc)

def setPyObjectAttrib(attr_name, attr_value, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     string that is a valid repr() of a python object
    """
    _setAttrHelper(attr_name, attr_value, "set_pyobject_attrib", workitem_id, subindex, server_addr, raise_exc)

def setStringAttrib(attr_name, attr_value, attr_index, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     string value
    """
    _setAttrIndexHelper(attr_name, attr_value, attr_index, "set_string_attrib", workitem_id, subindex, server_addr, raise_exc)

def setIntAttrib(attr_name, attr_value, attr_index, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     integer value
    """
    _setAttrIndexHelper(attr_name, attr_value, attr_index, "set_int_attrib", workitem_id, subindex, server_addr, raise_exc)

def setFloatAttrib(attr_name, attr_value, attr_index, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     float value
    """
    _setAttrIndexHelper(attr_name, attr_value, attr_index, "set_float_attrib", workitem_id, subindex, server_addr, raise_exc)

def setFileAttrib(attr_name, attr_value, attr_index, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     pdg.File value
    """
    _setAttrIndexHelper(attr_name, attr_value, attr_index, "set_file_attrib", workitem_id, subindex, server_addr, raise_exc)

def setDictAttrib(attr_name, attr_value, attr_index, workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Writes attribute data back into a work item in PDG via the callback server.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    attr_name:      name of the attribute
    attr_value:     dictionary string representation
    """
    _setAttrIndexHelper(attr_name, attr_value, attr_index, "set_dict_attrib", workitem_id, subindex, server_addr, raise_exc)

def invalidateCache(workitem_id=None, subindex=-1, server_addr=None, raise_exc=True):
    """
    Requests that the cache of the work item be invalidated by PDG. This forces
    downstream tasks to cook. The same effect can be achieved by adding an
    output file to the work item, however this method can be used to invalidate
    caches without explicitly adding a file.

    workitem_id:    id of the associated work item (default $PDG_ITEM_ID)
    subindex:       batch subindex of item (-1 indicates a non-batch item)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
                    if there is no env var it will default to stdout reporting only.
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    """

    if not workitem_id:
        workitem_id = getItemIdFromEnv()

    printlog("PDG_INVALIDATE_CACHE: {};{}".format(workitem_id, subindex))

    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)

    _invokeRpc(False, "invalidate_cache", workitem_id, subindex, theJobid, server_addr=server_addr, raise_exc=raise_exc)

def reportServerStarted(servername, pid, host, port, proto_type, log_fname, workitem_id=None,
                        server_addr=None, raise_exc=True):
    """
    Reports that a shared server has been started.

    workitem_id:    id of the associated workitem (default $PDG_ITEM_ID)
    server_addr:    callback server in format 'IP:PORT' (default $PDG_RESULT_SERVER)
    raise_exc:      whether or not RPC exceptions should be re-raised after logging
    """

    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)

    sharedserver_message = {
        "name" : servername,
        "pid" : pid,
        "host" : host,
        "port" : port,
        "proto_type" : proto_type
    }

    if not workitem_id:
        workitem_id = int(os.environ['PDG_ITEM_ID'])

    multicall = _getRPCProxy(False, True, server_addr, raise_exc)

    if sys.version_info.major >= 3:
        host = bytes(host, 'utf8')
        port = bytes(str(port), 'utf8')
        log_fname = bytes(log_fname, 'utf8')
    else:
        host = str(host)
        port = str(port)
        log_fname = str(log_fname)

    multicall.call('sharedserver_started', sharedserver_message, theJobid)
    multicall.call('result', workitem_id, xmlrpclib.Binary(host), "socket/ip", 0, theJobid)
    multicall.call('result', workitem_id, xmlrpclib.Binary(port), "socket/port", 0, theJobid)
    multicall.call('result', workitem_id, xmlrpclib.Binary(log_fname), "file/text/log", 0, theJobid)
    _invokeMultiRpc(multicall, 'reportServerStarted')

def warning(message, workitem_id=None, server_addr=None, raise_exc=True):
    if disable_rpc:
        return
    _checkItemIdArg(workitem_id)

    if not workitem_id:
        workitem_id = int(os.environ['PDG_ITEM_ID'])

    _invokeRpc(False, "warning", workitem_id, message, theJobid,
        server_addr=server_addr, raise_exc=raise_exc)

def keepalive(workitem_id=None, server_addr=None, raise_exc=True):
    """
    Called by the job wrapper script when the scheduler requires heartbeat signals.
    """
    if not workitem_id:
        workitem_id = int(os.environ['PDG_ITEM_ID'])
    _checkItemIdArg(workitem_id)
    _invokeRpc(False, 'keepalive', workitem_id, theJobid,
        server_addr=server_addr, raise_exc=raise_exc)

def sanitizeEnvironment(sanitize_list):
    if sys.platform.startswith("win"):
        return os.environ

    if not sanitize_list:
        return os.environ

    hfs = os.environ.get("HFS")
    if not hfs:
        return os.environ

    env_copy = os.environ.copy()

    for var_name in sanitize_list:
        paths = env_copy.get(var_name, "").split(":")
        sanitized_paths = [
            path for path in paths if hfs not in path
        ]
        sanitized_value = ":".join(sanitized_paths)
        env_copy[var_name] = sanitized_value

    return env_copy

    
def execCommand(work_item, command, toolName=None, sanitizeVars=[],
    proc_input=None, proc_output=subprocess.PIPE, log_line_callback=None):
    """
    Executes a command.
    log_line_callback: Callable[[WorkItem, str], None] Function that runs on
        each line of stdout produced by the command.
    """

    work_item.addMessage("Executing command: {}".format(command))

    try:
        input_pipe = subprocess.PIPE if proc_input else None
        with subprocess.Popen(
            shlex.split(command),
            env=sanitizeEnvironment(sanitizeVars),
            stdin=input_pipe,
            stdout=proc_output,
            stderr=subprocess.STDOUT) as process:

            out = ""
            while process.poll() is None:
                output = process.stdout.readline()
                if output:
                    line = output.strip().decode()
                    work_item.addMessage(line)
                    out += line

                    if log_line_callback is not None:
                        log_line_callback(work_item, line)

            if process.returncode != 0:
                work_item.addError(
                    "Processed failed with exit code '{}'".format(
                        process.returncode),
                    fail_task=True)

            return out

    except subprocess.CalledProcessError as cmd_err:
        work_item.addError(
            "Except when running command: {}".format(cmd_err),
            fail_task=True)
    except OSError as os_err:

        # OSError might be due to missing executable, if that's the
        # case, inform the user about it.
        # We could check this before trying to execute, but considering this is
        # the exception, I'd rather not check this every time we run the command

        try:
            import distutils.spawn

            executableName = shlex.split(command)[0]
            if not distutils.spawn.find_executable(executableName):
                work_item.addError(
                    "Could not find executable {}".format(executableName))
                work_item.addError(
                    "Are you sure you have {} installed?".format(toolName or executableName),
                    fail_task=True)
            else:
                work_item.addError(
                    "Problem executing command: '{}'".format(os_err),
                    fail_task=True)
        except:
            work_item.addError(
                "Problem executing command: '{}'".format(os_err),
                fail_task=True)

    return None

class PDGPingHelper():
    """
    Checks if PDG Result Server is reachable at a fixed period.
    """
    def __init__(self, host, port, min_check_period):
        self.result_server = (host, port)
        self.min_check_period = min_check_period
        self.next_check_time = time.time() + self.min_check_period

    def isReachable(self, raise_on_failure=False):
        """
        Returns True if PDG is reachable or if the minimum check period has not
        elapsed.
        """
        now = time.time()
        if self.next_check_time > now:
            return True
        self.next_check_time = now + self.min_check_period
        # ping the PDG Result Server port and raise an exception if it can't be
        # reached
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect(self.result_server)
            s.close()
        except Exception as e:
            print("Exception caught while contacting PDG Result Server:", e)
            sys.stdout.flush()
            # Might get a timeout or connection-refused error
            if raise_on_failure:
                raise RpcError(
                    'Could not reach PDG Result Server at {}:{}'.format(
                        *self.result_server))
            return False
        return True
    def ensureReachable(self):
        return self.isReachable(True)

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=\
"""
Runs an RPC as via command line.
The following env vars are expected:
PDG_ITEM_ID
PDG_RESULT_SERVER
PDG_JOBUSE_PDGNET
If PDG_JOBUSE_PDGNET=1:
PDG_RESULT_CLIENT_ID
PDG_HTTP_PORT
""")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--cancel', action='store_true',
                       help='Work Item has been cancelled')
    group.add_argument('--fail', action='store_true',
                       help='Work Item has failed')
    group.add_argument('--success', action='store_true',
                       help='Work Item succeeded')
    
    args = parser.parse_args()
    
    workitem_id = getItemIdFromEnv()

    if args.cancel:
        workItemCancelled(workitem_id)
    if args.fail:
        workItemFailed(workitem_id)
    if args.success:
        workItemSuccess(workitem_id)

if __name__ == "__main__":
    main()
