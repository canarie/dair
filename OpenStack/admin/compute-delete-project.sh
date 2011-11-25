#!/usr/bin/env bash

# Deletes given project and releases all associated resources
# Currently deletes all associated users regardless of whether they are part of another project
# Must be run from the management node


NOVA_CONF='/etc/nova/nova.conf'
QUOTA_CFG="/etc/nova/quota-baseline.cfg"


if [[ $EUID -ne 0 ]]; then
	echo "This script must be run as root"
	exit 1
fi

if [ ! -n "$1" ]; then
	echo -n "Specify project to delete: "
	read PROJECT
else
	PROJECT=$1
fi

# This function does all the project deleting by zone.
function delete_project()
{
	EC2_URL="$1/services/Cloud"
	echo "deleting project from $1"
	# Use project admin's credentials
	ADMIN="$PROJECT-admin"
	EXPORTS=$(nova-manage user exports $ADMIN)

	if [ ! $(echo "$EXPORTS" | wc -l) -eq 2 ]; then
		echo "Bad project name or admin is not $ADMIN"
		exit 1
	fi

	echo -n "About to delete project $PROJECT, continue? (y/N) "
	read ANSWER

	if [[ $ANSWER != 'y' && $ANSWER != 'Y' ]]; then
		echo "Aborting..."
		exit 0
	fi

	SECRET_KEY=$(echo "$EXPORTS" | grep EC2_SECRET_KEY | cut -d '=' -f2)
	ACCESS_KEY=$(echo "$EXPORTS" | grep EC2_ACCESS_KEY | cut -d '=' -f2):$PROJECT

	echo "Deleting images..."
	IMAGES=$(euca-describe-images -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL | grep "	private	" | cut -f2)

	if [[ $IMAGES != '' ]]; then
		echo $IMAGES | xargs -n1 euca-deregister -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL
	fi

	echo "Releasing addresses..."
	ADDRESSES=$(euca-describe-addresses -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL | cut -f2)

	if [[ $ADDRESSES != '' ]]; then
		echo $ADDRESSES | xargs -n1 euca-disassociate-address -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL
		sleep 2
		echo $ADDRESSES | xargs -n1 euca-release-address -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL
	fi

	echo "Deleting volumes..."
	VOLUMES=$(euca-describe-volumes -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL | cut -f2)

	if [[ $VOLUMES != '' ]]; then
		echo $VOLUMES | xargs -n1 euca-detach-volume -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL
		sleep 2
		echo $VOLUMES | xargs -n1 euca-delete-volume -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL
	fi

	echo "Terminating instances..."
	INSTANCES=$(euca-describe-instances -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL | grep "INSTANCE" | cut -f2)

	if [[ $INSTANCES != '' ]]; then
		echo $INSTANCES | xargs -n1 euca-terminate-instances -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL
	fi

	echo "Deleting keypairs..."
	KEYPAIRS=$(euca-describe-keypairs -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL | cut -f2)

	if [[ $KEYPAIRS != '' ]]; then
		echo $KEYPAIRS | xargs -n1 euca-delete-keypair -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL
	fi

	# Currently unsupported in OpenStack
	#echo "Deleting snapshots..."
	#SNAPSHOTS=$(euca-describe-snapshots -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL | cut -f2)

	#if [[ ! $SNAPSHOTS = '' ]]; then
	#	echo $SNAPSHOTS | xargs -n1 euca-delete-snapshot -s $SECRET_KEY -a $ACCESS_KEY -U $EC2_URL
	#fi

}

REGION_LIST=$(grep region_list ${NOVA_CONF} | sed 's/--region_list=//' | sed 's/,/ /g')

for REGION in $REGION_LIST; do
	IP=`echo $REGION | cut -d'=' -f2`
	region="https://$IP:8772"
	delete_project "$region"
done

# LDAP 
echo "Deleting users..."
LDAP_ADDR=$(sed -n 's/--ldap_url=ldap:\/\/\(.\+\)/\1/p' $NOVA_CONF)
LDAP_USER=$(sed -n 's/--ldap_user_dn=\(.\+\)/\1/p' $NOVA_CONF)
LDAP_PW=$(sed -n 's/--ldap_password=\(.\+\)/\1/p' $NOVA_CONF)
LDAP_SEARCHBASE="ou=Groups,$(echo $LDAP_USER | grep -o 'dc=.\+')"
USERS=$(ldapsearch -b "cn=$PROJECT,$LDAP_SEARCHBASE" -D $LDAP_USER -h $LDAP_ADDR -xw $LDAP_PW | sed -n 's/member: uid=\([^,]\+\).\+/\1/p' | grep -xv $ADMIN | sort | uniq)

VENV="/usr/local/openstack-dashboard/dair/openstack-dashboard/tools/with_venv.sh"
MANAGE="/usr/local/openstack-dashboard/dair/openstack-dashboard/dashboard/manage.py"

if [[ $USERS != '' ]]; then
	echo $USERS | xargs -n1 nova-manage user delete

	# Delete dashboard users
	for USER in $USERS; do
		$VENV $MANAGE deleteuser --username=$USER --noinput
	done
fi

echo "Deleting project..."
nova-manage project delete $PROJECT
nova-manage project scrub $PROJECT

echo "Deleting project admin..."
nova-manage user delete $ADMIN
$VENV $MANAGE deleteuser --username=$ADMIN --noinput

if [ -s "$QUOTA_CFG" ]
then
  echo "deleting $PROJECT from $QUOTA_CFG."
  # delete the line with project name in it from the cfg and save.
  (echo "g/${PROJECT}/d"; echo 'wq') | ex -s $QUOTA_CFG
else
  echo "$QUOTA_CFG not found. Manually delete or comment out this project's quotas."
fi

echo "Project $PROJECT deleted."

exit 0
