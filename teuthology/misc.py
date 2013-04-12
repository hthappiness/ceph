from cStringIO import StringIO

import os
import logging
import configobj
import getpass
import socket
import tarfile
import time
import urllib2
import urlparse
import yaml
import json

from teuthology import safepath
from teuthology import lockstatus
from .orchestra import run

log = logging.getLogger(__name__)

import datetime
stamp = datetime.datetime.now().strftime("%y%m%d%H%M")
global_jobid = None
checked_jobid = False

def get_testdir(ctx):
    if 'test_path' in ctx.teuthology_config:
        return ctx.teuthology_config['test_path']

    basedir = ctx.teuthology_config.get('base_test_dir', '/tmp/cephtest')

    global global_jobid
    global checked_jobid

    # check if a jobid exists in the machine status for all our targets
    # and if its the same jobid, use that as the subdir for the test
    if not checked_jobid:
        jobids = {}
        for machine in ctx.config['targets'].iterkeys():
            status = lockstatus.get_status(ctx, machine)
            jid = status['description'].split('/')[-1]
            jobids[jid] = 1
            if len(jobids) > 1:
                break
        if len(jobids) == 1:
            # same job id on all machines, use that as the test subdir
            (jobid,) = jobids.iterkeys()
            global_jobid = jobid
        checked_jobid = True

    # the subdir is chosen using the priority:
    # 1. jobid chosen by the teuthology beanstalk queue
    # 2. run name specified by teuthology schedule
    # 3. user@timestamp
    if global_jobid:
        log.debug('with jobid basedir: {b}'.format(b=str(global_jobid)))
        return '{basedir}/{jobid}'.format(
                    basedir=basedir,
                    jobid=global_jobid,
                    )
    elif hasattr(ctx, 'name') and ctx.name:
        log.debug('with name basedir: {b}'.format(b=basedir))
        # we need a short string to keep the path short
        import re
        m = re.match(r"(.*)-(.*)-(.*)-(.*)_(.*)-(.*)-(.*)-(.*)-(.*)", ctx.name)
        (u, y, m, d, hms, s, c, k, f) = m.groups()
        short = u[0:2] + y[2:3] + m[0:2] + d[0:2] + hms[0:2] + hms[3:5] + s[0] + c[0] + k[0] + f[0]
        return '{basedir}/{rundir}'.format(
                    basedir=basedir,
                    rundir=short,
                    )
    else:
        log.debug('basedir: {b}'.format(b=basedir))
        return '{basedir}/{user}{stamp}'.format(
                    basedir=basedir,
                    user=get_user()[0:2],
                    stamp=stamp)

def get_testdir_base(ctx):
    if 'test_path' in ctx.teuthology_config:
        return ctx.teuthology_config['test_path']
    return ctx.teuthology_config.get('base_test_dir', '/tmp/cephtest')

def get_ceph_binary_url(package=None,
                        branch=None, tag=None, sha1=None, dist=None,
                        flavor=None, format=None, arch=None):
    BASE = 'http://gitbuilder.ceph.com/{package}-{format}-{dist}-{arch}-{flavor}/'.format(
        package=package,
        flavor=flavor,
        arch=arch,
        format=format,
        dist=dist
        )

    if sha1 is not None:
        assert branch is None, "cannot set both sha1 and branch"
        assert tag is None, "cannot set both sha1 and tag"
    else:
        # gitbuilder uses remote-style ref names for branches, mangled to
        # have underscores instead of slashes; e.g. origin_master
        if tag is not None:
            ref = tag
            assert branch is None, "cannot set both branch and tag"
        else:
            if branch is None:
                branch = 'master'
            ref = branch

        sha1_url = urlparse.urljoin(BASE, 'ref/{ref}/sha1'.format(ref=ref))
        log.debug('Translating ref to sha1 using url %s', sha1_url)

        try:
            sha1_fp = urllib2.urlopen(sha1_url)
            sha1 = sha1_fp.read().rstrip('\n')
            sha1_fp.close()
        except urllib2.HTTPError as e:
            log.error('Failed to get url %s', sha1_url)
            raise e

    log.debug('Using %s %s sha1 %s', package, format, sha1)
    bindir_url = urlparse.urljoin(BASE, 'sha1/{sha1}/'.format(sha1=sha1))
    return (sha1, bindir_url)

