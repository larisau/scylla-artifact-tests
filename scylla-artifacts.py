#!/usr/bin/python

import os
import re
import logging
import threading
from pkg_resources import parse_version

from avocado import Test
from avocado import main
from avocado.utils import distro
from avocado.utils import process
from avocado.utils import path
from avocado.utils import service
from avocado.utils import wait
from avocado.utils import network

from avocado.utils.software_manager import AptBackend
from avocado.utils.software_manager import YumBackend
from avocado.utils.software_manager import DnfBackend
from avocado.utils.software_manager import ZypperBackend
from avocado.utils.software_manager import SystemInspector

SCRIPTLET_FAILURE_LIST = []


def _search_scriptlet_failure(result):
    global SCRIPTLET_FAILURE_LIST
    output = result.stdout + result.stderr
    failure_pattern = 'scriptlet failure in rpm package scylla.*'
    if re.search(failure_pattern, output):
        pkgs = []
        occurrences = re.findall(failure_pattern, output)
        for occurrence in occurrences:
            pkg = occurrence.split()[-1]
            pkgs.append(pkg)
            SCRIPTLET_FAILURE_LIST.append(pkg)


class ScyllaYumBackend(YumBackend):

    def install(self, name):
        """
        Installs package [name]. Handles local installs.
        """
        i_cmd = self.base_command + ' ' + 'install' + ' ' + name

        try:
            _search_scriptlet_failure(process.run(i_cmd, sudo=True))
            return True
        except process.CmdError:
            return False


class ScyllaDnfBackend(DnfBackend):

    def install(self, name):
        """
        Installs package [name]. Handles local installs.
        """
        i_cmd = self.base_command + ' ' + 'install' + ' ' + name

        try:
            _search_scriptlet_failure(process.run(i_cmd, sudo=True))
            return True
        except process.CmdError:
            return False


class ScyllaAptBackend(AptBackend):

    def upgrade(self, name=None):
        """
        Upgrade all packages of the system with eventual new versions.

        Optionally, upgrade individual packages.

        :param name: optional parameter wildcard spec to upgrade
        :type name: str
        """
        def update_pkg_list():
            ud_command = 'update'
            ud_cmd = self.base_command + ' ' + ud_command
            try:
                process.system(ud_cmd)
                return True
            except process.CmdError:
                return False

        wait.wait_for(update_pkg_list, timeout=300, step=30,
                      text="Wait until package list is up to date...")

        if name:
            up_command = 'install --only-upgrade'
            up_cmd = " ".join([self.base_command, self.dpkg_force_confdef,
                               up_command, name])
        else:
            up_command = 'upgrade'
            up_cmd = " ".join([self.base_command, self.dpkg_force_confdef,
                               up_command])

        try:
            process.system(up_cmd, shell=True, sudo=True)
            return True
        except process.CmdError:
            return False


class ScyllaSoftwareManager(object):

    """
    Package management abstraction layer.

    It supports a set of common package operations for testing purposes, and it
    uses the concept of a backend, a helper class that implements the set of
    operations of a given package management tool.
    """

    def __init__(self):
        """
        Lazily instantiate the object
        """
        self.initialized = False
        self.backend = None
        self.lowlevel_base_command = None
        self.base_command = None
        self.pm_version = None

    def _init_on_demand(self):
        """
        Determines the best supported package management system for the given
        operating system running and initializes the appropriate backend.
        """
        if not self.initialized:
            inspector = SystemInspector()
            backend_type = inspector.get_package_management()
            backend_mapping = {'apt-get': ScyllaAptBackend,
                               'yum': ScyllaYumBackend,
                               'dnf': ScyllaDnfBackend,
                               'zypper': ZypperBackend}

            if backend_type not in backend_mapping.keys():
                raise NotImplementedError('Unimplemented package management '
                                          'system: %s.' % backend_type)

            backend = backend_mapping[backend_type]
            self.backend = backend()
            self.initialized = True

    def __getattr__(self, name):
        self._init_on_demand()
        return self.backend.__getattribute__(name)


class StartServiceError(Exception):
    pass


class StopServiceError(Exception):
    pass


class RestartServiceError(Exception):
    pass


class InstallPackageError(Exception):
    pass


def get_scylla_logs():
    try:
        journalctl_cmd = path.find_command('journalctl')
        process.run('sudo %s -f '
                    '-u scylla-io-setup.service '
                    '-u scylla-server.service '
                    '-u scylla-jmx.service' % journalctl_cmd,
                    verbose=True, ignore_status=True)
    except path.CmdNotFoundError:
        process.run('tail -f /var/log/syslog | grep scylla', shell=True,
                    ignore_status=True)


