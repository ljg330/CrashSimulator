from tracereplay_python import *
import logging
from time import strptime, mktime, time


# This function will need some work when the file descriptor rework goes
# through.
def pipe_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering pipe entry handler')
    read_end_from_trace = int(syscall_object.args[0].value)
    write_end_from_trace = int(syscall_object.args[1].value.strip(']'))
    logging.debug('Read end from trace: %d', read_end_from_trace)
    logging.debug('Write end from trace: %d', write_end_from_trace)
    array_addr = tracereplay.peek_register(pid, tracereplay.EBX)
    add_replay_fd(read_end_from_trace)
    add_replay_fd(write_end_from_trace)
    noop_current_syscall(pid)
    tracereplay.populate_pipefd_array(pid,
                                      array_addr,
                                      read_end_from_trace,
                                      write_end_from_trace)
    apply_return_conditions(pid, syscall_object)


def dup_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering dup handler')
    validate_integer_argument(pid, syscall_object, 0, 0)
    oldfd = int(syscall_object.args[0].value)
    if should_replay_based_on_fd(pid, oldfd):
        noop_current_syscall(pid)
        returned_fd = int(syscall_object.ret[0])
        add_replay_fd(returned_fd)
        apply_return_conditions(pid, syscall_object)


def dup_exit_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering dup exit handler')
    exec_fd = tracereplay.peek_register(pid, tracereplay.EAX)
    trace_fd = int(syscall_object.ret[0])
    check_fd = offset_file_descriptor(trace_fd)
    logging.debug('Execution return value: %d', exec_fd)
    logging.debug('Trace return value: %d', trace_fd)
    logging.debug('Check fd: %d', check_fd)
    if ret_val_from_execution != check_ret_val_from_trace:
        raise Exception('Return value from execution ({}) differs from '
                        'check return value from trace ({})'
                        .format(ret_val_from_execution,
                                check_ret_val_from_trace))
    if ret_val_from_execution >= 0:
        add_os_fd_mapping(ret_val_from_execution, ret_val_from_trace)


def close_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering close entry handler')
    # Pull out everything we can check
    fd = tracereplay.peek_register(pid, tracereplay.EBX)
    fd_from_trace = syscall_object.args[0].value
    check_fd_from_trace = offset_file_descriptor(fd_from_trace)
    logging.debug('File descriptor from execution: %d', fd)
    logging.debug('File descriptor from trace: %d', fd_from_trace)
    logging.debug('Check fd from trace: %d', check_fd_from_trace)
    # Check to make sure everything is the same
    # Decide if this is a system call we want to replay
    if fd_from_trace in tracereplay.REPLAY_FILE_DESCRIPTORS:
        if fd_from_trace != fd:
            raise ReplayDeltaError('File descriptor from execution ({}) '
                                   'differs from file descriptor from '
                                   'trace ({})'
                                   .format(fd, fd_from_trace))
        logging.info('Replaying this system call')
        noop_current_syscall(pid)
        if syscall_object.ret[0] != -1:
            logging.debug('Got unsuccessful close call')
            fd = syscall_object.args[0].value
            try:
                tracereplay.REPLAY_FILE_DESCRIPTORS.remove(fd)
            except ValueError:
                pass
        apply_return_conditions(pid, syscall_object)
    else:
        logging.info('Not replaying this system call')
        # fd != 1 is a horrible hack to deal with programs that close stdout
        if offset_file_descriptor(fd_from_trace) != fd and fd != 1:
            raise ReplayDeltaError('File descriptor from execution ({}) '
                                   'differs from file descriptor from '
                                   'trace ({})'
                                   .format(fd, fd_from_trace))
        # This is a hack. We have to try to remove the mapping here because we
        # know the file descriptor from both the os and the trace.
        # Unfortunately, we don't know if the call was successful or not for
        # the execution until the call exits. All we can do is look at the
        # system call object and base our action on it.
        if fd >= 0 and syscall_object.ret[0] != -1:
            remove_os_fd_mapping(fd, fd_from_trace)