def feed_many_stdins(fp, processes):
    while True:
        data = fp.read(8192)
        if not data:
            break
        for proc in processes:
            proc.stdin.write(data)

def feed_many_stdins_and_close(fp, processes):
    feed_many_stdins(fp, processes)
    for proc in processes:
        proc.stdin.close()

def get_mons(roles, ips):
    mons = {}
    mon_ports = {}
    mon_id = 0
    for idx, roles in enumerate(roles):
        for role in roles:
            if not role.startswith('mon.'):
                continue
            if ips[idx] not in mon_ports:
                mon_ports[ips[idx]] = 6789
            else:
                mon_ports[ips[idx]] += 1
            addr = '{ip}:{port}'.format(
                ip=ips[idx],
                port=mon_ports[ips[idx]],
                )
            mon_id += 1
            mons[role] = addr
    assert mons
    return mons

def generate_caps(type_):
    defaults = dict(
        osd=dict(
            mon='allow *',
            osd='allow *',
            ),
        mds=dict(
            mon='allow *',
            osd='allow *',
            mds='allow',
            ),
        client=dict(
            mon='allow rw',
            osd='allow rwx pool data, allow rwx pool rbd, allow rwx pool newpool',
            mds='allow',
            ),
        )
    for subsystem, capability in defaults[type_].items():
        yield '--cap'
        yield subsystem
        yield capability

def skeleton_config(ctx, roles, ips):
    """
    Returns a ConfigObj that's prefilled with a skeleton config.

    Use conf[section][key]=value or conf.merge to change it.

    Use conf.write to write it out, override .filename first if you want.
    """
    path = os.path.join(os.path.dirname(__file__), 'ceph.conf.template')
    t = open(path, 'r')
    skconf = t.read().format(testdir=get_testdir(ctx))
    conf = configobj.ConfigObj(StringIO(skconf), file_error=True)
    mons = get_mons(roles=roles, ips=ips)
    for role, addr in mons.iteritems():
        conf.setdefault(role, {})
        conf[role]['mon addr'] = addr
    # set up standby mds's
    for roles_subset in roles:
        for role in roles_subset:
            if role.startswith('mds.'):
                conf.setdefault(role, {})
                if role.find('-s-') != -1:
                    standby_mds = role[role.find('-s-')+3:]
                    conf[role]['mds standby for name'] = standby_mds
    return conf

def roles_of_type(roles_for_host, type_):
    prefix = '{type}.'.format(type=type_)
    for name in roles_for_host:
        if not name.startswith(prefix):
            continue
        id_ = name[len(prefix):]
        yield id_

def all_roles(cluster):
    for _, roles_for_host in cluster.remotes.iteritems():
        for name in roles_for_host:
            yield name

def all_roles_of_type(cluster, type_):
    prefix = '{type}.'.format(type=type_)
    for _, roles_for_host in cluster.remotes.iteritems():
        for name in roles_for_host:
            if not name.startswith(prefix):
                continue
            id_ = name[len(prefix):]
            yield id_

def is_type(type_):
    """
    Returns a matcher function for whether role is of type given.
    """
    prefix = '{type}.'.format(type=type_)
    def _is_type(role):
        return role.startswith(prefix)
    return _is_type

def num_instances_of_type(cluster, type_):
    remotes_and_roles = cluster.remotes.items()
    roles = [roles for (remote, roles) in remotes_and_roles]
    prefix = '{type}.'.format(type=type_)
    num = sum(sum(1 for role in hostroles if role.startswith(prefix)) for hostroles in roles)
    return num

