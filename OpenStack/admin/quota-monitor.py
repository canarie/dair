#! /usr/bin/env python

#################################################################
# File:     quota-monitor.py
# Purpose:  Aggregate and balance quotas over zones in OpenStack 
# Author:   Andrew Nisbet andrew.nisbet@cybera.ca
# Date:     October 3, 2011
# Version:  0.1 - Dev. Not ready for initial release. 
#################################################################

import os			# for getcwd()
import sys
import getopt       # Command line processing.
import string		# for atoi()
import subprocess	# for __execute_call__()
import logging		# for logging
import os.path		# for file testing.

APP_DIR = '/home/cybera/dev/dair/OpenStack/admin/'
GSTD_QUOTA_FILE = APP_DIR + "baseline_quotas.txt" # Gold standard quotas for baseline.
DELINQUENT_FILE = APP_DIR + "Quota-monitor_scratch.tmp" # list of delinquent projects that HAVE been emailed.
### PRODUCTION CODE ###
NOVA_CONF = "/home/cybera/dev/nova.conf" # nova.conf -- change for production.


class QuotaLogger:
	"""Logs events of interest.
	>>> """
	#LOG_FILE = "/var/log/dair/quota-monitor.log" # log file
	LOG_FILE = "quota-monitor.log" # log file
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
		'flags: 0, metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		"""
		for project in project_count:
			project_instance = Quota(project)
			try:
				project_instance = self.instances[project]
			except KeyError: # no instances for this project logged yet.
				pass
			project_instance.set_quota(which_quota, project_count[project])
			self.instances[project] = project_instance
			#print project_instance
			
	# this method aggregates the argument zone_instance's values with this one.
	def __sum__(self, other_zone):
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
			my_quota.__add__(other_zone.get_resources(project))
			self.instances[project] = my_quota
			
	def get_projects(self):
		"""Returns the running instances of a given project name or an empty quota
		object if the project doesn't exist.
		>>> zi = ZoneInstance()
		>>> pc = {}
		>>> pc['a'] = 1
		>>> pc['b'] = 2
		>>> zi.set_instance_count_per_project(Quota.M, pc)
		>>> print zi.get_projects()
		['a', 'b']
		"""
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
		'flags: 0, metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> quota = zi.get_resources('no_project')
		>>> print quota
		'flags: 0, metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
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
		self.Q_PROJECT_INSTANCES = """ "select project_id, count(state) from instances where state=1 group by project_id;" """
		self.Q_PROJECT_GIGABYTES = """ "select project_id, sum(size) from volumes where attach_status=\\"attached\\" group by project_id;" """ # returns empty set not expected '0'
		self.Q_PROJECT_VOLUMES   = """ "select project_id, count(size) from volumes where attach_status=\\"attached\\" group by project_id;" """
		self.Q_PROJECT_FLOAT_IPS = """ "select project_id, count(deleted) from floating_ips where deleted=0 group by project_id;" """
		self.Q_PROJECT_CPUS      = """ "select project_id, sum(vcpus) from instances where state=1 group by project_id;" """
		self.regions = {} # {'full_name' = ip}
		self.instances = {} # {'full_name' = ZoneInstance}
		self.password = None
		# this requires two greps of the nova.conf file but could be done in one.
		# get the regions like: --region_list=alberta=208.75.74.10,quebec=208.75.75.10
		results = self.__execute_call__("grep region_list " + NOVA_CONF)
		results = results.split('region_list=')[1]
		# now split on the regions separated by a ','
		results = results.split(',')
		for result in results:
			name_value = result.split('=')
			self.regions[name_value[0].strip()] = name_value[1].strip() # gets rid of nagging newline
		# now the password --sql_connection=mysql://root:xxxxxxxxxxxxx@192.168.2.10/nova
		results = self.__execute_call__("grep sql_connection " + NOVA_CONF)
		self.password = results.split('root:')[1].split('@')[0] # yuck.
		#print self.password, self.regions
		
	def __execute_call__(self, command_and_args):
		""" returns the stdout of a Unix command """
		cmd = command_and_args.split()
		if len(cmd) < 1:
			return "<no cmd to execute>"
		process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		return process.communicate()[0]
		
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
		# test data
		cmd_result = """"""
		if zone == 'quebec':
			cmd_result = """
			project_id	sum(size)
			1003unbc	30
			project_a	99
			"""
		else: # zone == 'ab':
			cmd_result = """project_id	sum(size)
			project_d	199
			1003unbc	10
			"""
		### PRODUCTION CODE ###
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_GIGABYTES + sql_cmd_suffix
		#cmd_result = __execute_call__(ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_GIGABYTES + sql_cmd_suffix)
		result_dict = self.__parse_query_result__(cmd_result)
		zone_project_instances.set_instance_count_per_project(Quota.G, result_dict)
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_FLOAT_IPS + sql_cmd_suffix
		zone_project_instances.set_instance_count_per_project(Quota.F, result_dict)
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_INSTANCES + sql_cmd_suffix
		zone_project_instances.set_instance_count_per_project(Quota.I, result_dict)
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_VOLUMES + sql_cmd_suffix
		zone_project_instances.set_instance_count_per_project(Quota.V, result_dict)
		#print ssh_cmd + sql_cmd_prefix + self.Q_PROJECT_CPUS + sql_cmd_suffix
		zone_project_instances.set_instance_count_per_project(Quota.C, result_dict)
		
	def connection_is_open(self):
		return True
		
	def set_quota(self, zone, quota):
		"""Sets a quota for a project in a secific zone."""
		address = ""
		try:
			address = self.regions[zone]
		except KeyError:
			log = QuotaLogger()
			msg = "ZoneQueryManager.set_quota failed because no zone: '" + zone + "'"
			log.error(msg)
			return
		# ssh -o StrictHostKeyChecking=no ADDRESS "nova-manage project quota PROJECT gigabytes QUOTA_GIGABYTES"
		# fl = 'flags'
		# G = 'gigabytes'
		# F = 'floating_ips'
		# I = 'instances'
		# V = 'volumes'
		# C = 'cores'
		# M = 'metadata_items'
		euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + Quota.C + " " + str(quota.get_quota(Quota.C)) + "\""
		print euca_cmd #self.__execute_call__(euca_cmd)
		euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + Quota.F + " " + str(quota.get_quota(Quota.F)) + "\""
		print euca_cmd #self.__execute_call__(euca_cmd)
		euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + Quota.G + " " + str(quota.get_quota(Quota.G)) + "\""
		print euca_cmd #self.__execute_call__(euca_cmd)
		euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + Quota.I + " " + str(quota.get_quota(Quota.I)) + "\""
		print euca_cmd #self.__execute_call__(euca_cmd)
		euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + Quota.M + " " + str(quota.get_quota(Quota.M)) + "\""
		print euca_cmd #self.__execute_call__(euca_cmd)
		euca_cmd = 'ssh -o StrictHostKeyChecking=no ' + address + " \"nova-manage project quota " + Quota.V + " " + str(quota.get_quota(Quota.V)) + "\""
		print euca_cmd #self.__execute_call__(euca_cmd)
		
		
	def get_zones(self):
		""" Returns the names of the regions collected from nova.conf. """
		return self.regions.keys()
		
	def get_other_zones_current_resources(self, zone):
		"""
		Returns a ZoneInstance object of all projects in zones other
		than the named zone, and including project resource usage.
		"""
		#other_zones_resources = zoneManager.get_other_zones_current_resources(zone)
		other_zones_resources = ZoneInstance()
		for z in self.regions.keys():
			if z == zone:
				continue
			else:
				other_zones_resources.__sum__(self.instances[z]) # add the project quotas from the other snapshot(s)
		return other_zones_resources
		
	def compute_zone_quotas(self, baseline_quotas, other_zones_resources):
		"""
		Given a zone and baseline quotas for a project returns baseline_quotas 
		ZoneInstance object with modified quota values.
		param zone: the target zone to set quotas for
		param baseline_quotas: the quotas a project is entitled to, will be 
		modified to new permitted quotas for a zone.
		param other_zone_resources: a ZoneInstance with all the other zones in-use
		resources aggregated.
		return: ZoneInstance object with new quotas.
		>>> zqm = ZoneQueryManager()
		>>> table = "project_id	sum(size)\\nproject_b	2\\nproject_a	9\\n"
		>>> resources = zqm.__parse_query_result__(table)
		>>> other_zi = ZoneInstance()
		>>> other_zi.set_instance_count_per_project(Quota.M, resources)
		>>> quotas = {}
		>>> quota_a = Quota('project_a', 0, 10, 0, 0, 0, 0, 0);
		>>> quota_b = Quota( 'project_b', 0, 1, 0, 0, 0, 0, 0);
		>>> quotas['project_a'] = quota_a
		>>> print quota_a
		'flags: 0, metadata_items: 10, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> quotas['project_b'] = quota_b
		>>> new_quotas = zqm.compute_zone_quotas(quotas, other_zi)
		>>> print new_quotas['project_a']
		'flags: 0, metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> print new_quotas['project_b'] # project_b is over quota but don't set to a negative value.
		'flags: 1, metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		"""
		new_quotas = {}
		for project in baseline_quotas.keys():
			# if the zone doesn't have a project by that name returns a zero-ed quota.
			resources = other_zones_resources.get_resources(project)
			# for each project in this zone subtract the projects total instances
			new_quota = baseline_quotas[project].__minus__(resources)
			if new_quota.is_over_quota():
				# Don't set a quota to a negative value but signal to email.
				new_quota.__normalize__()
			# add the new quotas for this project to the return dictionary.
			new_quotas[project] = new_quota
		return new_quotas
		
	def __parse_query_result__(self, table):
		""" 
		Takes a table output on stdout and returns it as a dictionary of 
		results project=value. 
		>>> zqm = ZoneQueryManager()
		>>> table = "project_id	sum(size)\\n1003unbc	30\\nproject_a	99\\n\\n\\n"
		>>> r = zqm.__parse_query_result__(table)
		>>> print r['1003unbc']
		30
		>>> print r['project_a']
		99
		>>> table = "project_id	sum(size)\\n1003unbc	0\\nproject_a	1"
		>>> r = zqm.__parse_query_result__(table)
		>>> print r['1003unbc']
		0
		>>> print r['project_a']
		1
		>>> table = "project_id	sum(size)"
		>>> r = zqm.__parse_query_result__(table)
		>>> print r
		{}
		>>> table = ""
		>>> r = zqm.__parse_query_result__(table)
		>>> print r
		{}
		"""
		results = {}
		if len(table) < 1: # empty set test -- naive test fix me
			return results
		clean_table = table.strip()
		rows = clean_table.splitlines()
		# remove the first row which is the column titles from the query
		# or possibly an empty set message.
		rows.__delitem__(0)
		for row in rows:
			data = row.split()
			if len(data) == 2:
				results[data[0]] = string.atoi(data[1]) # project_id = [0], value = [1]
		return results
		
	def email(self, zone, quota):
		"""Emails users in the address list the message in msg. The list of recipiants 
		is mailed if the quota object has the email flag set to 1 then it resets it.
		>>> zqm = ZoneQueryManager()
		>>> quota = Quota('bloaded andrew.nisbet@cybera.ca', 1, -1) # over quota and sent email flag up.
		>>> quota.is_over_quota()
		True
		>>> zqm.email('alberta', quota)
		False
		>>> quota = Quota('under_quota andrew.nisbet@cybera.ca', 0, -1) # over quota.
		>>> zqm.email('alberta', quota)
		True
		>>> zqm.email('alberta', quota) # over quota but email sent already.
		False
		>>> quota = Quota('project_b', -1, 22) # over quota but no one to send to.
		>>> zqm.email('alberta', quota)
		False
		"""
		# in Unix: echo "Project x is overquota in zone y" | mail -s "Message from ROOT at Nova-ab" andrew.nisbet@cybera.ca
		subject = "Quota-Monitor: " + quota.get_project_name() + " over quota"
		body = "Project " + quota.get_project_name() + " is overquota in zone " + zone 
		project_stakeholders = quota.get_contact()
		if len(project_stakeholders) == 0:
			return False
		if quota.get_quota(Quota.fl) & Quota.EMAILED == 0: # The contacts haven't been emailed yet.
			for contact in project_stakeholders:
				cmd = 'echo \"' + body + '\" | mail -s \"' + body + '\" ' + contact
			quota.set_quota(Quota.fl, Quota.EMAILED)
			#print cmd
			### PRODUCTION CODE ###
			#self.__execute_call__(cmd)
			return True
		else:
			return False
		
		
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
	>>> quota = Quota("binky", 1,2,3,4,5,6,7)
	>>> print quota
	'flags: 1, metadata_items: 2, gigabytes: 3, floating_ips: 4, instances: 5, volumes: 6, cores: 7'
	"""
	PROJECT = 'project'
	fl = 'flags'
	M = 'metadata_items'
	G = 'gigabytes'
	F = 'floating_ips'
	I = 'instances'
	V = 'volumes'
	C = 'cores'
	EMAILED = 1
	
	def __init__(self, name, flags=0, meta=0, Gb=0, f_ips=0, insts=0, vols=0, cors=0):
		self.quota = {}
		self.exceeded = []
		self.project = name
		self.set_quota(Quota.fl, flags) # flags is currently if we have issued an email yet
		self.set_quota(Quota.M, meta)
		self.set_quota(Quota.G, Gb)
		self.set_quota(Quota.F, f_ips)
		self.set_quota(Quota.I, insts)
		self.set_quota(Quota.V, vols)
		self.set_quota(Quota.C, cors)
		
	# This function adds the values from another quota to this one.
	def __add__(self, quota):
		"""
		This method adds the values of quota to this quota object. It is used
		for calculating all the instances of resources within other zones.
		>>> p = Quota('not_p')
		>>> p.set_values(["project=a","flags=1","metadata_items=1","gigabytes=1"," floating_ips=1","instances=1","volumes =  1","cores=1"])
		'a'
		>>> print p
		'flags: 1, metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> q = Quota('q')
		>>> q.__add__(p)
		>>> print q
		'flags: 1, metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		"""
		for key in self.quota.keys():
			try:
				self.set_quota(key, (self.get_quota(key) + quota.get_quota(key)))
			except KeyError:
				pass # mismatched quotas don't get added to this object that is
					 # a quota must exist in both quota objects for it to be added
					 
	def __minus__(self, quota):
		"""
		Computes difference between minuend (this quota) and subtrahend (arg quota)
		and returns a new quota.
		param quota: the subtrahend quota
		return: new difference quota **can be a negative value**
		>>> p = Quota('not_a')
		>>> p.set_values(["project=a","flags=1","metadata_items=1","gigabytes=1"," floating_ips=1","instances=1","volumes =  1","cores=1"])
		'a'
		>>> print p
		'flags: 1, metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> q = Quota('not_b')
		>>> q.set_values(["project=b","flags=1","metadata_items=1","gigabytes=1"," floating_ips=1","instances=1","volumes =  1","cores=1"])
		'b'
		>>> print q
		'flags: 1, metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> r = q.__minus__(p)
		>>> print q
		'flags: 1, metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> print r
		'flags: 0, metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> s = r.__minus__(p)
		>>> print s
		'flags: -1, metadata_items: -1, gigabytes: -1, floating_ips: -1, instances: -1, volumes: -1, cores: -1'
		"""
		return_quota = self.__clone__()
		for key in self.quota.keys():
			return_quota.set_quota(key, return_quota.get_quota(key) - quota.get_quota(key)) # this can be a neg value.
		return return_quota
		
	def __clone__(self):
		"""Returns a clone of this quota using deep copy.
		>>> a = Quota('a', 0, 128, 100, 10, 10, 10, 20)
		>>> b = a.__clone__()
		>>> print b
		'flags: 0, metadata_items: 128, gigabytes: 100, floating_ips: 10, instances: 10, volumes: 10, cores: 20'
		>>> a = Quota('z', 0, 1, 1, 1, 1, 1, 1)
		>>> print a
		'flags: 0, metadata_items: 1, gigabytes: 1, floating_ips: 1, instances: 1, volumes: 1, cores: 1'
		>>> print b
		'flags: 0, metadata_items: 128, gigabytes: 100, floating_ips: 10, instances: 10, volumes: 10, cores: 20'
		"""
		new_quota = Quota(self.get_project_name())
		for key in self.quota.keys():
			new_quota.set_quota(key, self.get_quota(key))
		return new_quota
	
	# reads lines from a file and if the values are '=' separated it will assign the named quota the '=' value.
	# WARNING: If the quota name is spelt incorrectly the default value is used.
	def set_values(self, values):
		""" 
		This method is used for reading in the original quota values for
		projects. This is the values that the project admins signed up for.
		parsing a string of 'name=value,' quotas.
		>>> quota = Quota('a')
		>>> print quota
		'flags: 0, metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> quota.set_values(["project=1003unbc","flags=1","metadata_items=120","gigabytes=99"," floating_ips=30","   instances=4","	volumes =  2"," cores    =   20"])
		'1003unbc'
		>>> print quota
		'flags: 1, metadata_items: 120, gigabytes: 99, floating_ips: 30, instances: 4, volumes: 2, cores: 20'
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
		""" Stores a quota value. 
		>>> q = Quota('project_a', 0, 1)
		>>> print q
		'flags: 0, metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> q.get_exceeded()
		[]
		>>> q.set_quota(Quota.M, -1)
		>>> q.get_exceeded()
		['metadata_items']
		"""
		if value < 0 and self.exceeded.__contains__(name) == False:
			self.exceeded.append(name)
		self.quota[name] = value
		
	def get_quota(self, name):
		""" Returns a quota value or 0 if not found. """
		try:
			return self.quota[name]
		except KeyError:
			return 0
			
	# set negative quotas to zero and set the email flag to one.
	def __normalize__(self):
		"""Set any negative quotas to zero and set the email flag on the quota to 1.
		>>> quota_good = Quota('good', 0,1)
		>>> print quota_good
		'flags: 0, metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> quota_good.__normalize__()
		>>> print quota_good
		'flags: 0, metadata_items: 1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> quota_bad = Quota('bad', 0,-1)
		>>> print quota_bad
		'flags: 0, metadata_items: -1, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> quota_bad.__normalize__()
		>>> print quota_bad
		'flags: 1, metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		"""
		for key in self.quota.keys():
			if self.quota[key] < 0:
				self.set_quota(key, 0)
				self.set_quota(Quota.fl, (self.get_quota(Quota.fl) | Quota.EMAILED))
		
	def __str__(self):
		return repr("flags: %d, metadata_items: %d, gigabytes: %d, floating_ips: %d, instances: %d, volumes: %d, cores: %d" % \
		(self.quota[Quota.fl],
		self.quota[Quota.M],
		self.quota[Quota.G],
		self.quota[Quota.F],
		self.quota[Quota.I],
		self.quota[Quota.V],
		self.quota[Quota.C]))
		
	def is_over_quota(self):
		"""Returns True if the quota is over quota, that is the quota has a
		negative value.
		>>> p = Quota('a',0,2)
		>>> q = p.__clone__()
		>>> print q
		'flags: 0, metadata_items: 2, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> r = p.__minus__(q)
		>>> print r.is_over_quota()
		False
		>>> print r
		'flags: 0, metadata_items: 0, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> print q.is_over_quota()
		False
		>>> s = r.__minus__(q)
		>>> print s
		'flags: 0, metadata_items: -2, gigabytes: 0, floating_ips: 0, instances: 0, volumes: 0, cores: 0'
		>>> print s.is_over_quota()
		True
		>>> s.__normalize__()
		>>> print s.is_over_quota()
		True
		"""
		for key in self.quota.keys():
			if self.quota[key] < 0 or self.quota[Quota.fl] & Quota.EMAILED == 1: # accounts for normalized quota
				return True
		return False
		
	def get_exceeded(self):
		"""Returns a list of quotas that have been exceeded. 
		>>> q = Quota('a',0, -1, 0)
		>>> print q.get_exceeded()
		['metadata_items']
		"""
		return self.exceeded
		
	def get_project_name(self):
		"""Returns the name of the project that this quota belongs to.
		>>> q = Quota('binky')
		>>> print q.get_project_name()
		binky
		"""
		return self.project.split()[0]
		
	def get_contact(self):
		"""Returns a list of contacts that is stored in the project filed of the config file.
		>>> q = Quota('proj_a andrew.nisbet@cybera.ca foo@bar.ca', 0, 1)
		>>> print q.get_contact()
		['andrew.nisbet@cybera.ca', 'foo@bar.ca']
		"""
		contacts = self.project.split()
		return_list = []
		for contact in contacts:
			if contact.find('@') > 0:
				return_list.append(contact.strip())
		return return_list
		
		
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
def read_baseline_quota_file():
	quotas = {}
	try:
		f = open(GSTD_QUOTA_FILE)
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
			project_quota = Quota('<none>', 0, 128, 100, 10, 10, 10, 20)
			name = None
			try:
				name = project_quota.set_values(read_values)
			except:
				log = QuotaLogger()
				msg = "Malformed quota file on line %d" % (line_no)
				log.error(msg)
				raise QuotaException(msg)
			# save the project's quotas from file
			quotas[name] = project_quota
		#print quotas
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
		return {} # no file or could not be opened.
	
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
		f = open(file_name, 'w')
		for key in emailed_dict:
			f.write(key + " ")
		f.close()
	except:
		pass
	finally:
		f.close()
		
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
	quotas = read_baseline_quota_file()
	zoneManager = ZoneQueryManager()
	for zone in zoneManager.get_zones():
		zoneManager.set_quotas(zone, quotas) # all zones get the same project quota.
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
	quotas = read_baseline_quota_file()
	# set the quotas email flag. This is to stop over-quota projects from getting spam.
	# deleting this file is not dangerous and will resend email to user's that are over-quota.
	emailed_overquota_projects = read_emailed_list_file()
	zoneManager.get_zone_resource_snapshots()
	for zone in zoneManager.get_zones():
		other_zones_resources = zoneManager.get_other_zones_current_resources(zone)
		#print "I am zone " + zone
		new_quotas = zoneManager.compute_zone_quotas(quotas, other_zones_resources)
		for project in new_quotas.keys():
			zoneManager.set_quota(zone, new_quotas[project])
			if new_quotas[project].is_over_quota():
				zoneManager.email(zone, new_quotas[project])
		#update_emailed_list(emailed_overquota_projects, new_quotas)
	write_emailed_list(emailed_overquota_projects)
	return 0

def usage():
	return """
Usage: quota-monitor.py [-hr, --help, --reset]

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
"""
	
class QuotaException:
	def __init__(self, msg):
		self.msg = msg
	def __str__(self):
		return repr(self.msg)


def main():
    # parse command line options
	try:
		opts, args = getopt.getopt(sys.argv[1:], "hrb", ["help","reset","balance"])
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
		else:
			assert False, "unimplemented option '" + o + "'"


if __name__ == "__main__":
	import doctest
	doctest.testmod()
	sys.exit(main())
