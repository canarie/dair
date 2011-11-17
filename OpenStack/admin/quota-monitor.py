#! /usr/bin/env python

#################################################################
# File:     quota-monitor.py
# Purpose:  Aggregate and balance quotas over zones in OpenStack 
# Author:   Andrew Nisbet andrew.nisbet@cybera.ca
# Date:     October 3, 2011
# Version:  0.1 - Dev. Not ready for initial release. This 
#			version does not change metadata_items.
#################################################################

import os			# for getcwd()
import sys
import getopt       # Command line processing.
import string		# for atoi()
import subprocess	# for __execute_shell__()
import logging		# for logging
import os.path		# for file testing.

#APP_DIR = '/home/cybera/dev/dair/OpenStack/admin/'
APP_DIR = '/root/dair/OpenStack/admin/'
GSTD_QUOTA_FILE = APP_DIR + "quota-baseline.cfg" # Gold standard quotas for baseline.
DELINQUENT_FILE = "/var/lib/quota-monitor/emailed.lst" # list of delinquent projects that HAVE been emailed.
#NOVA_CONF = "/home/cybera/dev/nova.conf" # nova.conf -- change for production.
NOVA_CONF = "/etc/nova/nova.conf" # nova.conf
AUDIT = False

class ProcessExecutionError(IOError):
    def __init__(self, stdout=None, stderr=None, exit_code=None, cmd=None, description=None):
        if description is None:
            description = "Unexpected error while running command."
        if exit_code is None:
            exit_code = '-'
        message = "%(description)s\nCommand: %(cmd)s\nExit code: %(exit_code)s\nStdout: %(stdout)r\nStderr: %(stderr)r" % locals()
        IOError.__init__(self, message)

class QuotaLogger:
	"""Logs events of interest."""
	### PRODUCTION CODE ###
	LOG_FILE = "/var/log/dair/quota-monitor.log" # log file
	#LOG_FILE = "quota-monitor.log" # log file
	def __init__(self):
		self.logger = logging.getLogger('quota-monitor')
		hdlr = logging.FileHandler(QuotaLogger.LOG_FILE)
		formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
		hdlr.setFormatter(formatter)
		self.logger.addHandler(hdlr) 
		self.logger.setLevel(logging.WARNING)
		
	def error(self, msg):
		self.logger.error(msg)

class ZoneInstance:
	def __init__(self):
		self.instances = {}
		
	def set_instance_count_per_project(self, which_quota, project_count):
		"""Used to populate all the projects' specific quota.
		param which_quota: the name of the quota to set. Default values are
		listed in Quota, like Quota.G=gigabytes.
		param project_count: a dictionary of project name=current_usage.
		>>> zi = ZoneInstance()
		>>> pc = {}
		>>> pc['a'] = 1
		>>> zi.set_instance_count_per_project(Quota.M, pc)
		>>> print zi.get_projects()
		['a']
		>>> a_quota = zi.get_resources('a')
		>>> print a_quota
		'metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		"""
		for project in project_count:
			project_instance = Quota(project)
			try:
				project_instance = self.instances[project]
			except KeyError: # no instances for this project logged yet.
				pass
			project_instance.set_quota(which_quota, project_count[project])
			self.instances[project] = project_instance
			
	# this method aggregates the argument zone_instance's values with this one.
	def sum(self, other_zone):
		"""
		Given another zone_instance add its values to this zone's values. The
		net result is that this object will contain the sum of resources from 
		the second zone, and this zone.
		"""
		# zone_instance = {'project': Quota}, so for all the projects running...
		for project in other_zone.get_projects():
			my_quota = None
			try:
				my_quota = self.instances[project]
			except KeyError: # this project isn't using resources in other_zone.
				my_quota = Quota(project)
			my_quota.add(other_zone.get_resources(project))
			self.instances[project] = my_quota
			
	def get_projects(self):
		"""Returns the running instances of a given project name or an empty quota
		object if the project doesn't exist."""
		return self.instances.keys()
		
	def get_resources(self, project):
		"""Returns the running instances of a given project name or an empty quota
		object if the project doesn't exist.
		>>> zi = ZoneInstance()
		>>> pc = {}
		>>> pc['a'] = 1
		>>> zi.set_instance_count_per_project(Quota.M, pc)
		>>> print zi.get_projects()
		['a']
		>>> quota = zi.get_resources('a')
		>>> print quota
		'metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> quota = zi.get_resources('no_project')
		>>> print quota
		'metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		"""
		try:
			return self.instances[project]
		except KeyError:
			return Quota(project)
		
