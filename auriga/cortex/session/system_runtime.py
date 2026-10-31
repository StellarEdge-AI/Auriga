"""Publish explicit Linux runtime files without exposing installed applications."""

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


SHELL_COMMANDS = (
    'sh', 'bash', 'env', 'find', 'sed', 'awk', 'cat', 'head', 'tail', 'sort',
    'uniq', 'wc', 'ls', 'cp', 'mv', 'rm', 'mkdir', 'rmdir', 'tee', 'touch',
    'stat', 'readlink', 'realpath', 'sleep', 'printf', 'which', 'xargs', 'git',
    'cut', 'tr', 'basename', 'dirname', 'date', 'du', 'df', 'chmod', 'ln',
    'true', 'false', 'timeout', 'tar', 'gzip', 'gunzip', 'unzip', 'uname',
)


@lru_cache(maxsize=32)
def _library_mounts(objects):
    """Resolve dependencies of trusted runtime ELF objects, preserving aliases."""
    ldd = shutil.which('ldd', path=os.defpath)
    if not ldd:
        raise RuntimeError('runtime_dependency_inspection_requires_ldd')
    mounts = {}
    library_directories = os.pathsep.join(sorted({str(Path(path).resolve().parent) for path in objects}))
    for filename in objects:
        source = Path(filename).resolve(strict=True)
        with source.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue  # Explicit shell scripts execute the published shell.
        result = subprocess.run([ldd, str(source)], capture_output=True, text=True,
                                timeout=10, env={'PATH': os.defpath, 'LC_ALL': 'C',
                                                 'LD_LIBRARY_PATH': library_directories})
        output = result.stdout + result.stderr
        if '=> not found' in output:
            raise RuntimeError('runtime_shared_library_dependency_missing')
        if result.returncode and 'not a dynamic executable' not in output and 'statically linked' not in output:
            raise RuntimeError('runtime_shared_library_inspection_failed')
        for line in output.splitlines():
            match = re.search(r'(/.+?)\s+\(0x[0-9a-fA-F]+\)\s*$', line)
            if not match:
                continue
            destination = Path(match.group(1))
            library = destination.resolve(strict=True)
            if not library.is_file():
                raise RuntimeError('runtime_shared_library_must_be_regular_file')
            mounts[str(destination)] = library
            mounts[str(library)] = library
    return tuple((source, Path(destination)) for destination, source in sorted(mounts.items()))


def runtime_library_paths(objects):
    """Exact host files needed by the selected interpreter and extensions."""
    filenames = tuple(sorted({str(Path(path).resolve(strict=True)) for path in objects}))
    return tuple(sorted({source for source, _ in _library_mounts(filenames)}))


def runtime_library_mounts(objects):
    """Dependency mounts including the dynamic loader's original path aliases."""
    filenames = tuple(sorted({str(Path(path).resolve(strict=True)) for path in objects}))
    return _library_mounts(filenames)


@lru_cache(maxsize=8)
def system_library_path(soname):
    """Resolve one explicitly requested system library, without a directory grant."""
    ldconfig = shutil.which('ldconfig', path=os.defpath + ':/sbin:/usr/sbin')
    if not ldconfig:
        raise RuntimeError('runtime_library_discovery_requires_ldconfig')
    result = subprocess.run([ldconfig, '-p'], capture_output=True, text=True,
                            timeout=10, check=True, env={'PATH': os.defpath, 'LC_ALL': 'C'})
    for line in result.stdout.splitlines():
        name = line.strip().split(None, 1)
        before, separator, target = line.partition('=>')
        if name and name[0] == soname and separator:
            path = Path(target.strip()).resolve(strict=True)
            if path.is_file():
                return path
    raise RuntimeError('runtime_requested_system_library_missing')


def stdlib_runtime_entries(stdlib, module_names):
    """Select standard-library namespaces; omit application install locations."""
    stdlib = Path(stdlib).resolve(strict=True)
    for entry in sorted(stdlib.iterdir()):
        if entry.name in {'site-packages', 'dist-packages', 'sitecustomize.py', 'usercustomize.py'}:
            continue
        if entry.name == 'lib-dynload':
            children = [path for path in entry.iterdir() if path.name.split('.')[0] in module_names
                        and '.so' in path.suffixes and path.is_file()]
        elif entry.is_dir() and entry.name in module_names:
            children = [entry]
        elif entry.suffix == '.py' and entry.stem in module_names:
            children = [entry]
        else:
            continue
        for path in children:
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(stdlib):
                raise RuntimeError('runtime_stdlib_external_symlink')
            if path.is_dir() and any(item.is_symlink() and not item.resolve(strict=True).is_relative_to(path)
                                     for item in path.rglob('*')):
                raise RuntimeError('runtime_stdlib_external_symlink')
            yield path


@lru_cache(maxsize=1)
def system_runtime_mounts():
    """Return (source, destination) mounts for shell and system Python runtime.

    Directories are limited to actual standard-library package namespaces.
    Executables and their shared libraries are published as individual files.
    No /usr, /usr/local, library root or site/dist-packages is included.
    """
    python = shutil.which('python3', path=os.defpath)
    if not python:
        raise RuntimeError('system_runtime_python3_missing')
    probe = subprocess.run([python, '-I', '-S', '-c',
        'import json,sys,sysconfig; print(json.dumps([sysconfig.get_path("stdlib"),sorted(sys.stdlib_module_names|{sysconfig._get_sysconfigdata_name()})]))'],
        capture_output=True, text=True, timeout=10, check=True)
    stdlib, module_names = json.loads(probe.stdout)
    mounts, objects = {}, set()

    def add(source, destination):
        original = Path(source)
        resolved = original.resolve(strict=True)
        mounts[str(destination)] = resolved
        mounts[str(resolved)] = resolved
        if resolved.is_file():
            objects.add(str(resolved))
        elif resolved.is_dir():
            objects.update(str(path.resolve()) for path in resolved.rglob('*.so') if path.is_file())

    for name in (*SHELL_COMMANDS, 'python3'):
        executable = shutil.which(name, path=os.defpath)
        if executable:
            add(executable, Path('/usr/bin') / name)
        elif name in {'sh', 'bash', 'env', 'python3'}:
            raise RuntimeError('system_runtime_required_command_missing: ' + name)
    for entry in stdlib_runtime_entries(stdlib, set(module_names)):
        add(entry, entry)
    # Older manylinux wheels use separate glibc compatibility SONAMEs which
    # no longer appear in ldd output for current shell/Python executables.
    for soname in ('libc.so.6', 'libm.so.6', 'libpthread.so.0', 'librt.so.1',
                   'libdl.so.2', 'libutil.so.1', 'libgcc_s.so.1', 'libstdc++.so.6'):
        library = system_library_path(soname)
        add(library, library.parent / soname)
    for source, destination in _library_mounts(tuple(sorted(objects))):
        mounts[str(destination)] = source
    return tuple((source, Path(destination)) for destination, source in sorted(mounts.items()))
