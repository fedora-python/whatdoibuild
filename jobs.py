import collections
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
    sack = rawhide_sack()
    log('• Querying all packages to rebuild...', end=' ')
    results = sack.query().filter(requires=old_deps, arch__neq='src', latest=1)
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
    sack = target_sack()
    log('• Querying all successfully rebuilt packages...', end=' ')
    results = sack.query().filter(requires=new_deps, arch__neq='src', latest=1)
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


def are_all_done(*, packages_to_check, all_components, components_done, blocker_counter, loop_detector, missing_packages):
    """
    Given a collection of (binary) packages_to_check, and dicts of all_components and components_done,
    returns True if ALL packages_to_check are considered "done" (i.e. installable).

    missing_packages maps component names to sets of missing package names.
    """
    relevant_components = ReverseLookupDict()
    for pkg in packages_to_check:
        relevant_components[all_components.key(pkg)].append(pkg)
    relevant_components.default_factory = None

    log(f'  • {component}: {len(packages_to_check)} packages / {len(relevant_components)} '
        f'components relevant to our problem')
    all_available = True
    blocking_components = set()
    for relevant_component, required_packages in relevant_components.items():
        log(f'    • {relevant_component}')
        count_component = False
        for required_package in required_packages:
            has_older = False
            for done_package in components_done.get(relevant_component, ()):
                found = False
                # The done packages are from different repo and might have different EVR
                # Hence, we only compare the names
                # For Copr rebuilds, the Copr EVR must be >= Fedora EVR
                # For koji rebuilds, this will be always true anyway
                if done_package.name == required_package.name:
                    # if not done_package.evr_lt(required_package):
                    if True:
                        log(f'      ✔ {required_package.name}')
                        break
                    else:
                        has_older = True
                else:
                    # Check the virtual provides - maybe one of them matches what we look for
                    for provide in done_package.provides:
                        if provide.name == required_package.name:
                            log(f'      ✔ {required_package.name}')
                            found = True
                            break
                    if found:
                        break
            else:
                if has_older:
                    log(f'      ✗ {required_package.name} (older EVR available)')
                else:
                    log(f'      ✗ {required_package.name}')

                missing_packages[component].add(required_package.name)

                all_available = False
                count_component = True
        if count_component:
            blocker_counter['general'][relevant_component] += 1
            blocking_components.add(relevant_component)
    if len(blocking_components) == 1:
        blocker_counter['single'][next(iter(blocking_components))] += 1
    elif 1 < len(blocking_components) < 10:  # this is an arbitrarily chosen number to avoid cruft
        blocker_counter['combinations'][tuple(sorted(blocking_components))] += 1
    loop_detector[component] = sorted(blocking_components)
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


if __name__ == '__main__':
    # this is spaghetti code that will be split into functions later:
    from resolve_buildroot import resolve_buildrequires_of, resolve_requires
    from bconds import bcond_cache_identifier, extract_buildrequires_if_possible

    components = packages_to_rebuild(tuple(CONFIG['deps']['old']), excluded_components=tuple(CONFIG['components']['excluded']))
    for component in CONFIG['components']['extra']:
        components[component] = []
    components_done = packages_built(tuple(CONFIG['deps']['new']), excluded_components=tuple(CONFIG['components']['excluded']))
    binary_rpms = components.all_values()

    blocker_counter = {
        'general': collections.Counter(),
        'single': collections.Counter(),
        'combinations': collections.Counter(),
    }
    loop_detector = {}

    missing_packages = collections.defaultdict(set)  # requiring_component -> missing packages

    for component in components:
        if len(sys.argv) > 1 and component not in sys.argv[1:]:
            continue

        try:
            component_buildroot = resolve_buildrequires_of(component)
        except ValueError as e:
            log(f'\n  ✗ {e}')
            number_of_resolved = None
            ready_to_rebuild = False
        else:
            number_of_resolved = len(component_buildroot)

            ready_to_rebuild = are_all_done(
                packages_to_check=set(component_buildroot) & binary_rpms,
                all_components=components,
                components_done=components_done,
                blocker_counter=blocker_counter,
                loop_detector=loop_detector,
                missing_packages=missing_packages,
            )

        if ready_to_rebuild:
            if os.environ.get('PRINT_ALL') or component not in components_done:
                print(component)
        elif component in CONFIG['bconds']:
            for bcond_config in CONFIG['bconds'][component]:
                bcond_config['id'] = bcond_cache_identifier(component, bcond_config)
                log(f'• {component} not ready and {bcond_config["id"]} bcond found, will check that one')
                if 'buildrequires' not in bcond_config:
                    extract_buildrequires_if_possible(bcond_config)
                if 'buildrequires' in bcond_config:
                    try:
                        component_buildroot = resolve_requires(tuple(sorted(bcond_config['buildrequires'])))
                    except ValueError as e:
                        log(f'\n  ✗ {e}')
                        continue
                    if number_of_resolved == len(component_buildroot):
                        # XXX when this happens, the bcond might be bogus
                        # figure out a way to present that information
                        pass
                    ready_to_rebuild = are_all_done(
                        packages_to_check=set(component_buildroot) & binary_rpms,
                        all_components=components,
                        components_done=components_done,
                        blocker_counter=blocker_counter,
                        loop_detector=loop_detector,
                        missing_packages=missing_packages,
                    )
                    if ready_to_rebuild:
                        if os.environ.get('PRINT_ALL') or component not in components_done:
                            print(bcond_config['id'])
                else:
                    log(f' • {bcond_config["id"]} bcond SRPM not present yet, skipping')

    log('\nThe 50 most commonly needed components are:')
    for component, count in blocker_counter['general'].most_common(50):
        status_info = get_component_status_info(component, missing_packages, components)
        log(f'{count:>5} {component:<35} {status_info}')

    log('\nThe 20 most commonly last-blocking components are:')
    for component, count in blocker_counter['single'].most_common(20):
        status_info = get_component_status_info(component, missing_packages, components)
        log(f'{count:>5} {component:<35} {status_info}')

    log('\nThe 20 most commonly last-blocking small combinations of components are:')
    for components, count in blocker_counter['combinations'].most_common(20):
        log(f'{count:>5} {", ".join(components)}')

    report_blocking_components(loop_detector)