# sql queries:
# basic passing:
#		ssh root@openstack1.cybera.ca 'mysql -uroot -pxxxxxxxxxx nova -e "show tables;"'
# total instances by project:
# 		select project_id, count(state) from instances where state=1 group by project_id;
# total size of all volumes by project gigbytes:
# 		select project_id, sum(size) from volumes where attach_status='attached' group by project_id;
# total volumes by project:
#		select project_id, count(size) from volumes where attach_status='attached' group by project_id;
# Total number of floating_ips in use by a project (make sure you get rid of the NULL project value):
# 		select project_id, count(deleted) from floating_ips where deleted=0 group by project_id;
# CPUs in use by a project:
# 		select project_id, sum(vcpus) from instances where state_description='running' group by project_id;
#		select project_id, sum(vcpus) from instances where state=1 group by project_id;
class ZoneQueryManager:
	def __init__(self):
		self.Q_PROJECT_INSTANCES = """ "select project_id, count(state) from instances where deleted=0 group by project_id;" """
		self.Q_PROJECT_GIGABYTES = """ "select project_id, sum(size) from volumes where deleted=0 group by project_id;" """ # returns empty set not expected '0'. attach_status=\\"attached\\"
		self.Q_PROJECT_VOLUMES   = """ "select project_id, count(size) from volumes where deleted=0 group by project_id;" """
		self.Q_PROJECT_FLOAT_IPS = """ "select project_id, count(deleted) from floating_ips where deleted=0 group by project_id;" """
		self.Q_PROJECT_CPUS      = """ "select project_id, sum(vcpus) from instances where deleted=0 group by project_id;" """
		self.regions = {} # {'full_name' = ip}
		self.instances = {} # {'full_name' = ZoneInstance}
		self.password = None
		# this requires two greps of the nova.conf file but could be done in one.
		# get the regions like: --region_list=alberta=208.75.74.10,quebec=208.75.75.10
		results = self.__execute_shell__("grep region_list " + NOVA_CONF)
		try:
			results = results.split('region_list=')[1]
		except:
			log = QuotaLogger()
			msg = "Nova conf not found at: " + NOVA_CONF
			log.error(msg)
			raise QuotaException(msg)
		# now split on the regions separated by a ','
		results = results.split(',')
		for result in results:
			name_value = result.split('=')
			self.regions[name_value[0].strip()] = name_value[1].strip() # gets rid of nagging newline
		# now the password --sql_connection=mysql://root:xxxxxxxxxxxxx@192.168.2.10/nova
		results = self.__execute_shell__("grep sql_connection " + NOVA_CONF)
		self.password = results.split('root:')[1].split('@')[0] # yuck.
		#print self.password, self.regions
		
	def __execute_shell__(self, command_and_args):
		""" returns the stdout of a Unix command """
		cmd = command_and_args.split()
		if len(cmd) < 1:
			return "<no cmd to execute>"
		process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		return process.communicate()[0]
		
	######################################################
	# code snippet comes from the utils bundle package but does not work with regular unix shell commands.
	def __execute_nova__(self, cmd, process_input=None, addl_env=None, check_exit_code=True, attempts=1):
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


	######################################################
		
	def get_zone_resource_snapshots(self):
		"""
		Run queries of resources being used in each zone
		and stores the results in the instances dictionary
		"""
		for zone in self.regions.keys():
			# populate the zone's project instances
			zone_instance_data = ZoneInstance()
			self.__query_project_statuses__(zone, zone_instance_data)
			self.instances[zone] = zone_instance_data

	def __query_project_statuses__(self, zone, zone_project_instances):
		"""
		This method takes a zone instance and populates it with current
		values via queries to the database.
		"""
		ssh_cmd = "ssh root@" + self.regions[zone]
		sql_cmd_prefix = " 'mysql -uroot -p" + self.password + " nova -e "
		sql_cmd_suffix = "'"
		# for each quota run a query for the values currently in this zone.
		
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_GIGABYTES + sql_cmd_suffix
		cmd_result = self.__execute_nova__(ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_GIGABYTES + sql_cmd_suffix)
		result_dict = self.__parse_query_result__(cmd_result)
		zone_project_instances.set_instance_count_per_project(Quota.G, result_dict)
			
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_FLOAT_IPS + sql_cmd_suffix
		cmd_result = self.__execute_nova__(ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_FLOAT_IPS + sql_cmd_suffix)
		result_dict = self.__parse_query_result__(cmd_result)
		zone_project_instances.set_instance_count_per_project(Quota.F, result_dict)
		
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_INSTANCES + sql_cmd_suffix
		cmd_result = self.__execute_nova__(ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_INSTANCES + sql_cmd_suffix)
		result_dict = self.__parse_query_result__(cmd_result)
		zone_project_instances.set_instance_count_per_project(Quota.I, result_dict)
		
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_VOLUMES + sql_cmd_suffix
		cmd_result = self.__execute_nova__(ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_VOLUMES + sql_cmd_suffix)
		result_dict = self.__parse_query_result__(cmd_result)
		zone_project_instances.set_instance_count_per_project(Quota.V, result_dict)
		
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_CPUS + sql_cmd_suffix
		cmd_result = self.__execute_nova__(ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_CPUS + sql_cmd_suffix)
		result_dict = self.__parse_query_result__(cmd_result)
		zone_project_instances.set_instance_count_per_project(Quota.C, result_dict)
		
	def set_quota(self, zone, baseline_quota, computed_quota=None, audit=False):
		"""Sets a quota for a project in a secific zone."""
		address = self.regions[zone]
		audit_results_before = ""
		if audit: # collect the current quota setting later we will compare to what we set it to.
			euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + baseline_quota.get_project_name() + "\""
			audit_results_before = self.__execute_nova__(euca_cmd)
			
		# here we reset all quotas to their baselines.
		if computed_quota == None:
			print "baseline_quota for " + baseline_quota.get_project_name() + ": ", baseline_quota
			for quota_name in baseline_quota.get_changed_quotas(True):
				euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + baseline_quota.get_project_name() + " " + quota_name + " " + str(baseline_quota.get_quota(quota_name)) + "\""
				results = self.__execute_nova__(euca_cmd)
				if self.__is_successful__(quota_name, baseline_quota.get_quota(quota_name), results) == False:
					log = QuotaLogger()
					log.error("failed to set '" + quota_name + "' for " + baseline_quota.get_project_name() + " in zone " + zone)
		else:
			# ensure that we don't set negative quota values by normalizing the quota.
			# I do this because we have computed the project's allowed resources - the instances running
			# It could be a negative number but we shouldn't set a quota to a negative value. 
			#if computed_quota.is_over_quota():
			#	computed_quota.__normalize__()
			
			for quota_name in computed_quota.get_changed_quotas():
				euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + computed_quota.get_project_name() + " " + quota_name + " " + str(computed_quota.get_quota(quota_name)) + "\""
				results = self.__execute_nova__(euca_cmd)
				if self.__is_successful__(quota_name, computed_quota.get_quota(quota_name), results) == False:
					log = QuotaLogger()
					log.error("failed to set '" + quota_name + "' for " + computed_quota.get_project_name() + " in zone " + zone)
					
		# now check if setting the quotas worked.
		if audit: # collect the current quota setting later we will compare to what we set it to.
			euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + baseline_quota.get_project_name() + "\""
			audit_results_after = self.__execute_nova__(euca_cmd)
			if audit_results_before == audit_results_after:
				log = QuotaLogger()
				log.error("quotas have not changed for " + computed_quota.get_project_name() + " in zone " + zone)
		
	def __is_successful__(self, quota_name, expected, results):
		"""Returns True if the command successfully fired and false otherwise.
		>>> results = ('metadata_items: 128\\ngigabytes: 100\\nfloating_ips: 10\\ninstances: 10\\nvolumes: 10\\ncores: 8\\n', '')
		>>> zqm = ZoneQueryManager()
		>>> print zqm.__is_successful__(Quota.G, 100, results)
		True
		>>> print zqm.__is_successful__(Quota.G, 99, results)
		False
		>>> print zqm.__is_successful__(Quota.C, 8, results)
		True
		>>> print zqm.__is_successful__(Quota.F, 10, results)
		True
		>>> print zqm.__is_successful__(Quota.F, -1, results)
		False
		>>> print zqm.__is_successful__(Quota.F, 10, ())
		False
		>>> print zqm.__is_successful__(Quota.F, 10, (''))
		False
		>>> print zqm.__is_successful__(Quota.F, 10, ('', ''))
		False
		"""
		try:
			r_str = results[0]
		except IndexError:
			return False
		for q in r_str.splitlines():
			test_quota = q.split(': ')
			if test_quota[0] == quota_name and string.atoi(test_quota[1]) == expected:
				return True
		return False # if we got here we have failed.
		
		
	def get_zones(self):
		""" Returns the names of the regions collected from nova.conf. """
		return self.regions.keys()
		
	def get_my_zones_resources(self, zone, project):
		"""Returns the resouces being used by a project in the argument zone.
		"""
		instances_locally = self.instances[zone]
		return instances_locally.get_resources(project)

	def get_other_zones_current_resources(self, zone):
		"""
		Returns a ZoneInstance object of all projects in zones other
		than the named zone, and including project resource usage.
		"""
		#other_zones_resources = zoneManager.get_other_zones_current_resources(zone)
		other_zones_resources = ZoneInstance()
		for z in self.regions.keys():
			if z == zone:
				#print "skipping " + z
				continue
			else:
				other_zones_resources.sum(self.instances[z]) # add the project instances from the other snapshot(s)
				#print "adding: resources from zone " + z
		return other_zones_resources
		
	def __parse_query_result__(self, table):
		""" 
		Takes a table output on stdout and returns it as a dictionary of 
		results project=value. 
		>>> zqm = ZoneQueryManager()
		>>> table = ('project_id	sum(size)', '')
		>>> r = zqm.__parse_query_result__(table)
		>>> print r
		{}
		>>> table = ()
		>>> r = zqm.__parse_query_result__(table)
		>>> print r
		{}
		>>> table = ('project_id\\tcount(state)\\n1000RareLogic\\t4\\n1002Gnowit\\t1\\n1012BiOS\\t3\\n1016LiveReach3\\t5\\n1037Innovative\\t3\\n1041STC\\t1\\n1042IgnitePlayBeta\\t4\\n1045KiribatuRMS\\t2\\n1047VRStorm\\t5\\n1051BGPmon\\t3\\n1052idQuanta\\t3\\n1057TRLabs1\\t3\\n1057TRLabs2\\t1\\n1058iRok2\\t4\\n1059InsideMapp\\t1\\n1079ProjectWhiteCard\\t4\\n1150Dyno\\t2\\nMetafor\\t4\\nmoodle\\t9\\nnisbet\\t1\\npreTrainingTST\\t1\\nrackspace\\t1\\nrtest\\t1\\nspawn\\t1\\n', '')
		>>> r = zqm.__parse_query_result__(table)
		>>> print r
		{'spawn': 1, '1047VRStorm': 5, '1002Gnowit': 1, '1052idQuanta': 3, '1059InsideMapp': 1, '1037Innovative': 3, 'preTrainingTST': 1, '1041STC': 1, '1016LiveReach3': 5, '1045KiribatuRMS': 2, '1058iRok2': 4, '1042IgnitePlayBeta': 4, '1012BiOS': 3, 'rackspace': 1, 'Metafor': 4, '1051BGPmon': 3, 'moodle': 9, 'nisbet': 1, 'rtest': 1, '1150Dyno': 2, '1000RareLogic': 4, '1079ProjectWhiteCard': 4, '1057TRLabs1': 3, '1057TRLabs2': 1}
		"""
		results = {}
		if len(table) < 1: # empty set test -- naive test fix me
			return results
		clean_table = table[0]
		rows = clean_table.splitlines()
		# remove the first row which is the column titles from the query
		# or possibly an empty set message.
		del rows[0]
		for row in rows:
			data = row.split()
			results[data[0]] = string.atoi(data[1]) # project_id = [0], value = [1]
		return results
		
	def email(self, zone, quota, emailed_list):
		"""Emails users in the address list the message in msg. The list of recipiants 
		is mailed if the quota object has the EMAILED flag set to 0. 
		This method will always mail unless the EMAILED flag is set.
		"""
		# in Unix: echo "Project x is overquota in zone y" | mail -s "Message from ROOT at Nova-ab" andrew.nisbet@cybera.ca
		if emailed_list.has_key(quota.get_project_name()):
			return;
		subject = "Quota-Monitor: " + quota.get_project_name() + " over quota"
		body = "Project " + quota.get_project_name() + " is overquota: " + quota.get_exceeded()
		project_stakeholders = quota.get_contact()
		if len(project_stakeholders) == 0:
			log = QuotaLogger()
			log.error("Project %s has no stakeholder email and they are over quota!" % (quota.get_project_name()))
			return

		for contact in project_stakeholders:
			cmd = 'echo \"' + body + '\" | mail -s \"' + subject + '\" ' + contact
			self.__execute_nova__(cmd)
		