class ScyllaServiceManager(object):

    def __init__(self):
        self.services = ['scylla-server', 'scylla-jmx']

    def _scylla_service_is_up(self):
        srv_manager = service.ServiceManager()
        for srv in self.services:
            srv_manager.status(srv)
        return not network.is_port_free(9042, 'localhost')

    def wait_services_up(self):
        service_start_timeout = 900
        output = wait.wait_for(func=self._scylla_service_is_up,
                               timeout=service_start_timeout, step=5)
        if output is None:
            e_msg = 'Scylla service does not appear to be up after %s s' % service_start_timeout
            raise StartServiceError(e_msg)

    def start_services(self):
        srv_manager = service.ServiceManager()
        for srv in self.services:
            srv_manager.start(srv)
        for srv in self.services:
            if not srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                e_msg = ('Failed to start service %s '
                         '(see logs for details)' % srv)
                raise StartServiceError(e_msg)

    def stop_services(self):
        srv_manager = service.ServiceManager()
        for srv in reversed(self.services):
            srv_manager.stop(srv)
        for srv in self.services:
            if srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                e_msg = ('Failed to stop service %s '
                         '(see logs for details)' % srv)
                raise StopServiceError(e_msg)

    def restart_services(self):
        srv_manager = service.ServiceManager()
        for srv in self.services:
            srv_manager.restart(srv)
        for srv in self.services:
            if not srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                e_msg = ('Failed to restart service %s '
                         '(see logs for details)' % srv)
                raise RestartServiceError(e_msg)


class ScyllaInstallGeneric(object):

    def __init__(self, sw_repo=None, mode='ci'):
        self.base_url = 'https://s3.amazonaws.com/downloads.scylladb.com/'
        self.mode = mode
        self.sw_manager = ScyllaSoftwareManager()
        self.sw_repo_src = sw_repo
        self.sw_repo_dst = None
        self.log = logging.getLogger('avocado.test')
        self.srv_manager = ScyllaServiceManager()

    def try_report_uuid(self):
        uuid_path = '/var/lib/scylla-housekeeping/housekeeping.uuid'
        mark_path = '/var/lib/scylla-housekeeping/housekeeping.uuid.marked'
        cmd = 'curl "https://i6a5h9l1kl.execute-api.us-east-1.amazonaws.com/prod/check_version?uu=%s&mark=scylla"'
        wait.wait_for(lambda: os.path.exists(uuid_path), timeout=30, step=5,
                      text='Waiting for housekeeping.uuid generated')

        if os.path.exists(uuid_path) and not os.path.exists(mark_path):
            with open(uuid_path) as uuid_file:
                uuid = uuid_file.read().strip()
            self.log.debug('housekeeping.uuid is %s', uuid)
            process.run(cmd % uuid, shell=True, verbose=True)
            process.run('sudo -u scylla touch %s' % mark_path, verbose=True)

    def run(self):
        wait.wait_for(self.sw_manager.upgrade, timeout=300, step=30,
                      text="Wait until system is up to date...")
        if self.mode == 'ci':
            get_packages = self.setup_ci
        else:
            get_packages = self.setup_release
        pkgs = get_packages()
        for pkg in pkgs:
            if not self.sw_manager.install(pkg):
                e_msg = ('Package %s could not be installed '
                         '(see logs for details)' % os.path.basename(pkg))
                raise InstallPackageError(e_msg)
        process.run('/usr/lib/scylla/scylla_io_setup', shell=True)

        self.srv_manager.start_services()
        self.srv_manager.wait_services_up()
        self.try_report_uuid()


class ScyllaInstallDebian(ScyllaInstallGeneric):

    def __init__(self, sw_repo, mode='ci'):
        super(ScyllaInstallDebian, self).__init__(sw_repo, mode)
        self.sw_repo_dst = '/etc/apt/sources.list.d/scylla.list'