def close_exit_handler(syscall_id, syscall_object, pid):
    logging.debug('Entring close exit handler')
    ret_val_from_trace = syscall_object.ret[0]
    ret_val_from_execution = tracereplay.peek_register(pid, tracereplay.EAX)
    logging.debug('Return value from trace: %d', ret_val_from_trace)
    logging.debug('Return value from execution: %d', ret_val_from_execution)
    check_ret_val_from_trace = ret_val_from_trace
    if syscall_object.ret[0] == -1:
        logging.debug('Got unsuccessful close exit')
        errno_ret = (ERRNO_CODES[syscall_object.ret[1]] * -1)
        logging.debug('Errno return value: %d', errno_ret)
        check_ret_val_from_trace = errno_ret
    if ret_val_from_execution != check_ret_val_from_trace:
        raise Exception('Return value from execution ({}) differs from '
                        'Return value from trace ({})'
                        .format(ret_val_from_execution,
                                check_ret_val_from_trace))


def read_entry_handler(syscall_id, syscall_object, pid):
    fd = tracereplay.peek_register(pid, tracereplay.EBX)
    fd_from_trace = syscall_object.args[0].value
    logging.debug('File descriptor from execution: %s', fd)
    logging.debug('File descriptor from trace: %s', fd_from_trace)
    if fd_from_trace in tracereplay.REPLAY_FILE_DESCRIPTORS:
        if fd != int(fd_from_trace):
            raise Exception('File descriptor from execution differs from file '
                            'descriptor from trace')
        buffer_address = tracereplay.peek_register(pid, tracereplay.ECX)
        buffer_size_from_execution = tracereplay.peek_register(pid,
                                                               tracereplay.EDX)
        buffer_size_from_trace = int(syscall_object.args[2].value)
        logging.debug('Address: %x', buffer_address & 0xffffffff)
        logging.debug('Buffer size from execution: %d',
                      buffer_size_from_execution)
        logging.debug('Buffer size from trace: %d', buffer_size_from_trace)
        ret_val = int(syscall_object.ret[0])
        if buffer_size_from_execution != buffer_size_from_trace:
            raise ReplayDeltaError('Buffer size from execution does not match '
                                   'buffer size from trace')
        noop_current_syscall(pid)
        data = syscall_object.args[1].value.lstrip('"').rstrip('"')
        data = data.decode('string_escape')
        if len(data) != ret_val:
            raise ReplayDeltaError('Decoded bytes length does not equal '
                                   'return value from trace')
        tracereplay.populate_char_buffer(pid,
                                         buffer_address,
                                         data)
        apply_return_conditions(pid, syscall_object)
    else:
        logging.debug("Ignoring read call to untracked file descriptor")


# This thing must be here to handle exits for read calls that we let pass. This
# will go away once the new "open" machinery is in place and we intercept all
# calls to read.
def read_exit_handler(syscall_id, syscall_object, pid):
    pass


# Note: This handler only takes action on syscalls made to file descriptors we
# are tracking. Otherwise it simply does any required debug-printing and lets
# it execute
def write_entry_handler(syscall_id, syscall_object, pid):
    fd = tracereplay.peek_register(pid, tracereplay.EBX)
    fd_from_trace = syscall_object.args[0].value
    msg_addr = tracereplay.peek_register(pid, tracereplay.ECX)
    msg_len = tracereplay.peek_register(pid, tracereplay.EDX)
    logging.debug('Child attempted to write to FD: %s', fd)
    logging.debug('Child\'s message stored at: %s', msg_addr)
    logging.debug('Child\'s message length: %s', msg_len)
    if fd != 1 and fd_from_trace in tracereplay.REPLAY_FILE_DESCRIPTORS:
        logging.debug('We care about this file descriptor. No-oping...')
        noop_current_syscall(pid)
        apply_return_conditions(pid, syscall_object)


# Once again, this only has to be here until the new "open" machinery
# is in place
def write_exit_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering write exit handler')
    ret_val = tracereplay.peek_register(pid, tracereplay.EAX)
    ret_val_from_trace = int(syscall_object.ret[0])
    logging.debug('Return value from execution: %d', ret_val)
    logging.debug('Return value from trace: %d', ret_val_from_trace)
    if ret_val != ret_val_from_trace:
        raise ReplayDeltaError('Return value from execution ({}) differed '
                               'from return value from trace ({})'
                               .format(ret_val, ret_val_from_trace))


