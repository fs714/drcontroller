import logging
import os
from functools import wraps
from taskflow import engines
from base import AnsibleTask, ShellTask, LinearFlowCreator, UnorderedFlowCreator

import sys
sys.path.append("/home/eshufan/project/drcontroller/db")
from db_Dao import DRGlanceDao, DRNovaDao

def task_list(fn):
    @wraps(fn)
    def wrapper(self, tasks, *args, **kwargs):
        task = fn(self, *args, **kwargs)
        task.add_result_handler(self.result_handler)
        tasks.append(task)
    return wrapper

class RecoveryHandler(object):
    def __init__(self):
        self.logger = logging.getLogger("RecoveryHandler")
        self.logger.info('Init RecoveryHandler')
        result_handler = ResultHandler()
        self.glance_handler = GlanceHandler(result_handler)
        self.nova_handler = NovaHandler(result_handler)

    def start(self, *req, **kwargs):
        self.logger = logging.getLogger("RecoveryHandler:start")

        self.logger.info("--- Hello Recovery ---")
        flow = self.prepare()
        eng = engines.load(flow)
        eng.run()
        results = eng.storage.fetch_all()
        print results
        return ['Hello Recovery']

    def prepare(self):
        flow = [self.glance_handler.prepare(), self.nova_handler.prepare()]
        return UnorderedFlowCreator().create('DR', flow)

class RecoveryError(Exception):
    pass

class ResultHandler(object):
    def __init__(self):
        pass

    def analyze(self, name, result):
        for host in result['dark']:
            print 'Error in Task "%s": %s' % (name, result['dark'][host]['msg'])
            raise RecoveryError('Error in Task "%s": %s' % (name, result['dark'][host]['msg']))
        for host in result['contacted']:
            self.analyze_result_for_host(name, result['contacted'][host])

    def analyze_result_for_host(self, name, result):
        if 'msg' in result and result['msg'] != '':
            print 'Error in Task "%s": %s' % (name, result['msg'])
            if 'service-start' in name:
                raise RecoveryError('Error in Task "%s": %s' % (name, result['msg']))
        if 'stderr' in result and result['stderr'] != '':
            print 'Error in Task "%s": %s' % (name, result['stderr'])
            if 'role_change' in name and 'State change failed' in result['stderr']:
                raise RecoveryError('Error in Task "%s": %s' % (name, result['stderr']))
        if 'stdout' in result and result['stdout'] != '':
            print 'Output in Task "%s": %s' % (name, result['stdout'])

class ComponentHandler(object):
    def __init__(self, component, hosts, disc, result_handler):
        self.component = component
        self.hosts = hosts
        self.disc = disc
        self.config = None
        self.disc_tasks = []
        self.result_handler = result_handler
        self.restore_tasks =[]

    @task_list
    def create_service_stop_task(self, hosts):
        return AnsibleTask('%s_service_stop' % self.component, hosts, 'service', 'name=openstack-%s-api state=stopped' % self.component)

    @task_list
    def create_service_start_task(self, hosts):
        return AnsibleTask('%s_service_start' % self.component, hosts, 'service', 'name=openstack-%s-api state=started' % self.component)

    @task_list
    def create_role_change_task(self):
        drdb = 'openstack' #config
        return ShellTask('%s_role_change' % self.component, self.hosts, 'drbdadm primary %s' % drdb)

    @task_list
    def create_backup_task(self):
        return ShellTask('%s_fs_backup' % self.component, self.hosts, 'chdir=/var/lib mv %s %sbak' % (self.component, self.component))

    @task_list
    def create_mount_task(self):
        return AnsibleTask('%s_fs_mount' % self.component, self.hosts, 'mount', 'src=/dev/%s name=/var/lib/%s fstype=xfs state=mounted' % (self.disc, self.component))

    @task_list
    def create_rename_uuid_task(self, host, directory, old, new):
        return ShellTask('uuid_rename', [host], 'chdir=%s mv %s %s' % (directory, old, new))

    @task_list
    def create_copy_task(self, host, src, dest, uuid):
        #TODO backup
        return AnsibleTask('%s_copy_%s' % (uuid, os.path.basename(src)), [host], 'copy', 'src=%s dest=%s backup=yes' % (src, dest))

    @task_list
    def create_fetch_task(self, host, src, dest, uuid):
        return AnsibleTask('%s_fetch_%s' % (uuid, os.path.basename(src)), [host], 'fetch', 'src=%s dest=%s flat=yes' % (src, dest))

    def prepare(self):
        self.create_tasks()
        return self.create_flow()

    def create_tasks(self):
        raise NotImplementedError()

    def create_flow(self):
        raise NotImplementedError()

    def analyze(self):
        raise NotImplementedError()

