import functools
import sys

import dnf
import hawkey

from sacks import rawhide_sack, rawhide_group
from utils import log, stringify

# Some deps are only pulled in when those are installed:
DEFAULT_GROUPS = (
    # 'buildsys-build',  # for composed repo
    'build',  # for koji repo
)


def mandatory_packages_in_group(group_id):
    """
    For given group id (a.k.a. name),
    returns a set of names of mandatory packages in it.
    """
    group = rawhide_group(group_id)
    return {p.name for p in group.packages_iter()
            if p.option_type == dnf.comps.MANDATORY}


@functools.lru_cache(maxsize=1)
def mandatory_packages_in_groups(groups=DEFAULT_GROUPS):
    """
    For all group ids,
    returns a single set of names of mandatory packages in any of them.
    """
    all_mandatory_packages = set()
    for group in groups:
        all_mandatory_packages |= mandatory_packages_in_group(group)
    log(f'• Found {len(all_mandatory_packages)} default buildroot packages.')
    return all_mandatory_packages


@functools.cache
def buildrequires_of(package_name, extra_requires=()):
    """
    Given a package name, returns all buildrequires in their string representations.
    The result is a sorted, deduplicated tuple,
    so it can be hashed as an argument to other cached functions.

    This loads the BuildRequires from the rawhide-source repo,
    note that some packages may have different BuildRequires on different architectures
    and the architecture in the source repo is randomly selected by Koji.
    If you know some package is affected by this,
    you can manually add a hashable collection of extra_requires.

    The package name is searched in the source repo
    and this function only works if exactly 1 package is found.
    If multiple are found, something is wrong with the setup -> RuntimeError.
    If none is found, a package by that name does not exist -> ValueError.
    """
    sack = rawhide_sack()
    log(f'• Finding BuildRequires of {package_name}...', end=' ')
    pkgs = sack.query().filter(name=package_name, arch='src', latest=1).run()
    if not pkgs:
        raise ValueError(f'No SRPMs called {package_name} found.')
    if len(pkgs) > 1:
        raise RuntimeError(f'Too many SRPMs called {package_name} found: {pkgs!r}')
    log(f'found {len(pkgs[0].requires)+len(extra_requires)} requirements.')
    return tuple(
        sorted(
            set(str(r) for r in pkgs[0].requires) | set(str(r) for r in extra_requires)
        )
    )


@functools.cache
def resolve_requires(requires, ignore_weak_deps=True):
    """
    Given a hashable collection of requirements,
    resolves all of them and the default buildroot packages in the rawhide repos
    and returns a list of hawkey.Packages (in implicit hawkey order) to be installed.

    If ignore_weak_deps is true (the default), weak dependencies (e.g. Recommends) are ignored,
    which is what happens in mock/Koji as well.

    If hawkey wants to upgrade or erase stuff, something is wrong with the setup -> RuntimeError.
    If hawkey cannot resolve the set, the requires are not installable -> ValueError.
    """
    sack = rawhide_sack()
    goal = hawkey.Goal(sack)
    orig_len = len(requires)
    requires += tuple(mandatory_packages_in_groups())
    log(f'• Resolving {orig_len} requirements...', end=' ')
    for dep in requires:
        selector = hawkey.Selector(sack).set(provides=dep)
        goal.install(select=selector)
    if not goal.run(ignore_weak_deps=ignore_weak_deps):
        raise ValueError(f'Cannot resolve {stringify(requires)}')
    if goal.list_upgrades() or goal.list_erasures():
        raise RuntimeError('Got packages to upgrade or erase, that should never happen.')
    log(f'to {len(goal.list_installs())} installs.')
    return goal.list_installs()


@functools.cache
def resolve_buildrequires_of(package_name, *, extra_requires=(), ignore_weak_deps=True):
    """
    A glue function that takes a package name (and optional keyword arguments)
    and returns a resolved list of hawkey.Packages to install.

    See buildrequires_of() and resolve_requires() for details.
    """
    brs = buildrequires_of(package_name, extra_requires=extra_requires)
    return resolve_requires(brs, ignore_weak_deps=ignore_weak_deps)


if __name__ == '__main__':
    # this is merely to invoke the function via CLI for easier manual testing
    for package_name in sys.argv[1:]:
        installs = resolve_buildrequires_of(package_name)
        print(stringify(installs, '\n'))