def llseek_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering llseek entry handler')
    if should_replay_based_on_fd(pid, int(syscall_object.args[0].value)):
        logging.debug('Call using replayed file descriptor. Replaying this '
                      'system call')
        noop_current_syscall(pid)
        if syscall_object.ret[0] != -1:
            result = int(syscall_object.args[2].value.strip('[]'))
            result_addr = int(tracereplay.peek_register(pid, tracereplay.ESI))
            logging.debug('result: %s', result)
            logging.debug('result_addr: %s', result_addr)
            logging.debug('Got successful llseek call')
            logging.debug('Populating result')
            tracereplay.populate_llseek_result(pid, result_addr, result)
        else:
            logging.debug('Got unsucceesful llseek call')
        apply_return_conditions(pid, syscall_object)


def llseek_exit_handler(syscall_id, syscall_object, pid):
    logging.debug('llseek exit handler doesn\'t do anything')


def getcwd_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering getcwd entry handler')
    array_addr = tracereplay.peek_register(pid, tracereplay.EBX)
    data = str(syscall_object.args[0].value.strip('"'))
    data_length = int(syscall_object.ret[0])
    noop_current_syscall(pid)
    if data_length != 0:
        logging.debug('Got successful getcwd call')
        logging.debug('Data: %s', data)
        logging.debug('Data length: %s', data_length)
        logging.debug('Populating character array')
        tracereplay.populate_char_buffer(pid,
                                         array_addr,
                                         data)
    else:
        logging.debug('Got unsuccessful getcwd call')
    apply_return_conditions(pid, syscall_object)


def readlink_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering readlink entry handler')
    array_addr = tracereplay.peek_register(pid, tracereplay.ECX)
    data = str(syscall_object.args[0].value.strip('"'))
    data_length = int(syscall_object.ret[0])
    noop_current_syscall(pid)
    if data_length != -1:
        logging.debug('Got successful readlink call')
        logging.debug('Data: %s', data)
        logging.debug('Data length: %s', data_length)
        logging.debug('Populating character array')
        tracereplay.populate_char_buffer(pid,
                                         array_addr,
                                         data)
    else:
        logging.debug('Got unsuccessful readlink call')
    apply_return_conditions(pid, syscall_object)


def statfs64_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering statfs64 handler')
    ebx = tracereplay.peek_register(pid, tracereplay.EBX)
    ecx = tracereplay.peek_register(pid, tracereplay.ECX)
    edx = tracereplay.peek_register(pid, tracereplay.EDX)
    edi = tracereplay.peek_register(pid, tracereplay.EDI)
    esi = tracereplay.peek_register(pid, tracereplay.ESI)
    logging.debug("EBX: %s, ECX: %s, EDX: %s, ESI: %s, EDI: %s",
                  ebx, ecx, edx, edi, esi)
    addr = edx
    noop_current_syscall(pid)
    if syscall_object.ret[0] != -1:
        logging.debug('Got successful statfs64 call')
        f_type = syscall_object.args[2].value
        f_type = int(f_type[f_type.rfind('=')+1:].strip('{}'), 16)
        f_bsize = syscall_object.args[3].value
        f_bsize = int(f_bsize[f_bsize.rfind('=')+1:])
        f_blocks = syscall_object.args[4].value
        f_blocks = int(f_blocks[f_blocks.rfind('=')+1:])
        f_bfree = syscall_object.args[5].value
        f_bfree = int(f_bfree[f_bfree.rfind('=')+1:])
        f_bavail = syscall_object.args[6].value
        f_bavail = int(f_bavail[f_bavail.rfind('=')+1:])
        f_files = syscall_object.args[7].value
        f_files = int(f_files[f_files.rfind('=')+1:])
        f_ffree = syscall_object.args[8].value
        f_ffree = int(f_ffree[f_ffree.rfind('=')+1:])
        f_fsid1 = syscall_object.args[9].value
        f_fsid1 = int(f_fsid1[f_fsid1.rfind('=')+1:].strip('{}'))
        f_fsid2 = int(syscall_object.args[10].value.strip('{}'))
        f_namelen = syscall_object.args[11].value
        f_namelen = int(f_namelen[f_namelen.rfind('=')+1:])
        f_frsize = syscall_object.args[12].value
        f_frsize = int(f_frsize[f_frsize.rfind('=')+1:])
        f_flags = syscall_object.args[13].value
        f_flags = int(f_flags[f_flags.rfind('=')+1:].strip('{}'))
        logging.debug('pid: %d', pid)
        logging.debug('addr: %x', addr & 0xffffffff)
        logging.debug('f_type: %x', f_type)
        logging.debug('f_bsize: %s', f_bsize)
        logging.debug('f_blocks: %s', f_blocks)
        logging.debug('f_bfree: %s', f_bfree)
        logging.debug('f_bavail: %s', f_bavail)
        logging.debug('f_files: %s', f_files)
        logging.debug('f_ffree: %s', f_ffree)
        logging.debug('f_fsid1: %s', f_fsid1)
        logging.debug('f_fsid2: %s', f_fsid2)
        logging.debug('f_namelen: %s', f_namelen)
        logging.debug('f_frsize: %s', f_frsize)
        logging.debug('f_flags: %s', f_flags)
        tracereplay.populate_statfs64_structure(pid,
                                                addr,
                                                f_type,
                                                f_bsize,
                                                f_blocks,
                                                f_bfree,
                                                f_bavail,
                                                f_files,
                                                f_ffree,
                                                f_fsid1,
                                                f_fsid2,
                                                f_namelen,
                                                f_frsize,
                                                f_flags)
    apply_return_conditions(pid, syscall_object)


