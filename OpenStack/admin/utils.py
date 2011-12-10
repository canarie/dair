# vim: tabstop=4 shiftwidth=4 softtabstop=4

import os
import time
import subprocess

def get_regions(region_rc_filename=None):
    if not region_rc_filename:
        region_rc_files = ('albertarc', 'quebecrc')
    else:
        region_rc_files = (region_rc_filename, )

    regions = []

    for region_rc_file in region_rc_files:
        region_rc_file_path = os.path.expanduser('~/creds-admin/%(region_rc_file)s' % locals())

        if os.path.exists(region_rc_file_path):
            access_key = execute("grep EC2_ACCESS_KEY= " + region_rc_file_path)[0].split('=')[1].strip('"\n')
            secret_access_key = execute("grep EC2_SECRET_KEY= " + region_rc_file_path)[0].split('=')[1].strip('"\n')
            url = execute("grep EC2_URL= " + region_rc_file_path)[0].split('=')[1].strip('"\n')
            
            regions.append(Region(access_key, secret_access_key, url))

    return regions

class Region:
    def __init__(self, access_key, secret_access_key, url):
        self.access_key = access_key
        self.secret_access_key = secret_access_key
        self.url = url

def execute(cmd, process_input=None, addl_env=None, check_exit_code=True, attempts=1):
    while attempts > 0:
        attempts -= 1
        try:
            env = os.environ.copy()
            if addl_env:
                env.update(addl_env)
            obj = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            result = None
            if process_input != None:
                result = obj.communicate(process_input)
            else:
                result = obj.communicate()
            obj.stdin.close()
            if obj.returncode:
                if check_exit_code and obj.returncode != 0:
                    (stdout, stderr) = result
                    raise ProcessExecutionError(exit_code=obj.returncode, stdout=stdout, stderr=stderr, cmd=cmd)
            return result
        except ProcessExecutionError:
            if not attempts:
                raise
            else:
                time.sleep(5)

class ProcessExecutionError(IOError):
    def __init__(self, stdout=None, stderr=None, exit_code=None, cmd=None, description=None):
        if description is None:
            description = "Unexpected error while running command."
        if exit_code is None:
            exit_code = '-'
        message = "%(description)s\nCommand: %(cmd)s\nExit code: %(exit_code)s\nStdout: %(stdout)r\nStderr: %(stderr)r" % locals()
        IOError.__init__(self, message)