# metadata_items: 128
# gigabytes: 100
# floating_ips: 5
# instances: 5
# volumes: 5
# cores: 10

# Open the qotas for each project. This is a gold standard file since once this
# script runs the quotas are dynamically changed as over time.

class Quota:
	""" 
	This class represents a project's quotas and instances of current resource
	values within a zone. It also includes a flag to determine if the project 
	stackholders have been alerted to overquotas.
	>>> quota = Quota("binky",2,3,4,5,6,7)
	>>> print quota
	'metadata_items: 2, gigabytes: 3, floating_ips: 4, instances: 5, volumes: 6, cores: 7'
	"""
	PROJECT = 'project'
	M = 'metadata_items'
	G = 'gigabytes'
	F = 'floating_ips'
	I = 'instances'
	V = 'volumes'
	C = 'cores'
	
	def __init__(self, name, meta=0, Gb=0, f_ips=0, insts=0, vols=0, cors=0):
		self.quota = {}
		self.exceeded = []
		self.changed_quotas = [] # names of quotas to be set by nova-manage.
		self.project = name
		self.over_quota = False
		self.set_quota(Quota.M, meta)
		self.set_quota(Quota.G, Gb)
		self.set_quota(Quota.F, f_ips)
		self.set_quota(Quota.I, insts)
		self.set_quota(Quota.V, vols)
		self.set_quota(Quota.C, cors)
		
	# This function adds the values from another quota to this one.
	def add(self, quota):
		"""
		This method adds the values of quota to this quota object. It is used
		for calculating all the instances of resources within other zones.
		>>> p = Quota('not_p')
		>>> p.set_values(["project=a", "metadata_items=1","gigabytes=1"," floating_ips=1","instances=1","volumes =  1","cores=1"])
		'a'
		>>> print p
		'metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> q = Quota('q')
		>>> q.add(p)
		>>> print q
		'metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		"""
		for key in self.quota.keys():
			try:
				self.set_quota(key, (self.get_quota(key) + quota.get_quota(key)))
			except KeyError:
				pass # mismatched quotas don't get added to this object that is
					 # a quota must exist in both quota objects for it to be added
					 
	def minus(self, quota):
		"""
		Computes difference between minuend (this quota) and subtrahend (arg quota)
		and returns a new quota.
		param quota: the subtrahend quota
		return: new difference quota **can be a negative value**
		>>> p = Quota('not_a')
		>>> p.set_values(["project=a", "metadata_items=1","gigabytes=1"," floating_ips=1","instances=1","volumes =  1","cores=1"])
		'a'
		>>> print p
		'metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> q = Quota('not_b')
		>>> q.set_values(["project=b", "metadata_items=1","gigabytes=1"," floating_ips=1","instances=1","volumes =  1","cores=1"])
		'b'
		>>> print q
		'metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> r = q.minus(p)
		>>> print q
		'metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> print r
		'metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> s = r.minus(p)
		>>> print s
		'metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		"""
		return_quota = self.__clone__()
		for key in self.quota.keys():
			return_quota.set_quota(key, return_quota.get_quota(key) - quota.get_quota(key))
		
		return return_quota
		
	def __clone__(self):
		"""Returns a clone of this quota using deep copy.
		>>> a = Quota('a', 128, 100, 10, 10, 10, 20)
		>>> b = a.__clone__()
		>>> print b
		'metadata_items: 128, gigabytes: 100, floating_ips: 10, instances: 10, volumes: 10, cores: 20'
		>>> a = Quota('z', 1, 1, 1, 1, 1, 1)
		>>> print a
		'metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> print b
		'metadata_items: 128, gigabytes: 100, floating_ips: 10, instances: 10, volumes: 10, cores: 20'
		"""
		new_quota = Quota(self.get_project_name())
		for name in self.exceeded:
			new_quota.exceeded.append(name)
		for name in self.changed_quotas:
			new_quota.changed_quotas.append(name) # names of quotas to be set by nova-manage.
		new_quota.project = self.project
		new_quota.over_quota = self.over_quota
		for key in self.quota.keys():
			new_quota.set_quota(key, self.get_quota(key))
		return new_quota
		
	def set_current_values(self, values):
		"""Sets a quota to the current value
		>>> results = ('metadata_items: 128\\ngigabytes: 100\\nfloating_ips: 10\\ninstances: 10\\nvolumes: 10\\ncores: 20\\n', '')
		>>> q = Quota('test')
		>>> q.set_current_values(results)
		>>> print q
		'metadata_items: 128, gigabytes: 100, floating_ips: 10, instances: 10, volumes: 10, cores: 20'
		>>> q.set_current_values(('metadata_items: 128\\ngigabytes: 100\\nfloating_ips: 10\\ninstances: 3\\nvolumes: 10\\ncores: 20\\n', ''))
		>>> print q
		'metadata_items: 128, gigabytes: 100, floating_ips: 10, instances: 3, volumes: 10, cores: 20'
		"""
		try:
			r_str = values[0]
		except IndexError:
			log = QuotaLogger()
			log.error("failed to get quotas for " + quota.get_project_name() + " in zone " + zone)
			return # this is ok since it will compare as changed and just set all values anyway.
			
		for q in r_str.splitlines():
			test_quota = q.split(': ')
			if self.get_quota(test_quota[0]) != string.atoi(test_quota[1]):
				self.set_quota(test_quota[0], string.atoi(test_quota[1]))
	
	# reads lines from a file and if the values are '=' separated it will assign the named quota the '=' value.
	# WARNING: If the quota name is spelt incorrectly the default value is used.
	def set_values(self, values):
		""" 
		This method is used for reading in the original quota values for
		projects. This is the values that the project admins signed up for.
		parsing a string of 'name=value,' quotas.
		>>> quota = Quota('a')
		>>> print quota
		'metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> quota.set_values(["project=1003unbc","metadata_items=120","gigabytes=99"," floating_ips=30","   instances=4","	volumes =  2"," cores    =   20"])
		'1003unbc'
		>>> print quota
		'metadata_items: 120, gigabytes: 99, floating_ips: 30, instances: 4, volumes: 2, cores: 20'
		"""
		project_name = None
		for value in values:
			name = value.split('=')[0].strip()
			vs = value.split('=')[1].strip()
			if name == 'project':
				self.project = name = project_name = vs
			else:
				vs = string.atoi(vs)
				self.set_quota(name, vs) # possible to set a quota that doesn't exist by misspelling its name
			#print "name = " + name + " value = " + str(vs)
		if project_name == None or project_name == "":
			# project name not specified in file throw exception
			log = QuotaLogger()
			msg = "quotas found for a project with no name."
			log.error(msg)
			raise QuotaException(msg)
		else:
			return project_name
			
	def set_quota(self, name, value):
		""" Stores a quota value. Set mark_change to True if the quota
		should be set with nova-manage. 
		>>> q = Quota('project_a', 1)
		>>> print q
		'metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> q.get_exceeded()
		''
		>>> q.set_quota(Quota.M, -1)
		>>> q.get_exceeded()
		'metadata_items, '
		"""
		# if this quota marks a change in a quota's value that needs to be
		# updated with nova-manage then save the quota name
		try:
			if self.quota[name] != value:
				self.changed_quotas.append(name)
		except KeyError:
			pass
		# Mark as an exceeded quota for reporting purposes.
		if value < 0:
			if self.exceeded.__contains__(name) == False:
				self.exceeded.append(name)
			self.quota[name] = 0
			self.over_quota = True
		else:
			self.quota[name] = value
		
	def get_quota(self, name):
		""" Returns a quota value or 0 if not found. """
		try:
			return self.quota[name]
		except KeyError:
			return 0
		
	def __str__(self):
		return repr("metadata_items: %d, gigabytes: %d, floating_ips: %d, instances: %d, volumes: %d, cores: %d" % \
		(self.quota[Quota.M],
		self.quota[Quota.G],
		self.quota[Quota.F],
		self.quota[Quota.I],
		self.quota[Quota.V],
		self.quota[Quota.C]))
		
	def is_over_quota(self):
		"""Returns True if the quota is over quota, that is the quota has a
		negative value.
		>>> p = Quota('a',2)
		>>> q = p.__clone__()
		>>> print q
		'metadata_items: 2, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> r = p.minus(q)
		>>> print r.is_over_quota()
		False
		>>> print r
		'metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> print q.is_over_quota()
		False
		>>> s = r.minus(q)
		>>> print s
		'metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> print s.is_over_quota()
		True
		"""
		# if the over quota flag was set don't go any farther it was over quota at some time
		if self.over_quota:
			return True
		# if not then check if any of the quotas are negative.
		for key in self.quota.keys():
			if self.quota[key] < 0: # accounts for normalized quota
				return True
		return False
		
	def get_exceeded(self):
		"""Returns a list of quotas that have been exceeded. 
		>>> q = Quota('a', -1, 0)
		>>> print q.get_exceeded()
		metadata_items, 
		>>> q = Quota('a', -1, -10)
		>>> print q.get_exceeded()
		metadata_items, gigabytes, 
		"""
		return_str = ""
		for value in self.exceeded:
			return_str += value + ", "
		return return_str
		
	def get_project_name(self):
		"""Returns the name of the project that this quota belongs to."""
		return self.project.split()[0]
		
	def get_contact(self):
		"""Returns a list of contacts that is stored in the project filed of the config file.
		>>> q = Quota('proj_a andrew.nisbet@cybera.ca foo@bar.ca', 1)
		>>> print q.get_contact()
		['andrew.nisbet@cybera.ca', 'foo@bar.ca']
		"""
		contacts = self.project.split()
		return_list = []
		for contact in contacts:
			if contact.find('@') > 0:
				return_list.append(contact.strip())
		return return_list
		
	def get_changed_quotas(self, is_baseline=False):
		"""Returns the changed quotas.
		>>> q = Quota('q')
		>>> print q.get_changed_quotas()
		[]
		>>> q.set_quota(Quota.C, 10)
		>>> print q.get_changed_quotas()
		['cores']
		>>> print q.get_changed_quotas(True) # is a baseline quota
		['cores', 'metadata_items', 'gigabytes', 'floating_ips', 'instances', 'volumes']
		"""
		if is_baseline == True:
			for key in self.quota.keys():
				if self.changed_quotas.__contains__(key):
					continue
				self.changed_quotas.append(key)
		return self.changed_quotas
		
		