def lstat64_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering lstat64 handler')
    noop_current_syscall(pid)
    if syscall_object.ret[0] != -1:
        logging.debug('Got successful lstat64 call')
        raise NotImplementedError('Successful lstat64 not supported')
    apply_return_conditions(pid, syscall_object)


def open_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering open entry handler')
    ebx = tracereplay.peek_register(pid, tracereplay.EBX)
    fn_from_execution = peek_string(pid, ebx)
    fn_from_trace = syscall_object.args[0].value.strip('"')
    logging.debug('Filename from trace: %s', fn_from_trace)
    logging.debug('Filename from execution: %s', fn_from_execution)
    if fn_from_execution != fn_from_trace:
        raise Exception('File name from execution ({}) differs from '
                        'file name from trace ({})'.format(fn_from_execution,
                                                           fn_from_trace))
    fd_from_trace = int(syscall_object.ret[0])
    if fd_from_trace == -1 or not is_mmapd_before_close(fd_from_trace):
        if fd_from_trace == -1:
            logging.debug('This is an unsuccessful open call. We will replay '
                          'it')
        else:
            logging.debug('File descriptor is not mmap\'d before it is closed '
                          'so we will replay it')
            if fd_from_trace not in tracereplay.REPLAY_FILE_DESCRIPTORS:
                tracereplay.REPLAY_FILE_DESCRIPTORS.append(fd_from_trace)
            else:
                raise ReplayDeltaError('Tried to already existing file '
                                       'descriptor to replay file descriptors '
                                       'list')
        noop_current_syscall(pid)
        apply_return_conditions(pid, syscall_object)
    else:
        logging.debug('Resultant file descriptor is mmap\'d before close. '
                      'Will not replay')


def open_exit_handler(syscall_id, syscall_object, pid):
    logging.debug('Entring open exit handler')
    ret_val_from_trace = int(syscall_object.ret[0])
    ret_val_from_execution = tracereplay.peek_register(pid, tracereplay.EAX)
    if ret_val_from_trace == -1:
        errno_ret = (ERRNO_CODES[syscall_object.ret[1]] * -1)
        logging.debug('Errno return value: %d', errno_ret)
        check_ret_val_from_trace = errno_ret
    else:
        check_ret_val_from_trace = offset_file_descriptor(ret_val_from_trace)
    logging.debug('Return value from execution: %d', ret_val_from_execution)
    logging.debug('Return value from trace: %d', ret_val_from_trace)
    logging.debug('Check return value from trace: %d',
                  check_ret_val_from_trace)
    if ret_val_from_execution != check_ret_val_from_trace:
        raise Exception('Return value from execution ({}) differs from '
                        'check return value from trace ({})'
                        .format(ret_val_from_execution,
                                check_ret_val_from_trace))
    if ret_val_from_execution >= 0:
        add_os_fd_mapping(ret_val_from_execution, ret_val_from_trace)


