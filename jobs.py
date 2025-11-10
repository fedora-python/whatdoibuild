import collections
from dataclasses import dataclass, field
import functools
import os
import sys

from sacks import MULTILIB, rawhide_sack, target_sack
from utils import CONFIG, log


class ReverseLookupDict(collections.defaultdict):
    """
    An enhanced defaultdict(list) that can reverse-lookup the keys for given items.
    Unique values in the lists are assumed but not checked.

    Use it like a regular dict, but lookup a key by the key(item) method.
    Use the all_values() method to get a set of all items in all keys at once.

    The lookup is internally cached in a reversed dictionary.

    In our code, we use this with lists of hawkey.Packages,
    but should work with any hashable values.
    """
    def __init__(self):
        super().__init__(list)
        self._reverse_lookup_cache = {}

    def key(self, value):
        if value in self._reverse_lookup_cache:
            return self._reverse_lookup_cache[value]
        for candidate_key, lst in self.items():
            if value in lst:
                self._reverse_lookup_cache[value] = candidate_key
                return candidate_key
        raise KeyError(f'Value {value!r} found in no list in this dict.')

    def all_values(self):
        return {value for lst in self.values() for value in lst}


@dataclass
class RebuildContext:
    """
    Context object holding all shared data and state for rebuild analysis.
    
    Attributes:
        components: All components that need rebuilding (name -> list of packages)
        components_done: Components that have been rebuilt (name -> list of packages)
        binary_rpms: Set of all binary RPM packages to rebuild
        blocker_counter: Statistics about blocking components
        loop_detector: Map of components to their blocking components
        missing_packages: Map of components to their missing package names
    """
    components: ReverseLookupDict
    components_done: ReverseLookupDict
    binary_rpms: set
    blocker_counter: dict = field(default_factory=lambda: {
        'general': collections.Counter(),
        'single': collections.Counter(),
        'combinations': collections.Counter(),
    })
    loop_detector: dict = field(default_factory=dict)
    missing_packages: dict = field(default_factory=lambda: collections.defaultdict(set))


def _query_packages_by_deps(sack_getter, deps, excluded_components):
    """
    Common logic for querying packages by dependencies.
    
    Args:
        sack_getter: Function that returns a DNF sack (e.g., rawhide_sack or target_sack)
        deps: Hashable collection of string-dependencies to query
        excluded_components: Hashable collection of component names to exclude
    
    Returns:
        ReverseLookupDict with component names as keys and lists of hawkey.Packages as values
    """
    sack = sack_getter()
    results = sack.query().filter(requires=deps, arch__neq='src', latest=1)
    if CONFIG['architectures']['repoquery'] in MULTILIB:
        results = results.filter(arch__neq=MULTILIB[CONFIG['architectures']['repoquery']])
    components = ReverseLookupDict()
    anticount = 0
    for result in results:
        if result.source_name not in excluded_components:
            components[result.source_name].append(result)
        else:
            anticount += 1
    # no longer create lists on access to avoid mistakes:
    components.default_factory = None
    log(f'found {len(components)} components ({len(results)-anticount} binary packages).')
    return components


@functools.lru_cache(maxsize=1)
def packages_to_rebuild(old_deps, *, excluded_components=()):
    """
    Given a hashable collection of string-dependencies that are "old",
    queries rawhide for all binary packages that require those
    and returns them in a dict:
     - keys: SRPM-names
     - values: lists of hawkey.Packages

    Excluded_components is an optional hashable collection of component names
    to exclude from the results.

    If rawhide does not contain our newly rebuilt packages (which is expected here),
    the dict will also contain packages that already successfully rebuilt
    (in our side tag or copr, etc.).
    """
    log('• Querying all packages to rebuild...', end=' ')
    return _query_packages_by_deps(
        rawhide_sack,
        old_deps,
        excluded_components,
    )


