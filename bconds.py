import argparse
import datetime
import functools
import os
import pathlib
import sys

from gitrepo import patch_spec, refresh_or_clone
from utils import CONFIG, log, run

KOJI_ID_FILENAME = 'koji.id'
SRPM_EXTENSION = '.src.rpm'
SPEC_EXTENSION = '.spec'

KOJI_STATE_CLOSED = 'closed'
KOJI_FAILED_STATES = ('canceled', 'failed')

KOJI_ARTIFACT_RETENTION_DAYS = 7

reverse_id_lookup = {}


def bcond_cache_identifier(component_name, bcond_config, *, branch='', target=''):
    """
    Return an unique more or less human-readable string identifier for caching purposes.
    The form of the identifier is more or less:
        component_name:without_configuration:with_configuration:replacements_configuration:branch:target

    With "defaults" empty. E.g. the gcc package built without tests and docs in rawhide would be:
        gcc:docs-tests::::

    And a complex package without docs and tests but with bootstrap with replaced macro on f35 branch in f35-side-1234:
        complex:docs-tests:bootstrap:use_supernatular_forces:f35:f35-side-1234

    If multiple options are present, they are sorted for canonical representation
    and separated with dashes (not possible in macro names).
    The values of replaced macros are not stored, we assume it won't be needed.
    From the above notice, it is obvious the form might change in the future.
    """
    withouts_id = '-'.join(sorted(bcond_config.get('withouts', [])))
    withs_id = '-'.join(sorted(bcond_config.get('withs', [])))
    replacements_id = '-'.join(sorted(bcond_config.get('replacements', {})))
    if branch == CONFIG['distgit']['branch']:
        branch = ''
    identifier = f'{component_name}:{withouts_id}:{withs_id}:{replacements_id}:{branch}:{target}'
    reverse_id_lookup[identifier] = bcond_config
    return identifier


def srpm_path(directory):
    """
    Returns a path to a single SRPM found in the given directory.
    Returns None if not there.
    Raises RuntimeError when multiple SRPMs are found.
    """
    candidates = list(directory.glob(f'*{SRPM_EXTENSION}'))
    if not candidates:
        return None
    if (count := len(candidates)) > 1:
        raise RuntimeError(f'Found {count} SRPMs in {directory}.')
    return candidates[0]


def submit_scratchbuild(repopath, target=''):
    command = ('fedpkg', 'build', '--scratch', '--srpm',
               f'--arches={CONFIG["architectures"]["koji"]}', '--nowait', '--background')
    if target:
        command += (f'--target={target}',)
    try:
        log('   • Submitting Koji scratchbuild...', end=' ')
        fedpkg_output = run(*command, cwd=repopath).stdout
    finally:
        # we must cleanup the generated SRPM no matter what
        # not to confuse it with our Koji-downloaded one later
        if srpm := srpm_path(repopath):
            srpm.unlink()
    for line in fedpkg_output.splitlines():
        if line.startswith('Created task: '):
            koji_task_id = line.split(' ')[-1]
            log(f'task {koji_task_id}')
            koji_id_path = repopath / KOJI_ID_FILENAME
            koji_id_path.write_text(koji_task_id)
            return koji_task_id
    else:
        raise RuntimeError('Carnot parse fedpkg build output')


@functools.cache
def koji_status(koji_id):
    output = run('koji', 'taskinfo', koji_id).stdout.splitlines()
    for line in output:
        if line.startswith('State: '):
            return line.split(' ')[-1]
    raise RuntimeError('Cannot parse koji taskinfo output')


def koji_id_is_older_than_week(koji_id_path):
    """
    Returns True if koji_id file was created earlier than the retention period.
    We assume this is a threshold time after which Koji artifacts are deleted,
    and we need to remove koji_id to retrigger the scratchbuild.
    """
    koji_id_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(koji_id_path))
    retention_cutoff = datetime.datetime.now() - datetime.timedelta(days=KOJI_ARTIFACT_RETENTION_DAYS)
    return koji_id_mtime < retention_cutoff


def handle_existing_srpm(repopath, *, was_updated):
    srpm = srpm_path(repopath)
    if srpm and not was_updated:
        log(f'   • Found {srpm.name}, will not rebuild; remove it to force me.')
        return srpm
    if srpm:
        srpm.unlink()
    return None