def create_simple_monmap(ctx, remote, conf):
    """
    Writes a simple monmap based on current ceph.conf into <tmpdir>/monmap.

    Assumes ceph_conf is up to date.

    Assumes mon sections are named "mon.*", with the dot.
    """
    def gen_addresses():
        for section, data in conf.iteritems():
            PREFIX = 'mon.'
            if not section.startswith(PREFIX):
                continue
            name = section[len(PREFIX):]
            addr = data['mon addr']
            yield (name, addr)

    addresses = list(gen_addresses())
    assert addresses, "There are no monitors in config!"
    log.debug('Ceph mon addresses: %s', addresses)

    testdir = get_testdir(ctx)
    args = [
        '{tdir}/enable-coredump'.format(tdir=testdir),
        'ceph-coverage',
        '{tdir}/archive/coverage'.format(tdir=testdir),
        'monmaptool',
        '--create',
        '--clobber',
        ]
    for (name, addr) in addresses:
        args.extend(('--add', name, addr))
    args.extend([
            '--print',
            '{tdir}/monmap'.format(tdir=testdir),
            ])
    remote.run(
        args=args,
        )

def write_file(remote, path, data):
    remote.run(
        args=[
            'python',
            '-c',
            'import shutil, sys; shutil.copyfileobj(sys.stdin, file(sys.argv[1], "wb"))',
            path,
            ],
        stdin=data,
        )

def sudo_write_file(remote, path, data, perms=None):
    permargs = []
    if perms:
        permargs=[run.Raw('&&'), 'sudo', 'chmod', perms, path]
    remote.run(
        args=[
            'sudo',
            'python',
            '-c',
            'import shutil, sys; shutil.copyfileobj(sys.stdin, file(sys.argv[1], "wb"))',
            path,
            ] + permargs,
        stdin=data,
        )

def get_file(remote, path, sudo=False):
    """
    Read a file from remote host into memory.
    """
    args = []
    if sudo:
        args.append('sudo')
    args.extend([
            'cat',
            '--',
            path,
            ])
    proc = remote.run(
        args=args,
        stdout=StringIO(),
        )
    data = proc.stdout.getvalue()
    return data

def pull_directory(remote, remotedir, localdir):
    """
    Copy a remote directory to a local directory.
    """
    log.debug('Transferring archived files from %s:%s to %s',
              remote.shortname, remotedir, localdir)
    if not os.path.exists(localdir):
        os.mkdir(localdir)
    proc = remote.run(
        args=[
            'sudo',
            'tar',
            'c',
            '-f', '-',
            '-C', remotedir,
            '--',
            '.',
            ],
        stdout=run.PIPE,
        wait=False,
        )
    tar = tarfile.open(mode='r|', fileobj=proc.stdout)
    while True:
        ti = tar.next()
        if ti is None:
            break

        if ti.isdir():
            # ignore silently; easier to just create leading dirs below
            pass
        elif ti.isfile():
            sub = safepath.munge(ti.name)
            safepath.makedirs(root=localdir, path=os.path.dirname(sub))
            tar.makefile(ti, targetpath=os.path.join(localdir, sub))
        else:
            if ti.isdev():
                type_ = 'device'
            elif ti.issym():
                type_ = 'symlink'
            elif ti.islnk():
                type_ = 'hard link'
            else:
                type_ = 'unknown'
                log.info('Ignoring tar entry: %r type %r', ti.name, type_)
                continue
    proc.exitstatus.get()

def pull_directory_tarball(remote, remotedir, localfile):
    """
    Copy a remote directory to a local tarball.
    """
    log.debug('Transferring archived files from %s:%s to %s',
              remote.shortname, remotedir, localfile)
    out = open(localfile, 'w')
    proc = remote.run(
        args=[
            'sudo',
            'tar',
            'cz',
            '-f', '-',
            '-C', remotedir,
            '--',
            '.',
            ],
        stdout=out,
        wait=False,
        )
    proc.exitstatus.get()

# returns map of devices to device id links:
# /dev/sdb: /dev/disk/by-id/wwn-0xf00bad
def get_wwn_id_map(remote, devs):
    stdout = None
    try:
        r = remote.run(
            args=[
                'ls',
                '-l',
                '/dev/disk/by-id/wwn-*',
                ],
            stdout=StringIO(),
            )
        stdout = r.stdout.getvalue()
    except:
        log.error('Failed to get wwn devices! Using /dev/sd* devices...')
        return dict((d,d) for d in devs)

    devmap = {}

    # lines will be:
    # lrwxrwxrwx 1 root root  9 Jan 22 14:58 /dev/disk/by-id/wwn-0x50014ee002ddecaf -> ../../sdb
    for line in stdout.splitlines():
        comps = line.split(' ')
        # comps[-1] should be:
        # ../../sdb
        rdev = comps[-1]
        # translate to /dev/sdb
        dev='/dev/{d}'.format(d=rdev.split('/')[-1])

        # comps[-3] should be:
        # /dev/disk/by-id/wwn-0x50014ee002ddecaf
        iddev = comps[-3]

        if dev in devs:
            devmap[dev] = iddev

    return devmap

