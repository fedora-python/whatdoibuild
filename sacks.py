import functools

import dnf

from utils import CONFIG, log

MULTILIB = {'x86_64': 'i686'} # architectures to exclude in certain queries
ARCH = CONFIG['architectures']['repoquery']

@functools.cache
def _base(repo_key):
    f"""
    Creates a DNF base from repositories defined in CONFIG['repos'], based on the given key.
    The sack is filled, which can be extremely slow if not already cached on disk in {CONFIG['cache_dir']['dnf']}.
    Cache is never invalidated here, remove the directory manually if needed.
    """
    base = dnf.Base()
    dnf_conf = base.conf
    dnf_conf.arch = ARCH
    dnf_conf.cachedir = CONFIG['cache_dir']['dnf']
    dnf_conf.substitutions['releasever'] = 'rawhide'
    dnf_conf.substitutions['basearch'] = ARCH
    for repo in CONFIG['repos'][repo_key]:
        base.repos.add_new_repo(conf=dnf_conf, skip_if_unavailable=False, **repo)
    log(f'• Filling the DNF {repo_key} sack to/from {CONFIG["cache_dir"]["dnf"]}...', end=' ')
    base.fill_sack(load_system_repo=False, load_available_repos=True)
    log('done.')
    return base


def rawhide_group(group_id):
    """
    Return a rawhide comps group of a given id (a.k.a. name)
    """
    base = _base('rawhide')
    log('• Reading the comps information...', end=' ')
    base.read_comps()
    log('done.')
    for group in base.comps.groups_by_pattern(group_id):
        if group.id == group_id:
            return group
    raise ValueError(f'No such group {group_id}')


def rawhide_sack():
    """
    A filled sack to perform rawhide repoquries. See base() for details.
    """
    return _base('rawhide').sack


def target_sack():
    """
    A filled sack to perform target repoquries. See base() for details.
    """
    return _base('target').sack