# The baseline quota file is made up of lines of quotas where the minimum entry
# is the project name. There is no maximum entry so future quotas can be added
# with no impact to reading the file, unrecognized quotas are ignored, those
# that are not mentioned are set to the OpenStack defaults set by compute--
# project-create.sh. The format of the file is a single line per project with
# name value pairs separated by ','. Trailing ',' are not permitted. Examples:
# name_0=value_0, name_1=value_1
# Blank lines are allowed and the comment character is '#'. 
#
# Valid entries tested include:
# project=a,flags=1,metadata_items=120,gigabytes=99, floating_ips=9,   instances=4,	volumes =  2, cores    =   20
# flags=0,metadata_items=121,gigabytes=10, floating_ips=10,   instances=5,	volumes =4, cores=7,project=z
# cores=999, project=p
def read_baseline_quota_file(file_name=GSTD_QUOTA_FILE):
	"""Reads in the base quota file."""
	quotas = {}
	try:
		f = open(file_name)
	except:
		log = QuotaLogger()
		msg = "No initial project quota values defined for projects. There should be a file here called %s." % (GSTD_QUOTA_FILE)
		log.error(msg)
		raise QuotaException(msg)
	
	line_no = 0
	for line in f.readlines():
		if line.strip() != "" and line[0] != "#":
			read_values = line.split(',') # read_values holds name=value pairs.
			line_no = line_no + 1
			# preset the quota values to defaults flags=0, meta=128, Gb=100, f_ips=10, insts=10, vols=10, cors=20.
			project_quota = Quota('<none>', 128, 100, 10, 10, 10, 20)
			name = None
			try:
				name = project_quota.set_values(read_values)
			except:
				log = QuotaLogger()
				msg = "Malformed quota file on line %d" % (line_no)
				log.error(msg)
				raise QuotaException(msg)
			# save the project's quotas from file
			quotas[project_quota.get_project_name()] = project_quota
	return quotas
	