def handle_existing_koji_id(repopath, *, was_updated):
    koji_id_path = repopath / KOJI_ID_FILENAME
    if koji_id_path.exists():
        if was_updated:
            koji_id_path.unlink()
            return None
        else:
            koji_task_id = koji_id_path.read_text()
            status = koji_status(koji_task_id)
            if status in KOJI_FAILED_STATES:
                log(f'   • Koji task {koji_task_id} is {status}; '
                    f'removing {KOJI_ID_FILENAME}.')
                koji_id_path.unlink()
                return None
            elif status == KOJI_STATE_CLOSED and koji_id_is_older_than_week(koji_id_path):
                log(f'   • Koji task {koji_task_id} is older than {KOJI_ARTIFACT_RETENTION_DAYS} days, '
                    f'there may be nothing to download; removing {KOJI_ID_FILENAME}.')
                koji_id_path.unlink()
            else:
                log(f'   • Koji task {koji_task_id} is {status}; '
                    f'not rebuilding (rm {KOJI_ID_FILENAME} to force).')
                return koji_task_id


def check_existing_artifacts(repopath, was_updated, bcond_config):
    """
    Check if we can reuse existing SRPM or Koji task.
    
    Args:
        repopath: Path to the repository
        was_updated: Whether the repository was just updated
        bcond_config: Configuration dict to update with found artifacts
    
    Returns:
        bool: True if existing artifact found and can be reused (no rebuild needed)
    """
    if srpm := handle_existing_srpm(repopath, was_updated=was_updated):
        bcond_config['srpm'] = srpm
        return True
    
    if koji_id := handle_existing_koji_id(repopath, was_updated=was_updated):
        bcond_config['koji_task_id'] = koji_id
        return True
    
    return False


def prepare_spec_for_build(component_name, repopath, bcond_config):
    """
    Prepare the spec file by applying bcond patches and optionally bumping release.
    
    Args:
        component_name: Name of the component
        repopath: Path to the repository
        bcond_config: Configuration with bcond settings
    
    Returns:
        Path to the prepared spec file
    """
    specpath = repopath / f'{component_name}{SPEC_EXTENSION}'
    patch_spec(specpath, bcond_config)
    
    if 'bootstrap' in bcond_config.get('withs', ()):
        # bump the release not to create an older EVR with ~bootstrap
        # this is useful if we build the testing SRPMs in copr
        run('rpmdev-bumpspec', '--rightmost', specpath)
    
    return specpath


def scratchbuild_patched_if_needed(component_name, bcond_config, *, branch='', target='', no_git_refresh=False):
    """
    Clone/refresh a component repository and submit a Koji scratchbuild if needed.
    
    This function:
     1. Clones/fetches the component package from Fedora to fedpkg_cache_dir
     2. Checks for existing SRPM or Koji task (returns early if found)
     3. Prepares the specfile with bcond/macro patches
     4. Submits a scratchbuild to Koji
     5. Updates bcond_config with the Koji task ID
    
    Args:
        component_name: Name of the component to build
        bcond_config: Configuration dict with bcond settings and build cache identifier
        branch: Git branch to use (defaults to config value)
        target: Koji target to build for (optional)
        no_git_refresh: Skip git refresh if True
    
    Returns:
        bool: True if a scratchbuild was submitted, False if reusing existing artifact
    """
    repopath = pathlib.Path(CONFIG['cache_dir']['fedpkg']) / bcond_config['id']
    
    was_updated = refresh_or_clone(repopath, component_name, no_git_refresh=no_git_refresh, branch=branch)
    
    if check_existing_artifacts(repopath, was_updated, bcond_config):
        return False
    
    prepare_spec_for_build(component_name, repopath, bcond_config)
    
    bcond_config['koji_task_id'] = submit_scratchbuild(repopath, target=target)
    return True


