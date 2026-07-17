import pathlib
import sys

from bconds import reverse_id_lookup, build_reverse_id_lookup
from gitrepo import patch_spec, refresh_or_clone
from utils import CONFIG, run, log


PATCHDIR = pathlib.Path('patches_dir')
FEDPKG_CACHEDIR = pathlib.Path(CONFIG['cache_dir']['fedpkg'])
SPEC_EXTENSION = '.spec'


def parse_component_argument(component_arg):
    """
    Parse component name and optional bcond configuration from argument.
    
    Args:
        component_arg: Either a plain component name or bcond identifier
    
    Returns:
        Tuple of (component_name, bcond_config or None)
    """
    if ':' not in component_arg:
        return component_arg, None
    
    build_reverse_id_lookup()
    bcond_config = reverse_id_lookup[component_arg]
    component_name = component_arg.partition(':')[0]
    return component_name, bcond_config


def has_bootstrap_bcond(bcond_config):
    """Check if bcond config includes --with bootstrap."""
    return bcond_config is not None and 'bootstrap' in bcond_config.get('withs', [])


def strip_bootstrap_bcond(bcond_config):
    """Return a copy of bcond_config with bootstrap removed from withs."""
    config = dict(bcond_config)
    withs = [w for w in config.get('withs', []) if w != 'bootstrap']
    if withs:
        config['withs'] = withs
    else:
        config.pop('withs', None)
    return config


def needs_spec_changes(bcond_config):
    """Check if bcond config requires spec file modifications."""
    if bcond_config is None:
        return False
    return bool(
        bcond_config.get('withs')
        or bcond_config.get('withouts')
        or bcond_config.get('replacements')
    )


def get_patch_path(component_name):
    """Get the path to a component's patch file."""
    return PATCHDIR / f'{component_name}.patch'


def get_spec_path(repopath, component_name):
    """Get the path to a component's spec file."""
    return repopath / f'{component_name}{SPEC_EXTENSION}'


def revert_existing_patch(repopath, patch_path):
    """
    Revert an existing patch if it exists.
    
    Args:
        repopath: Path to the repository
        patch_path: Path to the patch file
    
    Raises:
        NotImplementedError: If trying to apply double bootstrap
    """
    if not patch_path.exists():
        return
    
    with patch_path.open('r') as patchfile:
        run('patch', '-R', '-p1', stdin=patchfile, cwd=repopath)
    patch_path.unlink()


def prepare_bootstrap_build(repopath, component_name, specpath, bootstrap):
    """
    Prepare a bootstrap build by patching spec and saving diff.
    
    Args:
        repopath: Path to the repository
        component_name: Name of the component
        specpath: Path to the spec file
        bootstrap: Bootstrap configuration
    
    Returns:
        str: Commit message for bootstrap build
    """
    message = CONFIG['distgit']['bootstrap_commit_message']
    patch_spec(specpath, bootstrap)
    diff = run('git', '-C', repopath, 'diff').stdout
    patch_path = get_patch_path(component_name)
    patch_path.write_text(diff)
    return message


def commit_and_push_changes(repopath, component_name, specpath, message):
    """
    Bump spec, commit changes, and push to remote.
    
    Args:
        repopath: Path to the repository
        component_name: Name of the component
        specpath: Path to the spec file
        message: Commit message
    """
    run('rpmdev-bumpspec', '-c', message, '--userstring', CONFIG['distgit']['author'], specpath)
    run('git', '-C', repopath, 'commit', '--allow-empty', 
        f'{component_name}{SPEC_EXTENSION}', '-m', message, 
        '--author', CONFIG['distgit']['author'])
    # raise NotImplementedError('no pushing yet')
    run('git', '-C', repopath, 'push')


def submit_koji_build(repopath, target=None):
    """
    Submit a Koji build for the component.
    
    Args:
        repopath: Path to the repository
        target: Koji target to build for (defaults to config value)
    """
    target = target or CONFIG['koji']['target']
    result = run('fedpkg', 'build', '--fail-fast', '--nowait',
        '--target', target, cwd=repopath)  # XXX '--background', 
    log(result.stdout)


def build_component(component_arg):
    """
    Build a component with optional bcond configuration.
    
    When --with bootstrap is in the bcond config, it is handled by submitting
    to a dedicated Koji bootstrap target instead of patching the spec file.
    Any other bconds (withouts, replacements, non-bootstrap withs) are still
    patched into the spec as before.
    
    Args:
        component_arg: Component name or bcond identifier (name:config)
    
    Raises:
        NotImplementedError: If double spec-level bcond patching is attempted
    """
    component_name, bcond_config = parse_component_argument(component_arg)
    repopath = FEDPKG_CACHEDIR / component_name
    
    refresh_or_clone(repopath, component_name, prune_existing=True)
    
    specpath = get_spec_path(repopath, component_name)
    patch_path = get_patch_path(component_name)
    
    use_bootstrap_target = has_bootstrap_bcond(bcond_config)
    spec_bcond_config = strip_bootstrap_bcond(bcond_config) if use_bootstrap_target else bcond_config
    modify_spec = needs_spec_changes(spec_bcond_config)
    
    # Handle existing patches from previous builds
    if patch_path.exists():
        if modify_spec:
            raise NotImplementedError('Double spec modify is not supported')
        revert_existing_patch(repopath, patch_path)
    
    if modify_spec:
        message = prepare_bootstrap_build(repopath, component_name, specpath, spec_bcond_config)
    else:
        message = CONFIG['distgit']['commit_message']
    
    # Bump and commit only if we haven't already, XXX ability to force this
    head_commit_msg = run('git', '-C', repopath, 'log', '--format=%B', '-n1', 'HEAD').stdout.rstrip()
    if modify_spec or head_commit_msg != message:
        commit_and_push_changes(repopath, component_name, specpath, message)
    
    target = CONFIG['koji']['bootstrap_target'] if use_bootstrap_target else None
    submit_koji_build(repopath, target=target)


def main():
    """Main entry point for building components."""
    if len(sys.argv) != 2:
        sys.exit('Usage: build.py <component_name|bcond_identifier>')
    
    try:
        build_component(sys.argv[1])
    except Exception:
        print(sys.argv[1])
        raise


if __name__ == '__main__':
    main()