class GlanceHandler(ComponentHandler):
    def __init__(self, result_handler):
        controllers = ['10.175.150.16'] #config
        super(GlanceHandler, self).__init__('glance', controllers, 'drbd0', result_handler)
        self.db = DRGlanceDao()
        self.image_tasks = {}

    def create_tasks(self):
        self.create_service_stop_task(self.disc_tasks, self.hosts)
        self.create_backup_task(self.disc_tasks)
        self.create_role_change_task(self.disc_tasks)
        self.create_mount_task(self.disc_tasks)
        for (uuid_primary, uuid_local) in self.db.get_all_uuids():
            self.image_tasks.setdefault(uuid_local, [])
            self.create_rename_uuid_task(self.image_tasks[uuid_local], self.hosts[0], '/var/lib/glance/images', uuid_primary, uuid_local)
        self.create_service_start_task(self.restore_tasks, self.hosts)

    def create_flow(self):
        image_flows = [LinearFlowCreator().create('image_op_by_uuid', self.image_tasks[uuid]) for uuid in self.image_tasks]
        image_flow = UnorderedFlowCreator().create('images_op', image_flows)
        self.disc_tasks.append(image_flow)
        #TODO empty ef
        return LinearFlowCreator().create('glance_op', self.disc_tasks + self.restore_tasks)

class NovaHandler(ComponentHandler):
    def __init__(self, result_handler):
        nodes = ['10.175.150.16'] #config
        super(NovaHandler, self).__init__('nova', nodes, 'drbd1', result_handler)
        self.uuid_db = DRNovaDao()
        self.instance_tasks = {}
        self.base_tasks = {}
        self.rebase_tasks = []

    @task_list
    def create_rebase_task(self, host, instance_uuid_local):
        base_uuid_local = self.uuid_mapping_db.get_base_uuid_for_instance(instance_uuid_local)
        return ShellTask('rebase', host, 'chdir=/var/lib/nova/instances/%s qemu-img -u -b /var/lib/nova/instances/_base/%s disk' % (instance_uuid_local, base_uuid_local))

    def create_tasks(self):
        controllers = ['10.175.150.16'] #config
        self.create_service_stop_task(self.disc_tasks, controllers)
        self.create_backup_task(self.disc_tasks)
        self.create_role_change_task(self.disc_tasks)
        self.create_mount_task(self.disc_tasks)
        for (uuid_primary, uuid_local, host) in self.uuid_db.get_uuids_node(base=True):
            self.base_tasks.setdefault(uuid_local, [])
            self.create_rename_uuid_task(self.base_tasks[uuid_local], host, '/var/lib/nova/instances', uuid_primary, uuid_local)
        for (uuid_primary, uuid_local, host) in self.uuid_db.get_uuids_node(base=False):#[('', 'f6158ecb-18ca-4295-b3dd-3d7e0f7394d2', '10.175.150.16')]:
            self.instance_tasks.setdefault(uuid_local, [])
            self.create_rename_uuid_task(self.instance_tasks[uuid_local], host, '/var/lib/nova/instances/_base', uuid_primary, uuid_local)
            self.create_fetch_task(self.instance_tasks[uuid_local], host, '/var/lib/novabak/instances/%s/disk.info' % uuid_local, '/tmp/%s/' % uuid_local, uuid_local)
            self.create_fetch_task(self.instance_tasks[uuid_local], host, '/var/lib/novabak/instances/%s/libvirt.xml' % uuid_local, '/tmp/%s/' % uuid_local, uuid_local)
            self.create_copy_task(self.instance_tasks[uuid_local], host, '/tmp/%s/disk.info' % uuid_local, '/var/lib/nova/instances/%s/' % uuid_local, uuid_local)
            self.create_copy_task(self.instance_tasks[uuid_local], host, '/tmp/%s/libvirt.xml' % uuid_local, '/var/lib/nova/instances/%s/' % uuid_local, uuid_local)
            #self.create_rebase_task(self.rebase_tasks, host, uuid_local)
        self.create_service_start_task(self.restore_tasks, self.hosts)

    def create_flow(self):
        base_flows = [LinearFlowCreator().create('base_op_by_uuid', self.base_tasks[uuid]) for uuid in self.base_tasks]
        base_flow = UnorderedFlowCreator().create('base_op', base_flows)
        instance_flows = [LinearFlowCreator().create('instance_op_by_uuid', self.instance_tasks[uuid]) for uuid in self.instance_tasks]
        instance_flow = UnorderedFlowCreator().create('instances_op', instance_flows)
        rebase_flow = UnorderedFlowCreator().create('rebase_op', self.rebase_tasks)
        self.disc_tasks.append(base_flow, instance_flow, rebase_flow)
        self.disc_tasks.append(instance_flow) #NO!!!!
        return LinearFlowCreator().create('nova_op', self.disc_tasks + self.restore_tasks)