def download_srpm_if_possible(bcond_config):
    """
    This will:
     1. inspect the bcond_config for srpm path and a koji build id
     2. if srpm exists or koji build doesn't, do nothing
     3. if koji build is closed, download the srpm and store the path in bcond_config
     4. return True if something was downloaded
    """
    if ('srpm' in bcond_config or
            'koji_task_id' not in bcond_config or
            koji_status(bcond_config['koji_task_id']) != KOJI_STATE_CLOSED):
        return False
    log(' • Downloading SRPM from Koji...', end=' ')
    repopath = pathlib.Path(CONFIG['cache_dir']['fedpkg']) / bcond_config['id']
    command = ('koji', 'download-task', bcond_config['koji_task_id'], '--arch=src', '--noprogress')
    koji_output = run(*command, cwd=repopath).stdout.splitlines()
    if (l := len(koji_output)) != 1:
        raise RuntimeError(f'Cannot parse koji download-task output, expected 1 line, got: {l}')
    srpm_filename = koji_output[0].split(' ')[-1]
    if not srpm_filename.endswith(SRPM_EXTENSION):
        raise RuntimeError(f'Cannot parse koji download-task output, expected a *{SRPM_EXTENSION} filename, got: {srpm_filename}')
    srpm = repopath / srpm_filename
    if not srpm.exists():
        raise RuntimeError(f'Downloaded SRPM does not exist: {srpm}')
    bcond_config['srpm'] = srpm
    log(srpm_filename)
    return True


def rpm_requires(rpm):
    """
    Returns a collection with Requires of given on-disk RPM package.
    If the package is a source package, those are BuildRequires.
    rpmlib() requires are filtered out.

    The result is a sorted, deduplicated tuple,
    so it can be hashed as an argument to other cached functions.
    """
    raw_requires = run('rpm', '-qp', '--requires', rpm).stdout.splitlines()
    return tuple(sorted({r for r in raw_requires if not r.startswith('rpmlib(')}))


def extract_buildrequires_if_possible(bcond_config):
    """
    This will:
     1. inspect the bcond_config for srpm path
     2. if srpm does not exist, do nothing
     3. add buildrequires of the found srpm to the bcond_config
     4. return True if srpm was found
    """
    if 'srpm' not in bcond_config:
        if srpm := srpm_path(pathlib.Path(CONFIG['cache_dir']['fedpkg']) / bcond_config['id']):
            bcond_config['srpm'] = srpm
        else:
            return False
    bcond_config['buildrequires'] = rpm_requires(bcond_config['srpm'])
    log(f' • Extracted {len(bcond_config["buildrequires"])} BuildRequires from {bcond_config["srpm"].name}')
    return True


def each_bcond_name_config():
    for component_name, bcond_configs in CONFIG['bconds'].items():
        for bcond_config in bcond_configs:
            bcond_config['id'] = bcond_cache_identifier(component_name, bcond_config)
            yield component_name, bcond_config


def build_reverse_id_lookup():
    for _ in each_bcond_name_config():
        pass


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-R', '--no-git-refresh',
        action='store_true',
        default=False,
        help="Don't refresh the gitrepo of each existing component, just send new components scratchbuilds and downloads srpms."
    )
    parser.add_argument(
        'packages',
        nargs='*',
        help='Only fetch bconds for given package name(s).'
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()

    # build everything
    something_was_submitted = False
    for component_name, bcond_config in each_bcond_name_config():
        if args.packages and component_name not in args.packages:
            continue
        something_was_submitted |= scratchbuild_patched_if_needed(component_name, bcond_config, no_git_refresh=args.no_git_refresh)

    # download everything until there's nothing downloaded
    # the idea is that while downloading, other tasks could finish
    something_was_downloaded = True  # bogus initial value to be able to start
    extracted_count = 0
    while something_was_downloaded:
        something_was_downloaded = False
        # while we were downloading, we could have finished Koji builds
        for pkg, bcond_configs in CONFIG['bconds'].items():
            if args.packages and pkg not in args.packages:
                continue
            for bcond_config in bcond_configs:
                if 'buildrequires' not in bcond_config:
                    something_was_downloaded |= download_srpm_if_possible(bcond_config)
                    if extract_buildrequires_if_possible(bcond_config):
                        extracted_count += 1
        koji_status.cache_clear()

    log(f'Extracted BuildRequires from {extracted_count} SRPMs.')
    if not_extracted_count := sum(len(bcond_configs) for bcond_configs in CONFIG['bconds'].values()) - extracted_count:
        sys.exit(f'{not_extracted_count} SRPMs remain to be built/downloaded/extracted, run this again in a while.')