def fstat64_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering fstat64 handler')
    if not should_replay_based_on_fd(pid, int(syscall_object.args[0].value)):
        return
    buf_addr = tracereplay.peek_register(pid, tracereplay.ECX)
    logging.debug('ECX: %x', (buf_addr & 0xffffffff))
    if syscall_object.ret[0] == -1:
        logging.debug('Got unsuccessful fstat64 call')
    else:
        logging.debug('Got successful fstat64 call')
        # There should always be an st_dev
        idx, arg = find_arg_matching_string(syscall_object.args[1:],
                                            'st_dev')[0]
        st_dev1 = arg
        st_dev1 = int(st_dev1.split('(')[1])
        # must increment idx by 2 in order to account for slicing out the
        # initial file descriptor
        st_dev2 = syscall_object.args[idx+2].value
        st_dev2 = int(st_dev2.strip(')'))
        logging.debug('st_dev1: %s', st_dev1)
        logging.debug('st_dev2: %s', st_dev2)

        # st_rdev is optional
        st_rdev1 = 0
        st_rdev2 = 0
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_rdev')
        if len(r) > 0:
            idx, arg = r[0]
            logging.debug('We have a st_rdev argument')
            st_rdev1 = arg
            st_rdev1 = int(st_rdev1.split('(')[1])
            st_rdev2 = syscall_object.args[idx+2].value
            st_rdev2 = int(st_rdev2.strip(')'))
            logging.debug('st_rdev1: %d', st_rdev1)
            logging.debug('st_rdev2: %d', st_rdev2)

        # st_ino
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_ino')
        idx, arg = r[0]
        st_ino = int(arg.split('=')[1])
        logging.debug('st_ino: %d', st_ino)

        # st_mode
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_mode')
        idx, arg = r[0]
        st_mode = int(cleanup_st_mode(arg.split('=')[1]))
        logging.debug('st_mode: %d', st_mode)

        # st_nlink
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_nlink')
        idx, arg = r[0]
        st_nlink = int(arg.split('=')[1])
        logging.debug('st_nlink: %d', st_nlink)

        # st_uid
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_uid')
        idx, arg = r[0]
        st_uid = int(arg.split('=')[1])
        logging.debug('st_uid: %d', st_uid)

        # st_gid
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_gid')
        idx, arg = r[0]
        st_gid = int(arg.split('=')[1])
        logging.debug('st_gid: %d', st_gid)

        # st_blocksize
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_blksize')
        idx, arg = r[0]
        st_blksize = int(arg.split('=')[1])
        logging.debug('st_blksize: %d', st_blksize)

        # st_blocks
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_blocks')
        idx, arg = r[0]
        st_blocks = int(arg.split('=')[1])
        logging.debug('st_block: %d', st_blocks)

        # st_size is optional
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_size')
        if len(r) >= 1:
            idx, arg = r[0]
            st_size = int(arg.split('=')[1])
            logging.debug('st_size: %d', st_size)
        else:
            st_size = 0
            logging.debug('optional st_size not present')
        # st_atime
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_atime')
        idx, arg = r[0]
        value = arg.split('=')[1]
        if value == '0':
            logging.debug('Got zero st_atime')
            st_atime = 0
        else:
            logging.debug('Got normal st_atime')
            st_atime = int(mktime(strptime(value, '%Y/%m/%d-%H:%M:%S')))
        logging.debug('st_atime: %d', st_atime)

        # st_mtime
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_mtime')
        idx, arg = r[0]
        value = arg.split('=')[1]
        if value == '0':
            logging.debug('Got zero st_mtime')
            st_mtime = 0
        else:
            logging.debug('Got normal st_mtime')
            st_mtime = int(mktime(strptime(value, '%Y/%m/%d-%H:%M:%S')))
        logging.debug('st_mtime: %d', st_mtime)

        # st_ctime
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_ctime')
        idx, arg = r[0]
        value = arg.split('=')[1].strip('}')
        if value == '0':
            logging.debug('Got zero st_ctime')
            st_ctime = 0
        else:
            logging.debug('Got normal st_ctime')
            st_ctime = int(mktime(strptime(value, '%Y/%m/%d-%H:%M:%S')))
        logging.debug('st_ctime: %d', st_ctime)

        logging.debug('Injecting values into structure')
        logging.debug('pid: %d', pid)
        logging.debug('addr: %d', buf_addr)
        tracereplay.populate_stat64_struct(pid,
                                           buf_addr,
                                           int(st_dev1),
                                           int(st_dev2),
                                           st_blocks,
                                           st_nlink,
                                           st_gid,
                                           st_blksize,
                                           int(st_rdev1),
                                           int(st_rdev2),
                                           st_size,
                                           st_mode,
                                           st_uid,
                                           st_ino,
                                           st_ctime,
                                           st_mtime,
                                           st_atime)
    noop_current_syscall(pid)
    apply_return_conditions(pid, syscall_object)


