"""
Calculate build order for arched packages based on inter-dependencies.

Dynamically discovers which packages need rebuilding, filters to arch-specific
ones, resolves their BuildRequires against the target repo, and topologically
sorts them. Identifies cycles and which bconds from config.toml break them.

Usage:
    python build_order.py
"""

import collections
import functools
import sys

import dnf
import hawkey

from bconds import bcond_cache_identifier, read_bconds_cache_if_exists
from jobs import initialize_component_data
from resolve_buildroot import buildrequires_of, mandatory_packages_in_groups
from utils import CONFIG, log


@functools.cache
def _target_with_sources_sack():
    """
    Create a sack with the target binary repo AND the rawhide source repo.

    The source repo is needed so that addfileprovides() (called internally by
    fill_sack) knows which file paths are required by SRPMs.  Without it, file
    provides like /usr/bin/sphinx-build are not indexed and resolution fails.
    """
    base = dnf.Base()
    conf = base.conf
    conf.arch = CONFIG['architectures']['repoquery']
    conf.cachedir = CONFIG['cache_dir']['dnf']
    conf.substitutions['releasever'] = 'rawhide'
    conf.substitutions['basearch'] = CONFIG['architectures']['repoquery']
    for repo in CONFIG['repos']['target']:
        base.repos.add_new_repo(conf=conf, skip_if_unavailable=False, **repo)
    for repo in CONFIG['repos']['rawhide']:
        if 'source' in repo.get('repoid', ''):
            base.repos.add_new_repo(conf=conf, skip_if_unavailable=False, **repo)
    log(f'• Filling the target+source sack to/from {CONFIG["cache_dir"]["dnf"]}...', end=' ')
    base.fill_sack(load_system_repo=False, load_available_repos=True)
    log('done.')
    return base.sack


@functools.cache
def resolve_requires_in_target(requires, ignore_weak_deps=True):
    """Resolve requirements against the target (side-tag) repo."""
    sack = _target_with_sources_sack()
    goal = hawkey.Goal(sack)
    orig_len = len(requires)
    requires += tuple(mandatory_packages_in_groups())
    log(f'• Resolving {orig_len} requirements in target...', end=' ')
    for dep in requires:
        selector = hawkey.Selector(sack).set(provides=dep)
        goal.install(select=selector)
    if not goal.run(ignore_weak_deps=ignore_weak_deps):
        # Find which deps are missing
        missing = []
        for dep in requires:
            q = sack.query().filter(provides=dep)
            if not q.run():
                missing.append(dep)
        raise ValueError(f'Cannot resolve in target: missing {", ".join(missing[:5])}')
    if goal.list_upgrades() or goal.list_erasures():
        raise RuntimeError('Got packages to upgrade or erase in target')
    log(f'to {len(goal.list_installs())} installs.')
    return goal.list_installs()


@functools.cache
def resolve_buildrequires_in_target(package_name, *, extra_requires=()):
    """Get BuildRequires from rawhide SRPM, resolve against target repo."""
    brs = buildrequires_of(package_name, extra_requires=extra_requires)
    return resolve_requires_in_target(brs)


def find_arched_packages(ctx):
    """
    From all components needing rebuild, find those that produce
    arch-specific (non-noarch) binary RPMs.
    """
    arched = set()
    for name, pkgs in ctx.components.items():
        if any(p.arch != 'noarch' for p in pkgs):
            arched.add(name)
    log(f'• Found {len(arched)} arched components out of {len(ctx.components)} total.')
    return arched


def _build_binary_to_source_map(ctx):
    """
    Build a reverse map from binary RPM name to source package name.
    Only includes binary packages that need rebuilding (carry old ABI).
    """
    binary_to_source = {}
    for src_name, pkgs in ctx.components.items():
        for p in pkgs:
            binary_to_source[p.name] = src_name
    return binary_to_source


def build_dependency_graph(arched_set, binary_to_source):
    """
    For each arched package, resolve its buildroot against the target repo
    and find which other arched packages provide ABI-relevant binary RPMs in it.

    Returns (graph, unresolvable) where graph maps package -> set of deps.
    Self-dependencies are included.
    """
    graph = {}
    unresolvable = set()

    for pkg in sorted(arched_set):
        log(f'\n• {pkg}...', end=' ')
        try:
            buildroot = resolve_buildrequires_in_target(pkg)
        except (ValueError, RuntimeError) as e:
            log(f'SKIP ({e})')
            unresolvable.add(pkg)
            continue

        deps = set()
        for p in buildroot:
            src = binary_to_source.get(p.name)
            if src and src in arched_set:
                deps.add(src)

        graph[pkg] = deps
        if deps - {pkg}:
            log(f'deps: {", ".join(sorted(deps - {pkg}))}')
        elif pkg in deps:
            log('self-dep only')
        else:
            log('no deps')

    return graph, unresolvable