def read_emailed_list_file(file_name=DELINQUENT_FILE):
	"""Reads the list of projects that have received emails already. If it can't find the file
	it returns an empty list and everyone overquota will get another email.
	>>> f = open('test.tmp', 'w')
	>>> f.write("a b c\\n# shouldn't find this\\nd")
	>>> f.close()
	>>> read_emailed_list_file('test.tmp') # this can fail because it is a hash and there is no guarantee of order.
	{'a': 1, 'c': 1, 'b': 1, 'd': 1}
	>>> os.remove('test.tmp')
	>>> f = open('test.tmp', 'w')
	>>> f.write("project_a")
	>>> f.close()
	>>> read_emailed_list_file('test.tmp')
	{'project_a': 1}
	>>> os.remove('test.tmp')
	>>> f = open('test.tmp', 'w')
	>>> f.write("")
	>>> f.close()
	>>> read_emailed_list_file('test.tmp')
	{}
	>>> os.remove('test.tmp')
	"""
	delinquent_projects = {}
	try:
		f = open(file_name)
	except:
		return {} # no file or could not be opened, every new overquota will get an email.
	
	for line in f.readlines():
		if line.strip() != "" and line[0] != "#": # empty lines # are commented lines.
			for each_name in line.strip().split():
				delinquent_projects[each_name] = 1
				
	return delinquent_projects
	