@functools.lru_cache(maxsize=1)
def packages_built(new_deps, *, excluded_components=()):
    """
    Given a hashable collection of string-dependencies that are "new",
    queries target for all binary packages that require those
    and returns them in a dict:
     - keys: SRPM-names
     - values: lists of hawkey.Packages

    Excluded_components is an optional hashable collection of component names
    to exclude from the results.
    """
    log('• Querying all successfully rebuilt packages...', end=' ')
    return _query_packages_by_deps(
        target_sack,
        new_deps,
        excluded_components,
    )


def _group_packages_by_component(packages_to_check, all_components):
    """
    Group packages by their source component.
    
    Args:
        packages_to_check: Collection of binary packages to check
        all_components: ReverseLookupDict mapping components to packages
    
    Returns:
        ReverseLookupDict with component names as keys and lists of packages as values
    """
    relevant_components = ReverseLookupDict()
    for pkg in packages_to_check:
        relevant_components[all_components.key(pkg)].append(pkg)
    relevant_components.default_factory = None
    return relevant_components


def _is_package_available(required_package, done_packages):
    """
    Check if a required package is available in the list of done packages.
    
    The done packages are from a different repo and might have different EVR.
    We compare by name or by virtual provides.
    
    Args:
        required_package: The package we need
        done_packages: List of packages that have been built
    
    Returns:
        Tuple of (is_available, has_older_version)
    """
    has_older = False
    for done_package in done_packages:
        # The done packages are from different repo and might have different EVR
        # Hence, we only compare the names
        # For Copr rebuilds, the Copr EVR must be >= Fedora EVR
        # For koji rebuilds, this will be always true anyway
        if done_package.name == required_package.name:
            # if not done_package.evr_lt(required_package):
            if True:
                return True, False
            else:
                has_older = True
        else:
            # Check the virtual provides - maybe one of them matches what we look for
            for provide in done_package.provides:
                if provide.name == required_package.name:
                    return True, False
    
    return False, has_older


def _update_blocker_statistics(blocking_components, blocker_counter, loop_detector, component):
    """
    Update blocker statistics based on which components are blocking.
    
    Args:
        blocking_components: Set of component names that are blocking
        blocker_counter: Dict with 'general', 'single', and 'combinations' counters
        loop_detector: Dict mapping components to their blocking components
        component: The component being checked
    """
    if len(blocking_components) == 1:
        blocker_counter['single'][next(iter(blocking_components))] += 1
    elif 1 < len(blocking_components) < 10:  # this is an arbitrarily chosen number to avoid cruft
        blocker_counter['combinations'][tuple(sorted(blocking_components))] += 1
    loop_detector[component] = sorted(blocking_components)


def are_all_done(component, packages_to_check, ctx):
    """
    Given a component name and a collection of (binary) packages_to_check,
    check if ALL packages are considered "done" (i.e. installable).
    
    Args:
        component: Name of the component being checked
        packages_to_check: Collection of binary packages to verify
        ctx: RebuildContext with shared data and state
    
    Returns:
        bool: True if ALL packages_to_check are considered "done" (i.e. installable).
    """
    relevant_components = _group_packages_by_component(packages_to_check, ctx.components)

    log(f'  • {component}: {len(packages_to_check)} packages / {len(relevant_components)} '
        f'components relevant to our problem')
    
    all_available = True
    blocking_components = set()
    
    for relevant_component, required_packages in relevant_components.items():
        log(f'    • {relevant_component}')
        component_is_blocking = False
        
        for required_package in required_packages:
            is_available, has_older = _is_package_available(
                required_package,
                ctx.components_done.get(relevant_component, ())
            )
            
            if is_available:
                log(f'      ✔ {required_package.name}')
            else:
                if has_older:
                    log(f'      ✗ {required_package.name} (older EVR available)')
                else:
                    log(f'      ✗ {required_package.name}')
                
                ctx.missing_packages[component].add(required_package.name)
                all_available = False
                component_is_blocking = True
        
        if component_is_blocking:
            ctx.blocker_counter['general'][relevant_component] += 1
            blocking_components.add(relevant_component)
    
    _update_blocker_statistics(blocking_components, ctx.blocker_counter, ctx.loop_detector, component)
    return all_available


