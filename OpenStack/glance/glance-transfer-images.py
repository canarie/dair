#!/usr/bin/env python

"""
Transfers images from the local Glance repository to another.  Will automatically
create kernel/ramdisk associations on the destination repository assuming they
are in the list of images to be transferred.
"""

from glance.client import Client
import argparse

GLANCE_PATH = "/var/lib/glance/images/"

parser = argparse.ArgumentParser(description="Transfer images from the local Glance repository.")
parser.add_argument('images', metavar='image', type=int, nargs='+', help="images to transfer")
parser.add_argument('-d', '--dest', dest='destination', required=True, help="address of destination Glance repository")
parser.add_argument('-p', '--port', dest='local_port', default=9292, help="local Glance port")

args = parser.parse_args()
dest = args.destination.split(':')
source_client = Client('localhost', args.local_port)
try:
	dest_client = Client(dest[0], dest[1])
except IndexError:
	dest_client = Client(dest[0])

images = {}

for image_id in args.images:
	images[image_id] = source_client.get_image_meta(image_id)

# Sort keys to that kernel/ramdisk images are transferred first, makes recreating
# associations easier later on
mapping = { 'aki': 0, 'ari': 0, 'ami': 1 }
sorted_ids = images.keys()
sorted_ids.sort(key=lambda image_id: mapping[images[image_id]['container_format']])

new_ids = {}

for image_id in sorted_ids:
	meta = images[image_id]
	del meta['id']
	del meta['location']
	try:
		meta['properties']['kernel_id'] = new_ids[meta['properties']['kernel_id']]
	except KeyError:
		pass
	try:
		meta['properties']['ramdisk_id'] = new_ids[meta['properties']['ramdisk_id']]
	except KeyError:
		pass	
	
	new_meta = dest_client.add_image(meta, open(GLANCE_PATH+str(image_id)))
	print("Image %d transferred, new ID is %d" % (image_id, new_meta['id']))

	# Track new kernel/ramdisk IDs to recreate associations
	if meta['container_format'] in ['aki', 'ari']:
		new_ids[str(image_id)] = str(new_meta['id'])
