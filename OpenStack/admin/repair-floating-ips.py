#!/usr/bin/env python
"""
TODO: COMMENT
"""
import os
import sys	
import time
import logging
from urlparse import urlparse

import boto
import boto.ec2

import utils

LIST = "iptables -n -t nat --line-numbers -L"
DEL = "iptables -n -t nat -D"
PREROUTING = "nova-network-PREROUTING"
OUTPUT = "nova-network-OUTPUT"
SNAT = "nova-network-floating-snat"

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

reservations = conn.get_all_instances()
instance_floating_ips = set()

for reservation in reservations:
    for instance in reservation.instances:
        if not instance.public_dns_name in instance_floating_ips:
            instance_floating_ips.add(instance.public_dns_name)
        else:
            message = 'Instance Floating IP %(instance.public_dns_name)s assigned to more than one instance!' % locals()
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
    elif dnat_floating_ip in prerouting_floating_ips:
        corrupt_floating_ips.add(dnat_floating_ip)
    else:
        prerouting_floating_ips.add(dnat_floating_ip)

corrupt_floating_ips = corrupt_floating_ips.union(prerouting_floating_ips.difference(instance_floating_ips))

if not corrupt_floating_ips:
    logger.info("No Floating IPs to repair")
else:
    logger.info("Corrupt Floating IP(s) %(corrupt_floating_ips)s" % locals())    

    import smtplib
    from email.mime.text import MIMEText

    body = "Corrupt Floating IP(s) %(corrupt_floating_ips)s in %(region_rc_filename)s" % locals()
    to = ['']
    msg = MIMEText(body)
    msg['Subject'] = 'Corrupt Floating IP report'
    msg['From'] = ''
    msg['To'] = ', '.join(to)

    s = smtplib.SMTP('')
    s.sendmail(msg['From'], to, msg.as_string())
    s.quit()

"""
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
"""
