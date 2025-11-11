import fcntl
import pathlib
import subprocess

import pytest

from resolve_buildroot import buildrequires_of
from resolve_buildroot import resolve_buildrequires_of
from resolve_buildroot import mandatory_packages_in_groups
from sacks import ARCH
from utils import name_or_str


TESTS_DIR = pathlib.Path(__file__).parent
RAWHIDE_MOCK = f'fedora-rawhide-{ARCH}'


def test_mandatory_packages_in_groups_contains_bacis_packages():
    # we will only assert packages that are likely to stay in the buildroot forever
    assert mandatory_packages_in_groups() >= {
        'bash',
        'coreutils',
        'patch',
        'redhat-rpm-config',
        'rpm-build',
    }


def test_mandatory_packages_in_groups_contains_limited_number_of_packages():
    # we can assume that the size will not grow much, the trend is to remove them
    #     Fedora 20: 24
    #     Fedora 30: 23
    #     Fedora 36: 22
    assert len(mandatory_packages_in_groups()) < 25


# XXX this test has data copied from simple specfiles, but it can change
@pytest.mark.parametrize('package_name, expected', [
    ('fedora-obsolete-packages', ()),
    ('fedora-repos', ('gnupg', 'rpm', 'sed')),
    ('fedora-release', ('redhat-rpm-config > 121-1', 'systemd-rpm-macros')),
    ('redhat-rpm-config', ('perl-generators',)),
])
def test_buildrequires_simple(package_name, expected):
    assert buildrequires_of(package_name) == expected


# XXX the mock tests are sloooooow
# XXX sometimes, we might manually need to scrub our cache as well as mock's (--scrub=dnf-cache)
def run_mock(*cmd, **kwargs):
    kwargs.setdefault('check', True)
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    return subprocess.run(['mock', '-r', RAWHIDE_MOCK, '--no-bootstrap-chroot',
                           '--isolation=simple', *cmd], **kwargs)


def resolve_buildroot_in_mock(package_name):
    with open((TESTS_DIR / RAWHIDE_MOCK).with_suffix('.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run_mock('--init')
        run_mock('--update')
        run_mock('--dnf-cmd', 'builddep', package_name)
        packages = run_mock('--shell', 'rpm -qa --qf=%{NAME}\\\\n').stdout
    return set(packages.splitlines()) - {'gpg-pubkey'}  # pubkey only installed in mock


@pytest.mark.parametrize('package_name', ['pytest',
                                          'python-setuptools',
                                          'python-pip',
                                          'rpm'])
def test_resolve_buildrequires_of(package_name):
    expected = resolve_buildroot_in_mock(package_name)
    got = resolve_buildrequires_of(package_name)
    assert {name_or_str(p) for p in got} == expected