def _sort_loop(loop):
    index = loop.index(min(loop))
    return tuple(loop[index:] + loop[:index+1])


def _detect_loop(loop_detector, probed_component, depchain, loops, seen):
    for component in loop_detector[probed_component]:
        recursedown = component not in seen
        seen.add(component)
        if component in CONFIG['bconds']:
            # we assume bconds are manually crafted not to have loops
            continue
        if loop_detector.get(component, []) == []:
            continue
        if component in depchain:
            loops.add(_sort_loop(depchain[depchain.index(component):]))
            continue
        if recursedown:
            _detect_loop(loop_detector, component, depchain + [component], loops, seen)

def report_blocking_components(loop_detector):
    loops = set()
    seen = set()
    for component in loop_detector:
        if component not in seen:
            _detect_loop(loop_detector, component, [component], loops, seen)
            seen.add(component)
    log('\nDetected dependency loops:')
    for loop in sorted(loops, key=lambda t: -len(t)):
        log('    • ' + ' → '.join(loop))


def get_component_status_info(component, missing_packages, components):
    """Generate status information for a component explaining why it's blocked."""
    if component in components:
        if component in missing_packages:
            missing_deps = missing_packages[component]
            if missing_deps:
                missing_deps_str = ", ".join(sorted(missing_deps)[:3])
                if len(missing_deps) > 3:
                    missing_deps_str += "..."
                return f" (blocked by: {missing_deps_str})"
            else:
                return " (blocked for unknown reason)"  # This should never happen, but keep it for debugging
        else:
            return " (ready)"  # This should never happen, but keep it for debugging
    else:
        return f" (build failed)"


def initialize_component_data():
    """
    Load and prepare all component data needed for rebuild analysis.
    
    Returns:
        RebuildContext containing all component data and state
    """
    components = packages_to_rebuild(
        tuple(CONFIG['deps']['old']),
        excluded_components=tuple(CONFIG['components']['excluded'])
    )
    for component in CONFIG['components']['extra']:
        components[component] = []
    
    components_done = packages_built(
        tuple(CONFIG['deps']['new']),
        excluded_components=tuple(CONFIG['components']['excluded'])
    )
    
    binary_rpms = components.all_values()
    
    return RebuildContext(
        components=components,
        components_done=components_done,
        binary_rpms=binary_rpms
    )


def check_regular_build(component, ctx):
    """
    Check if a component's regular (non-bconded) build is ready.
    
    Args:
        component: Name of the component to check
        ctx: RebuildContext with shared data and state
    
    Returns:
        Tuple of (ready_to_rebuild, number_of_resolved)
        - ready_to_rebuild: bool indicating if component can be rebuilt
        - number_of_resolved: int count of resolved packages, or None if resolution failed
    """
    from resolve_buildroot import resolve_buildrequires_of
    
    try:
        component_buildroot = resolve_buildrequires_of(component)
    except ValueError as e:
        log(f'\n  ✗ {e}')
        return False, None
    
    number_of_resolved = len(component_buildroot)
    ready_to_rebuild = are_all_done(
        component,
        set(component_buildroot) & ctx.binary_rpms,
        ctx
    )
    
    return ready_to_rebuild, number_of_resolved


