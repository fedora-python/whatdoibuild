#!/usr/bin/env python3
"""
Remove unused bcond entries from config.toml.

For packages not in todo.pkgs:
- "just bootstrap" packages: check koji for ~bootstrap builds since 2026-06-03
- Other packages: check distgit for Bootstrap/Rebuilt commits

Unused bconds are removed from config.toml in place.
"""

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time

import tomllib

def log(*args, **kwargs):
    kwargs.setdefault('file', sys.stderr)
    kwargs.setdefault('flush', True)
    return print(*args, **kwargs)


def load_todo_pkgs(config):
    path = config['todo']['path']
    max_age_minutes = config['todo']['max_age_minutes']
    mtime = os.path.getmtime(path)
    age_seconds = time.time() - mtime
    if age_seconds > max_age_minutes * 60:
        age_minutes = int(age_seconds / 60)
        sys.exit(f'{path} is {age_minutes} minutes old (> {max_age_minutes} min). Refresh it first.')

    with open(path) as f:
        return {line.strip() for line in f if line.strip()}


def is_just_bootstrap(entries):
    for entry in entries:
        if entry.get('withouts') or entry.get('replacements'):
            return False
        withs = entry.get('withs', [])
        if list(withs) != ['bootstrap']:
            return False
    return True


def remove_bcond_blocks(content, packages_to_remove):
    lines = content.splitlines(keepends=True)
    result = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if stripped.startswith('[[bconds.') and stripped.endswith(']]'):
            key = stripped[len('[[bconds.'):-len(']]')].strip()
            if key in packages_to_remove:
                i += 1
                # skip key-value lines
                while i < len(lines):
                    s = lines[i].strip()
                    if s and not s.startswith('#') and not s.startswith('['):
                        i += 1
                    else:
                        break
                # skip trailing blank lines
                while i < len(lines) and lines[i].strip() == '':
                    i += 1
                continue

        result.append(lines[i])
        i += 1

    return ''.join(result)


async def run_cmd(*cmd, cwd=None):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def check_distgit(pkg_name, tmpdir, config):
    clone_dir = os.path.join(tmpdir, pkg_name)
    clone_depth = config['distgit']['clone_depth']
    clone_retries = config['distgit']['clone_retries']

    for attempt in range(1, clone_retries + 1):
        rc, stdout, stderr = await run_cmd(
            'fedpkg', 'clone', '-a', pkg_name, clone_dir, '--', f'--depth={clone_depth}'
        )
        if rc == 0:
            break
        log(f'  WARNING: fedpkg clone failed for {pkg_name} (attempt {attempt}/{clone_retries}): {stderr.strip()}')
        if attempt < clone_retries:
            await asyncio.sleep(2 ** attempt)
            if os.path.isdir(clone_dir):
                shutil.rmtree(clone_dir)
        else:
            return False

    rc, stdout, stderr = await run_cmd(
        'git', '-C', clone_dir, 'log',
        f'--author={config["distgit"]["author"]}', '--format=%s', '--all',
    )
    if rc != 0:
        log(f'  WARNING: git log failed for {pkg_name}: {stderr.strip()}')
        return False

    bootstrap_msg = config['distgit']['bootstrap_commit_message']
    rebuild_msg = config['distgit']['commit_message']
    has_bootstrap = False
    has_rebuild = False
    for subject in stdout.splitlines():
        if subject == bootstrap_msg:
            has_bootstrap = True
        elif subject == rebuild_msg:
            has_rebuild = True

    # Unused: no bootstrap commit but was rebuilt directly
    return not has_bootstrap and has_rebuild


async def check_koji(pkg_name, config):
    builds_after = config['koji']['builds_after']
    bootstrap_marker = config['koji']['bootstrap_marker']
    rc, stdout, stderr = await run_cmd(
        'koji', 'list-builds', f'--package={pkg_name}', f'--after={builds_after}'
    )
    if rc != 0:
        log(f'  WARNING: koji list-builds failed for {pkg_name}: {stderr.strip()}')
        return False

    for line in stdout.splitlines():
        if bootstrap_marker in line:
            return False  # bootstrap build exists, was used

    return True  # no bootstrap builds found, unused


async def check_package(pkg_name, entries, tmpdir, config, sem):
    async with sem:
        just_bootstrap = is_just_bootstrap(entries)

        if just_bootstrap:
            log(f'  {pkg_name}: checking koji for ~bootstrap builds...')
            unused = await check_koji(pkg_name, config)
        else:
            log(f'  {pkg_name}: checking distgit for Bootstrap/Rebuilt commits...')
            unused = await check_distgit(pkg_name, tmpdir, config)

        status = 'UNUSED' if unused else 'used'
        log(f'  {pkg_name}: {status}')
        return pkg_name, unused


async def main():
    parser = argparse.ArgumentParser(description='Remove unused bconds from config.toml')
    parser.add_argument(
        '-j', '--jobs',
        type=int,
        default=os.cpu_count(),
        help=f'Max parallel tasks (default: {os.cpu_count()})',
    )
    args = parser.parse_args()

    with open('config.toml', 'rb') as f:
        config = tomllib.load(f)

    todo_pkgs = load_todo_pkgs(config)
    log(f'Loaded {len(todo_pkgs)} packages from {config["todo"]["path"]}')

    bconds = config['bconds']

    to_check = {}
    skipped_todo = 0
    for pkg_name in bconds:
        if pkg_name.strip() in todo_pkgs:
            skipped_todo += 1
        else:
            to_check[pkg_name] = bconds[pkg_name]

    log(f'Skipping {skipped_todo} packages in todo.pkgs')
    log(f'Checking {len(to_check)} packages with {args.jobs} parallel jobs...')

    sem = asyncio.Semaphore(args.jobs)
    to_remove = set()

    def collect_result(task):
        try:
            pkg_name, unused = task.result()
        except (asyncio.CancelledError, Exception):
            return
        if unused:
            to_remove.add(pkg_name)

    try:
        with tempfile.TemporaryDirectory(prefix='cleanup_bconds_') as tmpdir:
            tasks = []
            for pkg_name, entries in to_check.items():
                task = asyncio.ensure_future(check_package(
                    pkg_name.strip(), entries,
                    tmpdir, config, sem,
                ))
                task.add_done_callback(collect_result)
                tasks.append(task)
            await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log(f'\nInterrupted. {len(to_remove)} unused bconds found so far.')
    finally:
        if not to_remove:
            log('No unused bconds found.')
            return

        log(f'\nRemoving {len(to_remove)} unused bcond entries:')
        for pkg_name in sorted(to_remove):
            log(f'  - {pkg_name}')

        with open('config.toml') as f:
            content = f.read()
        content = remove_bcond_blocks(content, to_remove)
        with open('config.toml', 'w') as f:
            f.write(content)

        log(f'\nDone. Removed {len(to_remove)} entries from config.toml.')


if __name__ == '__main__':
    asyncio.run(main())
