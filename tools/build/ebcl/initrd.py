#!/usr/bin/env python
""" EBcL initrd generator. """
import argparse
import glob
import logging
import os
import queue
import shutil
import tempfile

from io import BufferedWriter
from pathlib import Path
from typing import Tuple, Any, Optional

from jinja2 import Template

from .apt import Apt, parse_depends
from .config import load_yaml
from .fake import Fake
from .proxy import Proxy
from .version import VersionDepends


class InitrdGenerator:
    """ EBcL initrd generator. """

    # TODO: use files helper

    # config file
    config: Path
    # config values
    modules: list[str]
    modules_packages: list[VersionDepends]
    root_device: str
    devices: list[dict[str, str]]
    files: list[dict[str, Any]]
    kversion: Optional[str]
    arch: str
    template: Optional[str]
    modules_folder: Optional[str]
    # use fakeroot or sudo
    fakeroot: bool
    # name of busybox package
    busybox: list[VersionDepends]
    # out and tmp folders
    target_dir: str
    image_path: str
    # proxy
    proxy: Proxy
    # fakeroot helper
    fake: Fake

    def __init__(self, config_file: str):
        """ Parse the yaml config file.

        Args:
            config_file (Path): Path to the yaml config file.
        """
        config = load_yaml(config_file)

        self.config = Path(config_file)

        self.modules = config.get('modules', [])
        self.root_device = config.get('root_device', '')
        self.devices = config.get('devices', [])
        self.files = config.get('files', [])
        self.kversion = config.get('kversion', '')
        self.arch = config.get('arch', 'arm64')
        self.apt_repos = config.get('apt_repos', None)
        self.template = config.get('template', None)
        self.fakeroot = config.get('fakeroot', False)
        self.modules_folder = config.get('modules_folder', None)

        self.modules_packages = []
        modules_packages = config.get('modules_packages', '')
        for package in modules_packages:
            vds = parse_depends(package, self.arch)
            if vds:
                # TODO: handle alternatives
                self.modules_packages.append(vds[0])
            else:
                logging.error('Parsing of package %s failed!', package)

        busybox = config.get('busybox', 'busybox-static')
        vds = parse_depends(busybox, self.arch)
        if vds:
            logging.info('Busybox packages: %s', vds)
            self.busybox = vds
        else:
            logging.critical('Parsing of busybox %s failed!', busybox)
            exit(1)

        kernel = config.get('kernel', None)
        if kernel:
            vds = parse_depends(kernel, self.arch)
            if vds:
                logging.info('Kernel package: %s', vds[0])
                # TODO: handle alternatives
                self.modules_packages.append(vds[0])
            else:
                logging.error('Parsing of kernel %s failed!', kernel)

        self.proxy = Proxy()
        if self.apt_repos is None:
            self.proxy.add_apt(
                Apt(
                    url='https://linux.elektrobit.com/eb-corbos-linux/1.2',
                    distro='ebcl',
                    components=['prod', 'dev'],
                    arch=self.arch
                )
            )
        else:
            for repo in self.apt_repos:
                self.proxy.add_apt(
                    Apt(
                        url=repo['apt_repo'],
                        distro=repo['distro'],
                        components=repo['components'],
                        arch=self.arch
                    )
                )

        self.fake = Fake()

    def _run_chroot(self, cmd: str) -> Tuple[str, str, int]:
        """ Run command in chroot target environment. """
        if self.fakeroot:
            return self.fake.run_chroot(cmd, self.target_dir)
        else:
            return self.fake.run_sudo_chroot(cmd, self.target_dir)

    def _run_root(
        self,
        cmd: str,
        cwd: Optional[str] = None,
        stdout: Optional[BufferedWriter] = None,
        check=True
    ) -> Tuple[Optional[str], str, int]:
        """ Run command as root. """
        if self.fakeroot:
            return self.fake.run(cmd=cmd, cwd=cwd, stdout=stdout, check=check)
        else:
            return self.fake.run_sudo(cmd=cmd, cwd=cwd, stdout=stdout, check=check)

    def install_busybox(self):
        """Get busybox and add it to the initrd. """
        versiondep = None
        package = None

        for vd in self.busybox:
            # Find first available package.
            versiondep = vd
            package = self.proxy.find_package(vd)
            if not package:
                continue

            # Downlaod first available package.
            package = self.proxy.download_package(
                arch=versiondep.arch,
                package=package,
                version_relation=versiondep.version_relation
            )

            if package and package.local_file and \
                    os.path.isfile(package.local_file):
                # Download was successful.
                logging.info('Using busybox deb %s.', package.local_file)
                break

        if package is None or versiondep is None:
            logging.error('Busybox was not found! %s', self.busybox)
            exit(1)

        if not package.local_file:
            logging.error('Busybox download failed! %s', self.busybox)
            exit(1)

        logging.info('Using busybox %s (%s).', package, versiondep)

        res = package.extract(self.target_dir)
        if res is None:
            logging.critical(
                'Extraction of busybox package %s (deb: %s) failed!', package, package.local_file)
            exit(1)

        logging.info('Busybox extracted to %s.', res)

        if not os.path.isfile(os.path.join(self.target_dir, 'bin', 'busybox')):
            logging.critical(
                'Busybox binary is missing! target: %s package: %s', self.target_dir, package)
            exit(1)

        self._run_chroot('/bin/busybox --install -s /bin')

    def find_kernel_version(self, mods_dir: str) -> str:
        """ Find the right kernel version. """
        if self.kversion:
            return self.kversion

        kernel_dirs = os.path.abspath(os.path.join(mods_dir, 'lib', 'modules'))
        versions = glob.glob(f'{kernel_dirs}/*')

        if not versions:
            logging.critical(
                'Kernel version not found! mods_dir: %s, kernel_dirs: %s', mods_dir, kernel_dirs)

        versions.sort()

        return os.path.basename(versions[-1])

    def extract_modules_from_deb(self, mods_dir: str):
        """Extract the required kernel modules from the deb package.

        Args:
            mods_dir (str): Folder containing the modules.
        """
        logging.info('Modules tmp folder: %s.', mods_dir)
        logging.info('Target tmp folder: %s.', self.target_dir)

        kversion = self.find_kernel_version(mods_dir)

        logging.info('Using kernel version %s.', kversion)

        mods_src = os.path.abspath(os.path.join(
            mods_dir, 'lib', 'modules', kversion))

        mods_dep_src = os.path.join(mods_src, 'modules.dep')

        mods_dst = os.path.abspath(os.path.join(
            self.target_dir, 'lib', 'modules', kversion))

        mods_dep_dst = os.path.join(mods_dst, 'modules.dep')

        logging.debug('Mods src: %s', mods_src)
        logging.debug('Mods dst: %s', mods_dst)

        logging.debug('Create modules target...')
        self._run_root(f'mkdir -p {mods_dst}')

        orig_deps: dict[str, list[str]] = {}

        if os.path.isfile(mods_dep_src):
            with open(mods_dep_src, encoding='utf8') as f:
                lines = f.readlines()
                for line in lines:
                    parts = line.split(':', maxsplit=1)
                    key = parts[0].strip()
                    values = []
                    if len(parts) > 1:
                        vs = parts[1].strip()
                        if vs:
                            values = [dep.strip()
                                      for dep in vs.split(' ') if dep != '']
                    orig_deps[key] = values

        mq: queue.Queue[str] = queue.Queue(maxsize=-1)

        for module in self.modules:
            mq.put_nowait(module)

        while not mq.empty():
            module = mq.get_nowait()

            logging.info('Processing module %s...', module)

            src = os.path.join(mods_src, module)
            dst = os.path.join(mods_dst, module)
            dst_dir = os.path.dirname(dst)

            logging.info('Copying module %s to folder %s.', src, dst)

            if not os.path.isfile(src):
                logging.error('Module %s not found.', module)
                continue

            self._run_root(f'mkdir -p {dst_dir}')
            self._run_root(f'cp {src} {dst}')

            # Find module dependencies.
            deps = ''
            if module in orig_deps:
                mdeps = orig_deps[module]
                deps = ' '.join(mdeps)
                for mdep in mdeps:
                    mq.put_nowait(mdep)

            self._run_root(f'echo {module}: {deps} >> {mods_dep_dst}')

        # Fix ownership of modules
        self._run_root(f'chown -R 0:0 {self.target_dir}/lib/modules')

    def add_devices(self):
        """ Create device files. """
        self._run_root(f'mkdir -p {self.target_dir}/dev')

        dev_folder = os.path.join(self.target_dir, 'dev')

        for device in self.devices:
            major = (int)(device['major'])
            minor = (int)(device['major'])

            if device['type'] == 'char':
                dev_type = 'c'
                mode = '200'
            elif device['type'] == 'block':
                dev_type = 'b'
                mode = '600'
            else:
                logging.error('Unsupported device type %s for %s',
                              device['type'], device['name'])
                continue

            self._run_root(
                f'mknod -m {mode} {dev_folder}/{device["name"]} {dev_type} {major} {minor}')

            uid = device.get('uid', '0')
            gid = device.get('uid', '0')
            self._run_root(f'chown {uid}:{gid} {dev_folder}/{device["name"]}')

    def copy_files(self):
        """ Copy user-specified files in the initrd. """
        logging.debug('Files: %s', self.files)

        for entry in self.files:
            src = Path(os.path.abspath(os.path.join(
                self.config.parent, entry['source'])))

            dst = Path(self.target_dir) / entry['destination']

            dst_file = Path(self.target_dir) / entry['destination'] / src.name

            mode: str = entry.get('mode', '666')

            uid = entry.get('uid', '0')
            gid = entry.get('gid', '0')

            logging.info('Copying %s to %s.', src, dst_file)

            self._run_root(f'mkdir -p {dst}')

            if src.is_file():
                self._run_root(f'cp {src} {dst}')

                self._run_root(f'chmod {mode} {dst_file}')
                self._run_root(f'chown {uid}:{gid} {dst_file}')
            elif src.is_dir():
                self._run_root(f'cp -R  {src}/* {dst}')

                self._run_root(f'chmod -R {mode} {dst_file}')
                self._run_root(f'chown -R {uid}:{gid} {dst_file}')
            else:
                logging.warning('Source %s does not exist', src)

    def create_initrd(self, image_path: str) -> Optional[str]:
        """ Create the initrd image.  """
        self.target_dir = tempfile.mkdtemp()

        logging.info('Installing busybox...')

        self.install_busybox()

        # Create necessary directories
        for dir_name in ['proc', 'sys', 'dev', 'sysroot', 'var', 'bin',
                         'tmp', 'run', 'root', 'usr', 'sbin', 'lib', 'etc']:
            self._run_root(
                f'mkdir -p {os.path.join(self.target_dir, dir_name)}')
            self._run_root(
                f'chown 0:0 {os.path.join(self.target_dir, dir_name)}')

        mods_dir = None

        if self.modules_folder:
            mods_dir = os.path.abspath(os.path.join(
                self.config.parent, self.modules_folder))
            logging.info('Using modules from folder %s...', mods_dir)
        else:
            mods_dir = tempfile.mkdtemp()

            logging.info('Using modules from deb packages...')
            (_debs, _contents, missing) = self.proxy.download_deb_packages(
                packages=self.modules_packages,
                contents=mods_dir
            )

            if missing:
                logging.critical('Not found packages: %s', missing)

        # Extract modules directly to the initrd /lib/modules directory
        self.extract_modules_from_deb(mods_dir)

        if not self.modules_folder:
            # Remove mods temporary folder
            shutil.rmtree(mods_dir)

        # Add device nodes
        self.add_devices()

        # Copy files and directories specified in the files
        self.copy_files()

        # Create init script
        init_script: Path = Path(self.target_dir) / 'init'

        if self.template is None:
            template = os.path.join(os.path.dirname(__file__), 'init.sh')
        else:
            template = os.path.join(
                os.path.dirname(self.config), self.template)

        with open(template, encoding='utf8') as f:
            tmpl = Template(f.read())

        init_script_content = tmpl.render(
            root=self.root_device,
            mods=[m.split("/")[-1] for m in self.modules]
        )

        init_script.write_text(init_script_content)
        os.chmod(init_script, 0o755)

        # Create initrd image
        os.makedirs(os.path.dirname(image_path), exist_ok=True)
        with open(image_path, 'wb') as img:
            self._run_root(
                'find . -print0 | cpio --null -ov --format=newc', cwd=self.target_dir, stdout=img)

        return image_path

    def finalize(self):
        """ Finalize output and cleanup. """

        # delete temporary folder
        self._run_root(f' rm -rf {self.target_dir}')


def main() -> None:
    """ Main entrypoint of EBcL initrd generator. """
    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(
        description='Create an initrd image for Linux.')
    parser.add_argument('config_file', type=str,
                        help='Path to the YAML configuration file')
    parser.add_argument('output', type=str,
                        help='Path to the output directory')

    args = parser.parse_args()

    logging.info('Running initrd_generator with args %s', args)

    # Read configuration
    generator = InitrdGenerator(args.config_file)

    # Define output image path
    output_image_path = os.path.join(args.output, 'initrd.img')

    image = None
    try:
        # Create the initrd.img
        image = generator.create_initrd(output_image_path)
    except Exception as e:
        logging.critical('Image build failed with exception! %s', e)

    try:
        generator.finalize()
    except Exception as e:
        logging.error('Cleanup failed with exception! %s', e)

    if image:
        print('Image was written to %s.', image)
    else:
        exit(1)


if __name__ == '__main__':
    main()