def write_emailed_list(emailed_dict, file_name=DELINQUENT_FILE):
	"""Writes the list of projects that have received emails already.
	>>> write_emailed_list({'a': 1, 'b': 1, 'c': 1, 'd': 1}, 'test.tmp')
	>>> read_emailed_list_file('test.tmp') # this can fail because it is a hash and there is no guarantee of order.
	{'a': 1, 'c': 1, 'b': 1, 'd': 1}
	>>> write_emailed_list({'project_a': 1}, 'test.tmp')
	>>> read_emailed_list_file('test.tmp') # this can fail because it is a hash and there is no guarantee of order.
	{'project_a': 1}
	>>> write_emailed_list({}, 'test.tmp')
	>>> read_emailed_list_file('test.tmp') # this can fail because it is a hash and there is no guarantee of order.
	{}
	>>> os.remove('test.tmp')
	"""
	try:
		f = open(file_name, 'w', 0)
		for key in emailed_dict.keys():
			f.write(key + " ")
		f.close()
	except Exception as inst:
		log = QuotaLogger()
		log.error("failed to get write list of delinquent projects " + type(inst))
		
def update_emailed_list(emailed_overquota_projects, quota):
	"""Function updates the dictionary of emailed users with any quotas that have gone over.
	>>> elist = {}
	>>> b = Quota( 'project_b', 0, 0, 0, 0, 0, 0)
	>>> update_emailed_list(elist, b)
	>>> print elist
	{}
	>>> b = Quota( 'project_b', -1, 0, 0, 0, 0, 0) # quota was normalized
	>>> update_emailed_list(elist, b)
	>>> print elist
	{'project_b': 1}
	>>> a = Quota('project_a', -10, 0, 0, 0, 0, 0)
	>>> print a.is_over_quota()
	True
	>>> b = Quota( 'project_b', -1, 0, 0, 0, 0, 0)
	>>> update_emailed_list(elist, a)
	>>> print elist
	{'project_b': 1, 'project_a': 1}
	>>> update_emailed_list(elist, b)
	>>> print elist
	{'project_b': 1, 'project_a': 1}
	>>> a = Quota('project_a', 10, 0, 0, 0, 0, 0) # back under quota...
	>>> update_emailed_list(elist, a)
	>>> print elist
	{'project_b': 1}
	>>> b = Quota('project_b', 1, 0, 0, 0, 0, 0) # back under quota...
	>>> update_emailed_list(elist, b)
	>>> print elist
	{}
	"""

	# if the project is over quota add it to the list.
	if quota.is_over_quota():
		emailed_overquota_projects[quota.get_project_name()] = 1
	else:
		# note that when they go over again they will get a new email.
		try:
			del emailed_overquota_projects[quota.get_project_name()]
		except KeyError:
			pass # there was no key so you can't delete it
		