class ScyllaInstallUbuntu1404(ScyllaInstallDebian):

    def setup_ci(self):
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        process.run('sudo apt-get update')
        result = process.run('sudo apt-cache show scylla')
        ver = re.findall("Version: (.*)", result.stdout)[0]
        if parse_version(ver) >= parse_version('1.7~rc0'):
            process.run('sudo apt-get install software-properties-common -y', shell=True)
            process.run('sudo add-apt-repository -y ppa:openjdk-r/ppa', shell=True)
            process.run('sudo apt-get update')
            process.run('sudo apt-get install -y openjdk-8-jre-headless', shell=True)
            process.run('sudo update-java-alternatives -s java-1.8.0-openjdk-amd64', shell=True)
        self.sw_manager.upgrade()
        return ['scylla']

    def setup_release(self):
        # Commenting out for now, till I make sure we don't need it anymore.
        # repo_src_1_0 = 'http://downloads.scylladb.com/deb/ubuntu/scylla-1.0.list'
        # repo_src_1_1 = 'http://downloads.scylladb.com/deb/ubuntu/scylla-1.1.list'
        # repo_src_1_2 = 'http://downloads.scylladb.com/deb/ubuntu/scylla-1.2-trusty.list'
        # repo_src_1_3 = 'http://downloads.scylladb.com/deb/ubuntu/scylla-1.3-trusty.list'
        # repo_src_1_4 = 'http://downloads.scylladb.com/deb/ubuntu/scylla-1.4-trusty.list'
        # repo_src_1_5 = 'http://downloads.scylladb.com/deb/ubuntu/scylla-1.5-trusty.list'
        # repo_src_unstable = 'http://downloads.scylladb.com/deb/unstable/ubuntu/master/latest/scylla.list'
        # repo_src = repo_src_1_5
        # pkg_list = ['scylla']
        # if self.mode == '1.0':
        #     repo_src = repo_src_1_0
        #     pkg_list = ['scylla-server', 'scylla-jmx', 'scylla-tools']
        # elif self.mode == '1.1':
        #     repo_src = repo_src_1_1
        #     pkg_list = ['scylla-server', 'scylla-jmx', 'scylla-tools']
        # elif self.mode == '1.2':
        #     repo_src = repo_src_1_2
        #     pkg_list = ['scylla']
        # elif self.mode == '1.3':
        #     repo_src = repo_src_1_3
        #     pkg_list = ['scylla']
        # elif self.mode == '1.4':
        #     repo_src = repo_src_1_4
        #     pkg_list = ['scylla']
        # elif self.mode == '1.5':
        #     repo_src = repo_src_1_5
        #     pkg_list = ['scylla']
        # elif self.mode == 'unstable':
        #     repo_src = repo_src_unstable
        #     pkg_list = ['scylla']
        # process.run('sudo curl %s -o %s' % (repo_src, self.sw_repo_dst),
        #             shell=True)
        # self.sw_manager.upgrade()
        # return pkg_list
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        process.run('sudo apt-get update')
        result = process.run('sudo apt-cache show scylla-enterprise')
        ver = re.findall("Version: (.*)", result.stdout)[0]
        if parse_version(ver) >= parse_version('1.7~rc0'):
            process.run('sudo apt-get install software-properties-common -y', shell=True)
            process.run('sudo add-apt-repository -y ppa:openjdk-r/ppa', shell=True)
            process.run('sudo apt-get update')
            process.run('sudo apt-get install -y openjdk-8-jre-headless', shell=True)
            process.run('sudo update-java-alternatives -s java-1.8.0-openjdk-amd64', shell=True)
        self.sw_manager.upgrade()
        return ['scylla-enterprise']


class ScyllaInstallUbuntu1604(ScyllaInstallDebian):

    def setup_ci(self):
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        self.sw_manager.upgrade()
        return ['scylla']

    def setup_release(self):
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        self.sw_manager.upgrade()
        return ['scylla-enterprise']


class ScyllaInstallDebian8(ScyllaInstallDebian):

    def setup_ci(self):
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        process.run('sudo apt-get update')
        result = process.run('sudo apt-cache show scylla')
        ver = re.findall("Version: (.*)", result.stdout)[0]
        if parse_version(ver) >= parse_version('1.7~rc0'):
            process.run("echo 'deb http://http.debian.net/debian jessie-backports main' > /etc/apt/sources.list.d/jessie-backports.list", shell=True)
            process.run('sudo apt-get update')
            process.run('sudo apt-get install -y -t jessie-backports openjdk-8-jre-headless', shell=True)
            process.run('sudo update-java-alternatives -s java-1.8.0-openjdk-amd64', shell=True)
        self.sw_manager.upgrade()
        return ['scylla']

    def setup_release(self):
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        process.run('sudo apt-get update')
        result = process.run('sudo apt-cache show scylla-enterprise')
        ver = re.findall("Version: (.*)", result.stdout)[0]
        if parse_version(ver) >= parse_version('1.7~rc0'):
            process.run("echo 'deb http://http.debian.net/debian jessie-backports main' > /etc/apt/sources.list.d/jessie-backports.list", shell=True)
            process.run('sudo apt-get update')
            process.run('sudo apt-get install -y -t jessie-backports openjdk-8-jre-headless', shell=True)
            process.run('sudo update-java-alternatives -s java-1.8.0-openjdk-amd64', shell=True)
        self.sw_manager.upgrade()
        return ['scylla-enterprise']