def fstat64_exit_handler(syscall_id, syscall_object, pid):
    logging.debug('fstat64 exit handler does nothing')


def stat64_exit_handler(syscall_id, syscall_object, pid):
    pass


def stat64_entry_handler(syscall_id, syscall_object, pid):
    # horrible work arouund
    if syscall_object.args[0].value == '"/etc/resolv.conf"':
        logging.error('Workaround for stat64 problem')
        return
    buf_addr = tracereplay.peek_register(pid, tracereplay.ECX)
    logging.debug('ECX: %x', (buf_addr & 0xffffffff))
    if syscall_object.ret[0] == -1:
        logging.debug('Got unsuccessful stat64 call')
    else:
        logging.debug('Got successful stat64 call')
        # There should always be an st_dev
        idx, arg = find_arg_matching_string(syscall_object.args[1:],
                                            'st_dev')[0]
        st_dev1 = arg
        st_dev1 = int(st_dev1.split('(')[1])
        # must increment idx by 2 in order to account for slicing out the
        # initial file descriptor
        st_dev2 = syscall_object.args[idx+2].value
        st_dev2 = int(st_dev2.strip(')'))
        logging.debug('st_dev1: %s', st_dev1)
        logging.debug('st_dev2: %s', st_dev2)

        # st_rdev is optional
        st_rdev1 = 0
        st_rdev2 = 0
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_rdev')
        if len(r) > 0:
            idx, arg = r[0]
            logging.debug('We have a st_rdev argument')
            st_rdev1 = arg
            st_rdev1 = int(st_rdev1.split('(')[1])
            st_rdev2 = syscall_object.args[idx+2].value
            st_rdev2 = int(st_rdev2.strip(')'))
            logging.debug('st_rdev1: %d', st_rdev1)
            logging.debug('st_rdev2: %d', st_rdev2)

        # st_ino
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_ino')
        idx, arg = r[0]
        st_ino = int(arg.split('=')[1])
        logging.debug('st_ino: %d', st_ino)

        # st_mode
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_mode')
        idx, arg = r[0]
        st_mode = int(cleanup_st_mode(arg.split('=')[1]))
        logging.debug('st_mode: %d', st_mode)

        # st_nlink
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_nlink')
        idx, arg = r[0]
        st_nlink = int(arg.split('=')[1])
        logging.debug('st_nlink: %d', st_nlink)

        # st_uid
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_uid')
        idx, arg = r[0]
        st_uid = int(arg.split('=')[1])
        logging.debug('st_uid: %d', st_uid)

        # st_gid
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_gid')
        idx, arg = r[0]
        st_gid = int(arg.split('=')[1])
        logging.debug('st_gid: %d', st_gid)

        # st_blocksize
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_blksize')
        idx, arg = r[0]
        st_blksize = int(arg.split('=')[1])
        logging.debug('st_blksize: %d', st_blksize)

        # st_blocks
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_blocks')
        idx, arg = r[0]
        st_blocks = int(arg.split('=')[1])
        logging.debug('st_block: %d', st_blocks)

        # st_size is optional
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_size')
        if len(r) >= 1:
            idx, arg = r[0]
            st_size = int(arg.split('=')[1])
            logging.debug('st_size: %d', st_size)
        else:
            st_size = 0
            logging.debug('optional st_size not present')
        # st_atime
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_atime')
        idx, arg = r[0]
        value = arg.split('=')[1]
        if value == '0':
            logging.debug('Got zero st_atime')
            st_atime = 0
        else:
            logging.debug('Got normal st_atime')
            st_atime = int(mktime(strptime(value, '%Y/%m/%d-%H:%M:%S')))
        logging.debug('st_atime: %d', st_atime)

        # st_mtime
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_mtime')
        idx, arg = r[0]
        value = arg.split('=')[1]
        if value == '0':
            logging.debug('Got zero st_mtime')
            st_mtime = 0
        else:
            logging.debug('Got normal st_mtime')
            st_mtime = int(mktime(strptime(value, '%Y/%m/%d-%H:%M:%S')))
        logging.debug('st_mtime: %d', st_mtime)

        # st_ctime
        r = find_arg_matching_string(syscall_object.args[1:],
                                     'st_ctime')
        idx, arg = r[0]
        value = arg.split('=')[1].strip('}')
        if value == '0':
            logging.debug('Got zero st_ctime')
            st_ctime = 0
        else:
            logging.debug('Got normal st_ctime')
            st_ctime = int(mktime(strptime(value, '%Y/%m/%d-%H:%M:%S')))
        logging.debug('st_ctime: %d', st_ctime)

        logging.debug('Injecting values into structure')
        logging.debug('pid: %d', pid)
        logging.debug('addr: %d', buf_addr)
        tracereplay.populate_stat64_struct(pid,
                                           buf_addr,
                                           int(st_dev1),
                                           int(st_dev2),
                                           st_blocks,
                                           st_nlink,
                                           st_gid,
                                           st_blksize,
                                           int(st_rdev1),
                                           int(st_rdev2),
                                           st_size,
                                           st_mode,
                                           st_uid,
                                           st_ino,
                                           st_ctime,
                                           st_mtime,
                                           st_ctime)
    noop_current_syscall(pid)
    apply_return_conditions(pid, syscall_object)


