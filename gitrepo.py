"""
This module contains helper functions to manipulate with distgit repositories:
clone them, refresh their local copies and patch the specfiles.
"""

import re

from utils import CONFIG, log, run


def clone_into(component_name, target, branch=''):
    branch = branch or CONFIG['distgit']['branch']
    log(f' • Cloning {component_name} into "{target}"...', end=' ')
    # I would like to use --depth=1 but that breaks rpmautospec
    # https://pagure.io/fedora-infra/rpmautospec/issue/227
    run('fedpkg', 'clone', component_name, target, f'--branch={branch}')
    log('done.')


def refresh_gitrepo(repopath, prune_existing=False):
    log(f' • Refreshing "{repopath}" git repo...', end=' ')
    git = 'git', '-C', repopath
    head_before = run(*git, 'rev-parse', 'HEAD').stdout.rstrip()
    run(*git, 'stash')
    run(*git, 'reset', '--hard')
    run(*git, 'pull')
    head_after = run(*git, 'rev-parse', 'HEAD').stdout.rstrip()
    if head_before == head_after:
        if not prune_existing:
            # we try to preserve the changes for local inspection, but if it fails, meh
            run(*git, 'stash', 'pop', check=False)
        log('already up to date.')
        return False
    else:
        log(f'updated {head_before[:10]}..{head_after[:10]}.')
        return True


def validate_bcond_config(bcond_config):
    """
    Validate that bcond configuration doesn't have conflicting with/without.
    
    Args:
        bcond_config: Configuration dict with 'withs' and 'withouts' keys
    
    Raises:
        ValueError: If the same bcond appears in both 'withs' and 'withouts'
    """
    withs = set(bcond_config.get('withs', ()))
    withouts = set(bcond_config.get('withouts', ()))
    overlap = withs & withouts
    
    if overlap:
        raise ValueError(f'Cannot have the same with and without: {", ".join(sorted(overlap))}')


def generate_bcond_lines(bcond_config):
    """
    Generate RPM macro lines for bcond configuration.
    
    Args:
        bcond_config: Configuration dict with 'withs' and 'withouts' keys
    
    Returns:
        List of strings containing RPM macro definitions to prepend to spec
    """
    lines = []
    
    for without in sorted(bcond_config.get('withouts', ())):
        lines.append(f'%global _without_{without} 1')
    
    for with_ in sorted(bcond_config.get('withs', ())):
        lines.append(f'%global _with_{with_} 1')
    
    return lines


def apply_macro_replacements(spec_text, replacements):
    """
    Apply macro replacements to spec file text.
    
    Finds lines like '%define macro value' or '%global macro value' and
    replaces the value with the one specified in replacements.
    
    Args:
        spec_text: Original spec file text
        replacements: Dict mapping macro names to new values
    
    Returns:
        Modified spec file text with replacements applied
    """
    for macro, value in replacements.items():
        # Escape the macro name in case it contains special regex characters
        escaped_macro = re.escape(macro)
        spec_text = re.sub(
            fr'^(\s*)%(define|global)(\s+){escaped_macro}(\s+)\S.*$',
            fr'\1%\2\g<3>{macro}\g<4>{value}',
            spec_text,
            flags=re.MULTILINE
        )
    
    return spec_text


def patch_spec(specpath, bcond_config):
    """
    Patch a spec file with bcond configuration and macro replacements.
    
    This function:
    1. Resets any uncommitted changes in the repository
    2. Validates bcond configuration
    3. Generates bcond macro definitions to prepend
    4. Applies macro replacements in the spec file
    5. Writes the modified spec file
    
    Args:
        specpath: Path to the spec file to patch
        bcond_config: Configuration dict with:
            - 'withs': list of bconds to enable (optional)
            - 'withouts': list of bconds to disable (optional)
            - 'replacements': dict of macro names to values (optional)
    
    Raises:
        ValueError: If the same bcond appears in both withs and withouts
    """
    log(f'   • Patching {specpath.name}')
    
    run('git', '-C', specpath.parent, 'reset', '--hard')
    
    validate_bcond_config(bcond_config)
    
    spec_text = specpath.read_text()
    spec_text = apply_macro_replacements(spec_text, bcond_config.get('replacements', {}))
    
    bcond_lines = generate_bcond_lines(bcond_config)
    bcond_lines.append(spec_text)
    
    specpath.write_text('\n'.join(bcond_lines))


def refresh_or_clone(repopath, component_name, *, prune_existing=False, no_git_refresh=False, branch=''):
    """
    Returns True if there's new contents of the repository.
    Returns False if the content of the repository remains the same
    or if no_git_refresh option is set to True
    (we skip the repository update and assume nothing has changed).
    """
    if repopath.exists():
        if no_git_refresh:
            return False
        else:
            return refresh_gitrepo(repopath, prune_existing=prune_existing)
    else:
        repopath.parent.mkdir(exist_ok=True)
        clone_into(component_name, repopath, branch=branch)
        return True