class ScyllaInstallFedora(ScyllaInstallGeneric):

    def __init__(self, sw_repo, mode='ci'):
        super(ScyllaInstallFedora, self).__init__(sw_repo, mode)
        self.sw_repo_dst = '/etc/yum.repos.d/scylla.repo'


class ScyllaInstallFedora22(ScyllaInstallFedora):

    def setup_ci(self):
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        self.sw_manager.upgrade()
        return ['scylla']

    def setup_release(self):
        repo_src_1_0 = 'http://downloads.scylladb.com/rpm/fedora/scylla-1.0.repo'
        repo_src_1_1 = 'http://downloads.scylladb.com/rpm/fedora/scylla-1.1.repo'
        repo_src_1_2 = 'http://downloads.scylladb.com/rpm/fedora/scylla-1.2.repo'
        repo_src_unstable = 'http://downloads.scylladb.com/rpm/unstable/fedora/master/latest/scylla.repo'
        repo_src = repo_src_1_1
        pkg_list = []
        if self.mode == '1.0':
            repo_src = repo_src_1_0
            pkg_list = ['scylla-server', 'scylla-jmx', 'scylla-tools']
        elif self.mode == '1.1':
            repo_src = repo_src_1_1
            pkg_list = ['scylla-server', 'scylla-jmx', 'scylla-tools']
        elif self.mode == '1.2':
            repo_src = repo_src_1_2
            pkg_list = ['scylla']
        elif self.mode == 'unstable':
            repo_src = repo_src_unstable
            pkg_list = ['scylla']
        process.run('sudo curl %s -o %s' % (repo_src, self.sw_repo_dst),
                    shell=True)
        self.sw_manager.upgrade()
        return pkg_list


class ScyllaInstallCentOS(ScyllaInstallGeneric):

    def __init__(self, sw_repo, mode='ci'):
        super(ScyllaInstallCentOS, self).__init__(sw_repo, mode)
        self.sw_repo_dst = '/etc/yum.repos.d/scylla.repo'

    def _centos_remove_system_packages(self):
        self.sw_manager.remove('boost-thread')
        self.sw_manager.remove('boost-system')
        self.sw_manager.remove('abrt')