def check_bcond_build(component, bcond_config, number_of_resolved, ctx):
    """
    Check if a component's bcond build is ready.
    
    Args:
        component: Name of the component to check
        bcond_config: Bcond configuration dictionary
        number_of_resolved: Number of packages in regular build (for comparison)
        ctx: RebuildContext with shared data and state
    
    Returns:
        bool indicating if the bcond build can be rebuilt
    """
    from resolve_buildroot import resolve_requires
    from bconds import extract_buildrequires_if_possible
    
    if 'buildrequires' not in bcond_config:
        extract_buildrequires_if_possible(bcond_config)
    
    if 'buildrequires' not in bcond_config:
        log(f' • {bcond_config["id"]} bcond SRPM not present yet, skipping')
        return False
    
    try:
        component_buildroot = resolve_requires(tuple(sorted(bcond_config['buildrequires'])))
    except ValueError as e:
        log(f'\n  ✗ {e}')
        return False
    
    if number_of_resolved == len(component_buildroot):
        # XXX when this happens, the bcond might be bogus
        # figure out a way to present that information
        pass
    
    ready_to_rebuild = are_all_done(
        component,
        set(component_buildroot) & ctx.binary_rpms,
        ctx
    )
    
    return ready_to_rebuild


def check_bcond_builds(component, number_of_resolved, ctx):
    """
    Check all bcond builds for a component that isn't ready for regular build.
    
    Args:
        component: Name of the component to check
        number_of_resolved: Number of packages in regular build (for comparison)
        ctx: RebuildContext with shared data and state
    
    Prints any bcond build identifiers that are ready to rebuild.
    """
    from bconds import bcond_cache_identifier
    
    if component not in CONFIG['bconds']:
        return
    
    for bcond_config in CONFIG['bconds'][component]:
        bcond_config['id'] = bcond_cache_identifier(component, bcond_config)
        log(f'• {component} not ready and {bcond_config["id"]} bcond found, will check that one')
        
        ready_to_rebuild = check_bcond_build(component, bcond_config, number_of_resolved, ctx)
        
        if ready_to_rebuild:
            if should_print_component(component, ctx.components_done):
                print(bcond_config['id'])


def should_print_component(component, components_done):
    """
    Determine if a component should be printed based on PRINT_ALL env var.
    
    Returns:
        bool indicating if component should be printed
    """
    return os.environ.get('PRINT_ALL') or component not in components_done


def process_component(component, ctx):
    """
    Process a single component: check regular build and bcond builds if needed.
    
    Args:
        component: Name of the component to process
        ctx: RebuildContext with shared data and state
    
    Prints the component or bcond identifier if ready to rebuild.
    """
    ready_to_rebuild, number_of_resolved = check_regular_build(component, ctx)
    
    if ready_to_rebuild:
        if should_print_component(component, ctx.components_done):
            print(component)
    else:
        check_bcond_builds(component, number_of_resolved, ctx)


def generate_reports(ctx):
    """
    Generate and print all summary reports.
    
    Args:
        ctx: RebuildContext with statistics and component data
    """
    log('\nThe 50 most commonly needed components are:')
    for component, count in ctx.blocker_counter['general'].most_common(50):
        status_info = get_component_status_info(component, ctx.missing_packages, ctx.components)
        log(f'{count:>5} {component:<35} {status_info}')
    
    log('\nThe 20 most commonly last-blocking components are:')
    for component, count in ctx.blocker_counter['single'].most_common(20):
        status_info = get_component_status_info(component, ctx.missing_packages, ctx.components)
        log(f'{count:>5} {component:<35} {status_info}')
    
    log('\nThe 20 most commonly last-blocking small combinations of components are:')
    for components_tuple, count in ctx.blocker_counter['combinations'].most_common(20):
        log(f'{count:>5} {", ".join(components_tuple)}')
    
    report_blocking_components(ctx.loop_detector)


def main():
    """
    Main entry point for rebuild analysis.
    
    Analyzes which components are ready to rebuild based on dependency resolution.
    Prints ready components to stdout and detailed reports to stderr.
    """
    ctx = initialize_component_data()
    
    # Filter components based on command line arguments
    components_to_process = ctx.components
    if len(sys.argv) > 1:
        components_to_process = {
            comp: ctx.components[comp]
            for comp in ctx.components
            if comp in sys.argv[1:]
        }
    
    for component in components_to_process:
        process_component(component, ctx)
    
    generate_reports(ctx)


if __name__ == '__main__':
    main()