# There has to be a way to reset the quotas to the baseline for all groups 
# in the case that there is a problem and the quotas get out of synch.
# This method does that.
# param: quotas = {'project': Quota}
def reset_quotas():
	""" 
	Reads the gold standard project quotas for projects and sets those
	values in each zone. This function does not balance zone quotas.
	"""
	print "resetting quotas..."
	baseline_quotas = read_baseline_quota_file()
	zoneManager = ZoneQueryManager()
	for zone in zoneManager.get_zones():
		print "zone: " + zone
		for project in baseline_quotas.keys():
			zoneManager.set_quota(zone, baseline_quotas[project], None)
	return 0
	
def balance_quotas():
	"""
	Balances quotas. Run from cron this function runs once per preset time period.
	The formula is: 
		Qnow = Qbaseline - Iother_zones
	where
	Qnow: the quota for a specific but arbitrary zone at this cycle
	Qbaseline: the quota for the project assigned when the project was created
	Iother: the number of instances of a resource being consumed in all other zones.
	"""
	zoneManager = ZoneQueryManager() # this will now contain the regions and sql password
	baseline_quotas = read_baseline_quota_file()
	emailed_overquota_projects = read_emailed_list_file()
	zoneManager.get_zone_resource_snapshots()
	for zone in zoneManager.get_zones():
		other_zones_resources = zoneManager.get_other_zones_current_resources(zone)
		for project in baseline_quotas.keys():
			# if the zone doesn't have a project by that name returns a zero-ed quota
			resources = other_zones_resources.get_resources(project)
			new_quota = baseline_quotas[project].minus(other_zones_resources.get_resources(project))
			zoneManager.set_quota(zone, baseline_quotas[project], new_quota)
			my_resources = zoneManager.get_my_zones_resources(zone, project)
			total_usage = new_quota.minus(my_resources)
			if total_usage.is_over_quota():
				zoneManager.email(zone, total_usage, emailed_overquota_projects)
				# this stops the user from getting emails every time the quota monitor runs.
				update_emailed_list(emailed_overquota_projects, total_usage)
	write_emailed_list(emailed_overquota_projects)
	return 0