def build_bcond_graph(packages, arched_set, binary_to_source):
    """
    For packages that have bconds with cached buildrequires, resolve the
    bcond buildroot against the target repo and find deps within the arched set.

    Returns dict mapping package -> set of deps when built with bcond.
    """
    bcond_deps = {}

    for pkg in sorted(packages):
        if pkg not in CONFIG.get('bconds', {}):
            continue

        for bcond_config in CONFIG['bconds'][pkg]:
            if 'buildrequires' not in bcond_config:
                continue

            bcond_id = bcond_config.get('id', bcond_cache_identifier(pkg, bcond_config))
            try:
                buildroot = resolve_requires_in_target(
                    tuple(sorted(bcond_config['buildrequires']))
                )
            except (ValueError, RuntimeError) as e:
                log(f'  bcond {bcond_id}: SKIP ({e})')
                continue

            deps = set()
            for p in buildroot:
                src = binary_to_source.get(p.name)
                if src and src in arched_set:
                    deps.add(src)

            bcond_deps[pkg] = deps
            break  # use first bcond config with buildrequires

    return bcond_deps


def tarjan_scc(graph):
    """Find strongly connected components using iterative Tarjan's algorithm."""
    index_counter = [0]
    stack = []
    lowlink = {}
    index = {}
    on_stack = set()
    result = []

    # Iterative version to avoid recursion limit issues
    def strongconnect(start):
        # (node, iterator_over_deps, is_root_call)
        call_stack = [(start, iter(graph.get(start, set()) & set(graph.keys())), True)]
        index[start] = lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)

        while call_stack:
            node, dep_iter, _ = call_stack[-1]
            pushed = False
            for dep in dep_iter:
                if dep not in index:
                    index[dep] = lowlink[dep] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(dep)
                    on_stack.add(dep)
                    call_stack.append((dep, iter(graph.get(dep, set()) & set(graph.keys())), True))
                    pushed = True
                    break
                elif dep in on_stack:
                    lowlink[node] = min(lowlink[node], index[dep])

            if not pushed:
                call_stack.pop()
                if lowlink[node] == index[node]:
                    component = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == node:
                            break
                    result.append(component)
                if call_stack:
                    parent = call_stack[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])

    for node in graph:
        if node not in index:
            strongconnect(node)

    return result


def find_cycles_and_bcond_breaks(graph, bcond_deps):
    """
    Find cycles (SCCs) and determine which bconds break them.

    Returns:
        bcond_breaks: dict mapping package -> set of deps removed by bcond
        stuck: set of packages in unbreakable cycles
    """
    sccs = tarjan_scc(graph)
    bcond_breaks = {}
    stuck = set()

    for scc in sccs:
        scc_set = set(scc)

        if len(scc) == 1:
            pkg = scc[0]
            if pkg not in graph.get(pkg, set()):
                continue  # no self-dep, not a cycle
            # self-dependency
            if pkg in bcond_deps and pkg not in bcond_deps[pkg]:
                bcond_breaks[pkg] = {pkg}
                log(f'Self-dep {pkg}: broken by bcond')
            elif pkg in CONFIG.get('bconds', {}):
                # bcond exists but no cached buildrequires or self-dep remains
                bcond_breaks[pkg] = {pkg}
                log(f'Self-dep {pkg}: assuming bcond breaks it (no cached BR to verify)')
            else:
                stuck.add(pkg)
                log(f'Self-dep {pkg}: NO bcond available!')
            continue

        log(f'\nCycle: {" -> ".join(scc + [scc[0]])}')

        cycle_broken = False
        for pkg in scc:
            if pkg not in bcond_deps:
                continue
            normal_in_scc = graph[pkg] & scc_set
            bcond_in_scc = bcond_deps[pkg] & scc_set
            removed = normal_in_scc - bcond_in_scc
            if removed:
                bcond_breaks[pkg] = removed
                log(f'  {pkg} bcond removes: {", ".join(sorted(removed))}')
                cycle_broken = True

        if not cycle_broken:
            # Try packages with bconds but no cached BRs
            for pkg in scc:
                if pkg in CONFIG.get('bconds', {}) and pkg not in bcond_deps:
                    normal_in_scc = graph[pkg] & scc_set
                    if normal_in_scc:
                        bcond_breaks[pkg] = normal_in_scc
                        log(f'  {pkg} bcond assumed to remove: {", ".join(sorted(normal_in_scc))} (no cached BR)')
                        cycle_broken = True
                        break

        if not cycle_broken:
            stuck.update(scc_set)
            log(f'  UNBREAKABLE cycle!')

    return bcond_breaks, stuck


def topological_sort_waves(graph, bcond_breaks, stuck):
    """
    Topological sort producing waves (layers of independent packages).

    Returns list of waves, where each wave is a list of (package, needs_bcond).
    """
    # Build modified graph with bcond-broken edges removed
    nodes = set(graph.keys()) - stuck
    modified = {}
    for pkg in nodes:
        deps = graph[pkg] & nodes
        if pkg in bcond_breaks:
            deps = deps - bcond_breaks[pkg]
        # Remove self-deps (already handled by bconds)
        deps.discard(pkg)
        modified[pkg] = deps

    # Kahn's algorithm producing waves
    in_degree = {pkg: len(modified[pkg]) for pkg in modified}
    reverse_graph = collections.defaultdict(set)
    for pkg, deps in modified.items():
        for dep in deps:
            reverse_graph[dep].add(pkg)

    waves = []
    remaining = set(modified.keys())

    while remaining:
        wave = sorted(pkg for pkg in remaining if in_degree[pkg] == 0)
        if not wave:
            # Remaining packages form unbreakable cycles
            log(f'WARNING: {len(remaining)} packages in unbreakable cycles during sort')
            stuck.update(remaining)
            break

        waves.append([(pkg, pkg in bcond_breaks) for pkg in wave])
        for pkg in wave:
            remaining.discard(pkg)
            for dependent in reverse_graph[pkg]:
                if dependent in remaining:
                    in_degree[dependent] -= 1

    return waves