def cleanup_st_mode(m):
    m = m.split('|')
    tmp = 0
    for i in m:
        if i[0] == '0':
            tmp = tmp | int(i, 8)
        else:
            tmp = tmp | STAT_CONST[i]
    return tmp


def fcntl64_entry_handler(syscall_id, syscall_object, pid):
    logging.debug('Entering fcntl64 entry handler')
    operation = syscall_object.args[1].value[0].strip('[]\'')
    noop_current_syscall(pid)
    if operation == 'F_GETFL' or operation == 'F_SETFL':
        apply_return_conditions(pid, syscall_object)
    else:
        raise NotImplementedError('Unimplemented fcntl64 operation {}'
                                  .format(operation))


def open_entry_debug_printer(pid, orig_eax, syscall_object):
    logging.debug('This call tried to open: %s',
                  peek_string(pid,
                              tracereplay.peek_register(pid,
                                                        tracereplay.EBX)))


def write_entry_debug_printer(pid, orig_eax, syscall_object):
    logging.debug('This call tried to write: %s',
                  peek_string(pid,
                              tracereplay.peek_register(pid,
                                                        tracereplay.ECX)))


def fstat64_entry_debug_printer(pid, orig_eax, syscall_object):
    logging.debug('This call tried to fstat: %s',
                  tracereplay.peek_register(pid, tracereplay.EBX))


def close_entry_debug_printer(pid, orig_eax, syscall_object):
    logging.debug('This call tried to close: %s',
                  tracereplay.peek_register(pid, tracereplay.EBX))


def dup_entry_debug_printer(pid, orig_eax, syscall_object):
    logging.debug('This call tried to dup: %d',
                  tracereplay.peek_register(pid, tracereplay.EBX))


def fcntl64_entry_debug_printer(pid, orig_eax, syscall_object):
    logging.debug('This call tried to fcntl: %d',
                  tracereplay.peek_register(pid, tracereplay.EBX))


def stat64_entry_debug_printer(pid, orig_eax, syscall_object):
    logging.debug('This call tried to use file descriptor: %d',
                  tracereplay.peek_register(pid, tracereplay.EBX))