def get_scratch_devices(remote):
    """
    Read the scratch disk list from remote host
    """
    devs = []
    try:
        file_data = get_file(remote, "/scratch_devs")
        devs = file_data.split()
    except:
        r = remote.run(
                args=['ls', run.Raw('/dev/[sv]d?')],
                stdout=StringIO()
                )
        devs = r.stdout.getvalue().split('\n')

    log.debug('devs={d}'.format(d=devs))

    retval = []
    for dev in devs:
        try:
            remote.run(
                args=[
                    # node exists
                    'stat',
                    dev,
                    run.Raw('&&'),
                    # readable
                    'sudo', 'dd', 'if=%s' % dev, 'of=/dev/null', 'count=1',
                    run.Raw('&&'),
                    # not mounted
                    run.Raw('!'),
                    'mount',
                    run.Raw('|'),
                    'grep', '-q', dev,
                    ]
                )
            retval.append(dev)
        except:
            pass
    return retval

def wait_until_healthy(ctx, remote):
    """Wait until a Ceph cluster is healthy."""
    testdir = get_testdir(ctx)
    while True:
        r = remote.run(
            args=[
                '{tdir}/enable-coredump'.format(tdir=testdir),
                'ceph-coverage',
                '{tdir}/archive/coverage'.format(tdir=testdir),
                'ceph',
                'health',
                '--concise',
                ],
            stdout=StringIO(),
            logger=log.getChild('health'),
            )
        out = r.stdout.getvalue()
        log.debug('Ceph health: %s', out.rstrip('\n'))
        if out.split(None, 1)[0] == 'HEALTH_OK':
            break
        time.sleep(1)

def wait_until_osds_up(ctx, cluster, remote):
    """Wait until all Ceph OSDs are booted."""
    num_osds = num_instances_of_type(cluster, 'osd')
    testdir = get_testdir(ctx)
    while True:
        r = remote.run(
            args=[
                '{tdir}/enable-coredump'.format(tdir=testdir),
                'ceph-coverage',
                '{tdir}/archive/coverage'.format(tdir=testdir),
                'ceph',
                '--concise',
                'osd', 'dump', '--format=json'
                ],
            stdout=StringIO(),
            logger=log.getChild('health'),
            )
        out = r.stdout.getvalue()
        j = json.loads('\n'.join(out.split('\n')[1:]))
        up = len(j['osds'])
        log.debug('%d of %d OSDs are up' % (up, num_osds))
        if up == num_osds:
            break
        time.sleep(1)

def wait_until_fuse_mounted(remote, fuse, mountpoint):
    while True:
        proc = remote.run(
            args=[
                'stat',
                '--file-system',
                '--printf=%T\n',
                '--',
                mountpoint,
                ],
            stdout=StringIO(),
            )
        fstype = proc.stdout.getvalue().rstrip('\n')
        if fstype == 'fuseblk':
            break
        log.debug('ceph-fuse not yet mounted, got fs type {fstype!r}'.format(fstype=fstype))

        # it shouldn't have exited yet; exposes some trivial problems
        assert not fuse.exitstatus.ready()

        time.sleep(5)
    log.info('ceph-fuse is mounted on %s', mountpoint)