def get_bcond_description(pkg):
    """Human-readable description of a package's bcond configuration."""
    if pkg not in CONFIG.get('bconds', {}):
        return '?'
    for bcond_config in CONFIG['bconds'][pkg]:
        parts = []
        if withs := bcond_config.get('withs'):
            parts.append('with ' + ','.join(withs))
        if withouts := bcond_config.get('withouts'):
            parts.append('without ' + ','.join(withouts))
        if replacements := bcond_config.get('replacements'):
            parts.append('replace ' + ','.join(f'{k}={v}' for k, v in replacements.items()))
        return '; '.join(parts)
    return '?'


def get_bcond_identifier(pkg):
    """Get the bcond cache identifier for a package."""
    if pkg not in CONFIG.get('bconds', {}):
        return None
    for bcond_config in CONFIG['bconds'][pkg]:
        return bcond_cache_identifier(pkg, bcond_config)
    return None


def main():
    read_bconds_cache_if_exists()

    log('\n=== Initializing component data ===')
    ctx = initialize_component_data()

    log('\n=== Finding arched packages ===')
    arched_set = find_arched_packages(ctx)

    log('\n=== Building binary-to-source map ===')
    binary_to_source = _build_binary_to_source_map(ctx)
    log(f'• Mapped {len(binary_to_source)} binary RPM names to source packages.')

    log('\n=== Building dependency graph (resolving against target) ===')
    graph, unresolvable = build_dependency_graph(arched_set, binary_to_source)

    # Remove unresolvable packages
    for pkg in unresolvable:
        graph.pop(pkg, None)
    for pkg in graph:
        graph[pkg] -= unresolvable

    # Find packages that participate in cycles and have bconds
    packages_needing_bcond_check = set()
    for pkg, deps in graph.items():
        if pkg in deps:  # self-dep
            packages_needing_bcond_check.add(pkg)
        for dep in deps:
            if dep in graph and pkg in graph.get(dep, set()):  # mutual dep
                packages_needing_bcond_check.add(pkg)
                packages_needing_bcond_check.add(dep)

    log('\n=== Resolving bcond dependencies (against target) ===')
    bcond_deps = build_bcond_graph(
        packages_needing_bcond_check | (arched_set & set(CONFIG.get('bconds', {}))),
        set(graph.keys()),
        binary_to_source,
    )

    log('\n=== Finding cycles and bcond breaks ===')
    bcond_breaks, stuck = find_cycles_and_bcond_breaks(graph, bcond_deps)

    log('\n=== Computing build order ===')
    waves = topological_sort_waves(graph, bcond_breaks, stuck)

    # Print results
    total_pkgs = sum(len(w) for w in waves)
    bcond_pkgs = sorted(bcond_breaks.keys())
    step = 0

    for i, wave in enumerate(waves, 1):
        print(f'\n=== WAVE {i} ({len(wave)} packages) ===')
        for pkg, needs_bcond in wave:
            step += 1
            if needs_bcond:
                desc = get_bcond_description(pkg)
                bid = get_bcond_identifier(pkg)
                print(f'  {step:>4}. {bid or pkg}  ({desc})')
            else:
                print(f'  {step:>4}. {pkg}')

    if bcond_pkgs:
        print(f'\n=== REBUILDS without bconds ({len(bcond_pkgs)} packages) ===')
        for pkg in bcond_pkgs:
            step += 1
            print(f'  {step:>4}. {pkg}')

    if stuck:
        print(f'\n=== UNRESOLVED CYCLES ({len(stuck)} packages) ===')
        for pkg in sorted(stuck):
            deps_in_stuck = sorted(graph.get(pkg, set()) & stuck)
            print(f'  {pkg} -> {", ".join(deps_in_stuck)}')

    if unresolvable:
        print(f'\n=== UNRESOLVABLE ({len(unresolvable)} packages) ===')
        for pkg in sorted(unresolvable):
            print(f'  {pkg}')

    print(f'\n=== SUMMARY ===')
    print(f'  Arched packages: {len(arched_set)}')
    print(f'  Ordered:         {total_pkgs} in {len(waves)} waves')
    print(f'  Bcond builds:    {len(bcond_pkgs)} (need rebuild without bcond later)')
    if stuck:
        print(f'  Stuck in cycles: {len(stuck)}')
    if unresolvable:
        print(f'  Unresolvable:    {len(unresolvable)}')


if __name__ == '__main__':
    main()
