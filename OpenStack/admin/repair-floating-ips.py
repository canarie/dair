#!/usr/bin/env python
"""
TODO: COMMENT
"""
import os
import os.path
import time
import logging
from urlparse import urlparse

import boto
import boto.ec2

import utils

EC2_ACCESS_KEY = os.getenv("EC2_ACCESS_KEY")
EC2_SECRET_KEY = os.getenv("EC2_SECRET_KEY")
EC2_URL = os.getenv("EC2_URL")

LIST = "iptables -t nat --line-numbers -n -L"
DEL = "iptables -t nat -D"
PREROUTING = "nova-network-PREROUTING"
OUTPUT = "nova-network-OUTPUT"
SNAT = "nova-network-floating-snat"

if not (EC2_ACCESS_KEY and EC2_SECRET_KEY and EC2_URL):
    print "No cloud credentials found.  Recommend you source your rc file."
    exit(1)

logger = logging.getLogger('repair-floating-ips')
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler('/var/log/dair/repair-floating-ips.log')
fh.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)

ec2_url_parsed = urlparse(EC2_URL)
novaRegion=boto.ec2.regioninfo.RegionInfo(name = "nova", endpoint = ec2_url_parsed.hostname)
conn = boto.connect_ec2(aws_access_key_id=EC2_ACCESS_KEY,
                        aws_secret_access_key=EC2_SECRET_KEY,
                        is_secure=True,
                        region=novaRegion,
                        port=ec2_url_parsed.port,
                        path=ec2_url_parsed.path)

reservations = conn.get_all_instances()
instance_floating_ips = set()

for reservation in reservations:
    for instance in reservation.instances:
        if not instance.public_dns_name in instance_floating_ips:
            instance_floating_ips.add(instance.public_dns_name)
        else:
            message = 'Instance Floating IP assigned to more than one instance!'
            logger.error(message)
            raise Exception(message)

prerouting_lines = utils.execute("%(LIST)s %(PREROUTING)s" % locals())[0]
prerouting_lines = prerouting_lines.splitlines()[2:] # skip the first two non-data lines 

prerouting_floating_ips = set()
corrupt_floating_ips = set()
ignore_floating_ips = set()
ignore_floating_ips.add(ec2_url_parsed.hostname)
ignore_floating_ips.add('169.254.169.254')

for prerouting_line in prerouting_lines:
    dnat_floating_ip = prerouting_line.split()[5]

    if dnat_floating_ip in ignore_floating_ips:
        continue
    elif dnat_floating_ip in prerouting_floating_ips:
        corrupt_floating_ips.add(dnat_floating_ip)
    else:
        prerouting_floating_ips.add(dnat_floating_ip)

corrupt_floating_ips = corrupt_floating_ips.union(prerouting_floating_ips.difference(instance_floating_ips))

if not corrupt_floating_ips:
    logger.info("No Floating IPs to repair")
else:
    for floating_ip in corrupt_floating_ips:
        prerouting_lines = utils.execute("%(LIST)s %(PREROUTING)s | grep %(floating_ip)s" % locals())[0]
        prerouting_lines = prerouting_lines.splitlines()
        line_number = prerouting_lines[0].split()[0]
        utils.execute("%(DEL)s %(PREROUTING)s %(line_number)s" % locals())

        output_lines = utils.execute("%(LIST)s %(OUTPUT)s | grep %(floating_ip)s" % locals())[0] 
        output_lines = output_lines.splitlines()
        line_number = output_lines[0].split()[0] 
        utils.execute("%(DEL)s %(OUTPUT)s %(line_number)s" % locals())

        snat_lines = utils.execute("%(LIST)s %(SNAT)s | grep %(floating_ip)s" % locals())[0]
        snat_lines = snat_lines.splitlines()
        line_number = snat_lines[0].split()[0] 
        utils.execute("%(DEL)s %(SNAT)s %(line_number)s" % locals()) 

        logger.info("Repaired Floating IP %(floating_ip)s" % locals())