def reconnect(ctx, timeout, remotes=None):
    """
    Connect to all the machines in ctx.cluster.

    Presumably, some of them won't be up. Handle this
    by waiting for them, unless the wait time exceeds
    the specified timeout.

    ctx needs to contain the cluster of machines you
    wish it to try and connect to, as well as a config
    holding the ssh keys for each of them. As long as it
    contains this data, you can construct a context
    that is a subset of your full cluster.
    """
    log.info('Re-opening connections...')
    starttime = time.time()

    if remotes:
        need_reconnect = remotes
    else:
        need_reconnect = ctx.cluster.remotes.keys()

    for r in need_reconnect:
        r.ssh.close()

    while need_reconnect:
        for remote in need_reconnect:
            try:
                log.info('trying to connect to %s', remote.name)
                from .orchestra import connection
                remote.ssh = connection.connect(
                    user_at_host=remote.name,
                    host_key=ctx.config['targets'][remote.name],
                    keep_alive=True,
                    )
            except Exception:
                if time.time() - starttime > timeout:
                    raise
            else:
                need_reconnect.remove(remote)

        log.debug('waited {elapsed}'.format(elapsed=str(time.time() - starttime)))
        time.sleep(1)

def write_secret_file(ctx, remote, role, keyring, filename):
    testdir = get_testdir(ctx)
    remote.run(
        args=[
            '{tdir}/enable-coredump'.format(tdir=testdir),
            'ceph-coverage',
            '{tdir}/archive/coverage'.format(tdir=testdir),
            'ceph-authtool',
            '--name={role}'.format(role=role),
            '--print-key',
            keyring,
            run.Raw('>'),
            filename,
            ],
        )

def get_clients(ctx, roles):
    for role in roles:
        assert isinstance(role, basestring)
        PREFIX = 'client.'
        assert role.startswith(PREFIX)
        id_ = role[len(PREFIX):]
        (remote,) = ctx.cluster.only(role).remotes.iterkeys()
        yield (id_, remote)

def get_user():
    return getpass.getuser() + '@' + socket.gethostname()

def read_config(ctx):
    filename = os.path.join(os.environ['HOME'], '.teuthology.yaml')
    ctx.teuthology_config = {}
    with file(filename) as f:
        g = yaml.safe_load_all(f)
        for new in g:
            ctx.teuthology_config.update(new)

def get_mon_names(ctx):
    mons = []
    for remote, roles in ctx.cluster.remotes.items():
        for role in roles:
            if not role.startswith('mon.'):
                continue
            mons.append(role)
    return mons

# return the "first" mon (alphanumerically, for lack of anything better)
def get_first_mon(ctx, config):
    firstmon = sorted(get_mon_names(ctx))[0]
    assert firstmon
    return firstmon

def replace_all_with_clients(cluster, config):
    """
    Converts a dict containing a key all to one
    mapping all clients to the value of config['all']
    """
    assert isinstance(config, dict), 'config must be a dict'
    if 'all' not in config:
        return config
    norm_config = {}
    assert len(config) == 1, \
        "config cannot have 'all' and specific clients listed"
    for client in all_roles_of_type(cluster, 'client'):
        norm_config['client.{id}'.format(id=client)] = config['all']
    return norm_config

def deep_merge(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, list):
        assert isinstance(b, list)
        a.extend(b)
        return a
    if isinstance(a, dict):
        assert isinstance(b, dict)
        for (k, v) in b.iteritems():
            if k in a:
                a[k] = deep_merge(a[k], v)
            else:
                a[k] = v
        return a
    return b

def get_valgrind_args(testdir, name, v):
    if v is None:
        return []
    if not isinstance(v, list):
        v = [v]
    val_path = '/var/log/ceph/valgrind'.format(tdir=testdir)
    if '--tool=memcheck' in v or '--tool=helgrind' in v:
        extra_args = [
            '{tdir}/chdir-coredump'.format(tdir=testdir),
            'valgrind',
            '--suppressions={tdir}/valgrind.supp'.format(tdir=testdir),
            '--xml=yes',
            '--xml-file={vdir}/{n}.log'.format(vdir=val_path, n=name)
            ]
    else:
        extra_args = [
            '{tdir}/chdir-coredump'.format(tdir=testdir),
            'valgrind',
            '--suppressions={tdir}/valgrind.supp'.format(tdir=testdir),
            '--log-file={vdir}/{n}.log'.format(vdir=val_path, n=name)
            ]
    extra_args.extend(v)
    log.debug('running %s under valgrind with args %s', name, extra_args)
    return extra_args
