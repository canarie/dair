#!/usr/bin/env python
"""
This script detects Floating IPs that have been "corrupted" or "orphaned" and repairs them.
"""
import os
import sys	
import time
import logging
from urlparse import urlparse
from pprint import pprint

import boto
import boto.ec2

import utils

LIST = "/sbin/iptables -n -t nat --line-numbers -L"
DEL = "/sbin/iptables -t nat -D"
PREROUTING = "nova-network-PREROUTING"
OUTPUT = "nova-network-OUTPUT"
SNAT = "nova-network-floating-snat"
CHAINS = [PREROUTING, OUTPUT, SNAT]

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

region_rc_filename = sys.argv[1]
region = utils.get_regions(region_rc_filename)[0]

url_parsed = urlparse(region.url)
novaRegion=boto.ec2.regioninfo.RegionInfo(name = "nova", endpoint = url_parsed.hostname)
conn = boto.connect_ec2(aws_access_key_id=region.access_key,
                        aws_secret_access_key=region.secret_access_key,
                        is_secure=True,
                        region=novaRegion,
                        port=url_parsed.port,
                        path=url_parsed.path)

addresses = conn.get_all_addresses()
used_floating_ips = set()

for address in addresses:
    if not address.instance_id.startswith('None'):
        used_floating_ips.add(address.public_ip)

reservations = conn.get_all_instances()
instance_floating_ips = set()
instance_info = {}

for reservation in reservations:
    for instance in reservation.instances:
        if instance.public_dns_name.startswith('10'):
            continue
        elif not instance.public_dns_name in instance_floating_ips:
            instance_floating_ips.add(instance.public_dns_name)
            instance_info[instance.public_dns_name] = (instance.id, instance.private_dns_name)
        else:
            message = 'Instance Floating IP %s assigned to more than one instance!' % instance.public_dns_name
            logger.error(message)
            raise Exception(message)

prerouting_lines = utils.execute("%(LIST)s %(PREROUTING)s" % locals())[0]
prerouting_lines = prerouting_lines.splitlines()[2:] # skip the first two non-data lines 

prerouting_floating_ips = set()
corrupt_floating_ips = set()
ignore_floating_ips = set()
ignore_floating_ips.add(url_parsed.hostname)
ignore_floating_ips.add('169.254.169.254')

for prerouting_line in prerouting_lines:
    dnat_floating_ip = prerouting_line.split()[5]

    if dnat_floating_ip in ignore_floating_ips:
        continue
    elif (dnat_floating_ip in prerouting_floating_ips or
          (not dnat_floating_ip in instance_floating_ips and
           not dnat_floating_ip in used_floating_ips)):
        corrupt_floating_ips.add(dnat_floating_ip)
    else:
        prerouting_floating_ips.add(dnat_floating_ip)

missing_from_instances_floating_ips = used_floating_ips.difference(instance_floating_ips)
missing_from_addresses_floating_ips = instance_floating_ips.difference(used_floating_ips)
body = ""

if not (corrupt_floating_ips or missing_from_instances_floating_ips or missing_from_addresses_floating_ips):
    logger.info("No Floating IPs to repair")
    body += "No Floating IPs to repair"
else:
    commands = []

    if missing_from_instances_floating_ips:
        message = "Floating IP(s) that appear to be associated but are not actually associated " \
                  "with any instances: %(missing_from_instances_floating_ips)s" % locals()
        logger.info(message)
        body += "%(message)s\n" % locals()

        for floating_ip in missing_from_instances_floating_ips:
            commands.append("euca-disassociate-address %(floating_ip)s" % locals())
            #conn.disassociate_address(floating_ip)

        body += "The following commands would have been run:\n\t"
        body += "\n\t".join(commands)

    commands = []

    if missing_from_addresses_floating_ips:
        message = "Floating IP(s) that do not appear to be associated but are actually associated " \
                  "with an instance: %(missing_from_addresses_floating_ips)s" % locals()
        logger.info(message)
        body += "\n%(message)s\n" % locals()

        for floating_ip in missing_from_addresses_floating_ips:
            instance_id = instance_info[floating_ip][0]
            commands.append("euca-associate-address -i %(instance_id)s %(floating_ip)s" % locals())
            #conn.disassociate_address(floating_ip)

        body += "The following commands would have been run:\n\t"
        body += "\n\t".join(commands)

    commands = []

    if corrupt_floating_ips:
        message = "Floating IP(s) with extraneous rules in iptables %(corrupt_floating_ips)s" % locals()
        logger.info(message)    
        body += "\n%(message)s\n" % locals()

        for floating_ip in corrupt_floating_ips:
            # there may not be an instance associated with the floating ip so provide a dummy default
            private_ip = instance_info.pop(floating_ip, ("i-xxxxxxxx", "x.x.x.x"))[1]

            for chain in CHAINS:
                lines = utils.execute("%(LIST)s %(chain)s | grep %(floating_ip)s" % locals())[0]
                lines = lines.splitlines()
                
                for line in lines:
                    if line.find(private_ip) == -1:
                        line_number = line.split()[0]
                        commands.append("%(DEL)s %(chain)s %(line_number)s" % locals())
                        #utils.execute("%(DEL)s %(chain)s %(line_number)s" % locals())

        body += "The following commands would have been run:\n\t"
        body += "\n\t".join(commands)

#    logger.info("Repaired Floating IP(s) %s" % missing_from_instances_floating_ips.union(corrupt_floating_ips))

import smtplib
from email.mime.text import MIMEText
import repair_floating_ips_email 

to = repair_floating_ips_email.to
msg = MIMEText(body)
msg['Subject'] = repair_floating_ips_email.subject
msg['From'] = repair_floating_ips_email.from_field
msg['To'] = ', '.join(to)

s = smtplib.SMTP(repair_floating_ips_email.smtp)
s.sendmail(msg['From'], to, msg.as_string())
s.quit()