class ScyllaInstallCentOS7(ScyllaInstallCentOS):

    def setup_ci(self):
        self._centos_remove_system_packages()
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        self.sw_manager.upgrade()
        return ['scylla']

    def setup_release(self):
        # Commenting out for now, till I make sure we don't need this anymore.
        # self._centos_remove_system_packages()
        # repo_src_1_0 = 'http://downloads.scylladb.com/rpm/centos/scylla-1.0.repo'
        # repo_src_1_1 = 'http://downloads.scylladb.com/rpm/centos/scylla-1.1.repo'
        # repo_src_1_2 = 'http://downloads.scylladb.com/rpm/centos/scylla-1.2.repo'
        # repo_src_1_3 = 'http://downloads.scylladb.com/rpm/centos/scylla-1.3.repo'
        # repo_src_1_4 = 'http://downloads.scylladb.com/rpm/centos/scylla-1.4.repo'
        # repo_src_1_5 = 'http://downloads.scylladb.com/rpm/centos/scylla-1.5.repo'
        # repo_src_unstable = 'http://downloads.scylladb.com/rpm/unstable/centos/master/latest/scylla.repo'
        # repo_src = repo_src_1_5
        # pkg_list = ['scylla']
        # if self.mode == '1.0':
        #     repo_src = repo_src_1_0
        #     pkg_list = ['scylla-server', 'scylla-jmx', 'scylla-tools']
        # elif self.mode == '1.1':
        #     repo_src = repo_src_1_1
        #     pkg_list = ['scylla-server', 'scylla-jmx', 'scylla-tools']
        # elif self.mode == '1.2':
        #     repo_src = repo_src_1_2
        #     pkg_list = ['scylla']
        # elif self.mode == '1.3':
        #     repo_src = repo_src_1_3
        #     pkg_list = ['scylla']
        # elif self.mode == '1.4':
        #     repo_src = repo_src_1_4
        #     pkg_list = ['scylla']
        # elif self.mode == '1.5':
        #     repo_src = repo_src_1_5
        #     pkg_list = ['scylla']
        # elif self.mode == 'unstable':
        #     repo_src = repo_src_unstable
        # process.run('sudo curl %s -o %s' % (repo_src, self.sw_repo_dst),
        #             shell=True)
        # self.sw_manager.upgrade()
        # return pkg_list
        self._centos_remove_system_packages()
        process.run('sudo curl %s -o %s' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        self.sw_manager.upgrade()
        return ['scylla-enterprise']


class ScyllaInstallAMI(ScyllaInstallGeneric):

    def run(self):
        self.log.info("Testing AMI, let's just check if the DB is up...")
        self.srv_manager.wait_services_up()
        self.try_report_uuid()


class ScyllaArtifactSanity(Test):

    """
    Sanity check of the build artifacts (deb, rpm)

    setup: Install artifacts (deb, rpm)
    1) Run cassandra-stress
    2) Run nodetool
    """
    setup_done_file = None
    srv_manager = ScyllaServiceManager()

    def get_setup_file_done(self):
        tmpdir = os.path.dirname(self.workdir)
        return os.path.join(tmpdir, 'scylla-setup-done')

    def scylla_setup(self):
        # Let's start the logs thread before package install
        self._log_collection_thread = threading.Thread(target=get_scylla_logs)
        self._log_collection_thread.start()
        sw_repo = self.params.get('sw_repo', default=None)
        mode = self.params.get('mode', default='ci')
        ami = self.params.get('ami', default=False) is True
        # Per my discussion with Lucas on the PR that added these lines, these lines doesn't make sense now.
        # Commenting out for now, till I fully test the impact on all artifacts jobs.
        # if sw_repo is not None:
        #     if sw_repo.strip() != 'EMPTY':
        #         mode = 'ci'

        detected_distro = distro.detect()
        fedora_22 = (detected_distro.name.lower() == 'fedora' and
                     detected_distro.version == '22')
        ubuntu_14_04 = (detected_distro.name.lower() == 'ubuntu' and
                        detected_distro.version == '14' and
                        detected_distro.release == '04')
        ubuntu_16_04 = (detected_distro.name.lower() == 'ubuntu' and
                        detected_distro.version == '16' and
                        detected_distro.release == '04')
        debian_8 = (detected_distro.name.lower() == 'debian' and
                    detected_distro.version == '8')
        centos_7 = (detected_distro.name.lower() == 'centos' and
                    detected_distro.version == '7')

        installer = None

        if ami:
            installer = ScyllaInstallAMI()

        elif ubuntu_14_04:
            installer = ScyllaInstallUbuntu1404(sw_repo=sw_repo, mode=mode)

        elif ubuntu_16_04:
            installer = ScyllaInstallUbuntu1604(sw_repo=sw_repo, mode=mode)

        elif debian_8:
            installer = ScyllaInstallDebian8(sw_repo=sw_repo, mode=mode)

        elif fedora_22:
            installer = ScyllaInstallFedora22(sw_repo=sw_repo, mode=mode)

        elif centos_7:
            installer = ScyllaInstallCentOS7(sw_repo=sw_repo, mode=mode)

        else:
            self.skip('Unsupported OS: %s' % detected_distro)

        installer.run()
        os.mknod(self.get_setup_file_done())

    def setUp(self):
        if not os.path.isfile(self.get_setup_file_done()):
            self.scylla_setup()

    def run_cassandra_stress(self):
        def check_output(result):
            output = result.stdout + result.stderr
            lines = output.splitlines()
            for line in lines:
                if 'java.io.IOException' in line:
                    self.fail('cassandra-stress: %s' % line.strip())
        cassandra_stress_exec = path.find_command('cassandra-stress')
        stress_populate = ('%s write n=10000 -mode cql3 native -pop seq=1..10000' %
                           cassandra_stress_exec)
        result_populate = process.run(stress_populate)
        check_output(result_populate)
        stress_mixed = ('%s mixed duration=1m -mode cql3 native '
                        '-rate threads=10 -pop seq=1..10000' %
                        cassandra_stress_exec)
        result_mixed = process.run(stress_mixed, shell=True)
        check_output(result_mixed)

    def run_nodetool(self):
        nodetool_exec = path.find_command('nodetool')
        nodetool = '%s status' % nodetool_exec
        process.run(nodetool)

    def test_after_install(self):
        self.run_nodetool()
        self.run_cassandra_stress()
        if SCRIPTLET_FAILURE_LIST:
            self.fail('RPM scriptlet failure reported for package(s): %s' %
                      ",".join(SCRIPTLET_FAILURE_LIST))

    def test_after_stop_start(self):
        self.srv_manager.stop_services()
        self.srv_manager.start_services()
        self.srv_manager.wait_services_up()
        self.run_nodetool()
        self.run_cassandra_stress()

    def test_after_restart(self):
        self.srv_manager.restart_services()
        self.srv_manager.wait_services_up()
        self.run_nodetool()
        self.run_cassandra_stress()


if __name__ == '__main__':
    main()