def usage():
	return """
Usage: quota-monitor.py [-a, --audit] -[b|h|r], --help, --reset, --balance

    This script is meant to be run routinely by cron to check and 
    balance quotas over zones in OpenStack.
    
    In the DAIR environment quotas are assigned at project creation
    but were not being propagated to other zones correctly. Worse 
    zones were getting the same values as the values set in the 
    management node's zone. This seems reasonable at first because
    a customer in any zone should have access to the full quota of
    VMs that they paid for. The problem is that as you instantiate
    a VM in one zone, people in other zones think they can start
    their full quota, not n -1 instances. To remedy that this 
    script makes note of what each project should have as resources
    then periodically queries how many instances are actually being
    used in different zones, then reduces the quota total for the 
    other zones and reports if the reduced quota falls below zero.
    
    -h, --help prints this message and exits with status 1.
    -r, --reset resets the quotas to the original values set in the
		./baseline_quotas.txt file.
    -b, --balance balances the quotas over the projects by zone.
    -a, --audit compares before and after quota setting and if the
		two results are the same writes an error message to log.
		This functionality takes longer to perform.
"""
	
class QuotaException:
	def __init__(self, msg):
		self.msg = msg
	def __str__(self):
		return repr(self.msg)


def main():
    # parse command line options
	try:
		opts, args = getopt.getopt(sys.argv[1:], "ahrb", ["audit", "help","reset","balance"])
	except getopt.GetoptError, err:
		print str(err)
		print usage()
		sys.exit(2)

	
	for o, a in opts:
		if o in ("-h", "--help"):
			print usage()
			return 1
		elif o in ("-r", "--reset"):
			return reset_quotas()
		elif o in ("-b", "--balance"):
			return balance_quotas()
		elif o in ("-a", "--audit"):
			print "setting auditing on."
			AUDIT = True
		else:
			assert False, "unimplemented option '" + o + "'"


if __name__ == "__main__":
	#import doctest
	#doctest.testmod()
	sys.exit(main())
