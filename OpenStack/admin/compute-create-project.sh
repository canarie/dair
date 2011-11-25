#! /bin/bash

LOG_DIR="/var/log/dair"
LOG="$LOG_DIR/compute-create-project.log"
ERR="$LOG_DIR/compute-create-project-error.log"
VENV="/usr/local/openstack-dashboard/dair/openstack-dashboard/tools/with_venv.sh"
MANAGE="/usr/local/openstack-dashboard/dair/openstack-dashboard/dashboard/manage.py"
QUOTA_CFG="/etc/nova/quota-baseline.cfg"

set -o nounset

function usage {
	echo "usage: $0 <project name> <admin first-name> <admin last-name> <admin email>"
}

function prompt {
	set +u
	read -e -i "$3" -p "$1 : " $2
	set -u
}

function log {
	echo $(date): $1 2>&1 |tee -a $LOG 
}

function project_exists {
	for P in $PROJECT_LIST; do
		if [ "$P" = "$PROJECT" ]; then
			echo "true"
			return
		fi
	done
}

if [[ $EUID -ne 0 ]]; then
	echo "This script must be run as root"
	exit
fi

PROJECT_LIST=$(nova-manage project list)

# we'll clear the error log, but hang on to the regular log"
mkdir -p $LOG_DIR > /dev/null 2>&1
cat /dev/null > $ERR
log "=============================================="


if [ "$#" -lt 10 ]; then
	usage
	prompt "Project name" PROJECT
	prompt "Project description" PROJECT_DESCRIPTION
	prompt "Project administrator's first name" FIRSTNAME
	prompt "Project administrator's last name" LASTNAME
	prompt "Project administrator's email" EMAIL
	prompt "Gigabyte quota" QUOTA_GIGABYTES 100
	prompt "Floating IP quota" QUOTA_FLOATING_IPS 10
	prompt "Instance quota" QUOTA_INSTANCES 10
	prompt "Volume quota" QUOTA_VOLUMES 10
	prompt "Cores quota" QUOTA_CORES 20
else
	PROJECT="$1"
	PROJECT_DESCRIPTION="$2"
	FIRSTNAME="$3"
	LASTNAME="$4"
	EMAIL="$5"
	QUOTA_GIGABYTES="$6"
	QUOTA_FLOATING_IPS="$7"
	QUOTA_INSTANCES="$8"
	QUOTA_VOLUMES="$9"
	QUOTA_CORES="$10"
fi

# project administrators usernames follow DAIR convention
USERNAME="$PROJECT-admin"

log "project = '$PROJECT', first name = '$FIRSTNAME', last name = '$LASTNAME', username = '$USERNAME', email = '$EMAIL'"

MYSQL_USER=$(grep sql_connection /etc/nova/nova.conf | cut -d '/' -f3 | cut -d ':' -f1)
MYSQL_PW=$(grep sql_connection /etc/nova/nova.conf | cut -d ':' -f3 | cut -d '@' -f1)
RESULT=$(mysql -u$MYSQL_USER -p$MYSQL_PW dashboard -e "select * from auth_user where email='$EMAIL'")

if [[ "$RESULT" != '' ]]; then
	log "A user with email $EMAIL already exists"
	exit
fi

# Create User
log "Creating user '$USERNAME'..."
$VENV $MANAGE  createuser --username="$USERNAME" --email="$EMAIL" \
	--firstname="$FIRSTNAME" --lastname="$LASTNAME" --noinput 1>>$LOG 2>>$ERR 
if [ $? -ne 0 ]; then
	log "error while creating user $USERNAME"
	exit
fi

# Password reset
log "Resetting password..."
log "Sending notification to '$EMAIL'..."
$VENV $MANAGE passwordreset --admin=True --email="$EMAIL" 1>>$LOG 2>>$ERR 

if [ "$(project_exists)" == "true" ]; then
	log "project $PROJECT already exists and has an administrator"
	exit
fi

# Create Project + assign user as project administrator
log "Creating new project '$PROJECT'..."
nova-manage project create "$PROJECT" "$USERNAME" "$PROJECT_DESCRIPTION" 1>>$LOG 2>>$ERR 

# Assign roles
log "Assigning netadmin role..."
nova-manage role add $USERNAME netadmin 1>>$LOG 2>>$ERR 
nova-manage role add $USERNAME netadmin $PROJECT 1>>$LOG 2>>$ERR 

log "Assigning sysadmin role..."
nova-manage role add $USERNAME sysadmin 1>>$LOG 2>>$ERR 
nova-manage role add $USERNAME sysadmin $PROJECT 1>>$LOG 2>>$ERR 

CREDENTIALS=$(nova-manage user exports $USERNAME)
SECRET_KEY=$(echo "$CREDENTIALS" | grep EC2_SECRET_KEY | cut -d"=" -f2)
ACCESS_KEY=$(echo "$CREDENTIALS" | grep EC2_ACCESS_KEY | cut -d"=" -f2)
ACCESS_KEY=$ACCESS_KEY:$PROJECT
REGION_LIST=$(grep region_list /etc/nova/nova.conf | sed 's/--region_list=//' | sed 's/,/ /g')

for REGION in $REGION_LIST; do
	log "Creating security group for $REGION..."
	EC2_URL="http://$(echo $REGION | cut -d '=' -f2):8773/services/Cloud"
	euca-authorize -S $SECRET_KEY -A $ACCESS_KEY -U $EC2_URL -P tcp -p 22 -s 0.0.0.0/0 default 1>>$LOG 2>>$ERR 
	euca-authorize -S $SECRET_KEY -A $ACCESS_KEY -U $EC2_URL -P tcp -p 80 -s 0.0.0.0/0 default 1>>$LOG 2>>$ERR 
	euca-authorize -S $SECRET_KEY -A $ACCESS_KEY -U $EC2_URL -P icmp -t -1:-1 default 1>>$LOG 2>>$ERR 
	log "Setting project quotas..."
	ADDRESS=$(echo $REGION | cut -d '=' -f2)
	ssh -o StrictHostKeyChecking=no $ADDRESS "nova-manage project quota ${PROJECT} gigabytes ${QUOTA_GIGABYTES}" 1>>$LOG 2>>$ERR
	ssh -o StrictHostKeyChecking=no $ADDRESS "nova-manage project quota ${PROJECT} floating_ips ${QUOTA_FLOATING_IPS}" 1>>$LOG 2>>$ERR
	ssh -o StrictHostKeyChecking=no $ADDRESS "nova-manage project quota ${PROJECT} instances ${QUOTA_INSTANCES}" 1>>$LOG 2>>$ERR
	ssh -o StrictHostKeyChecking=no $ADDRESS "nova-manage project quota ${PROJECT} volumes ${QUOTA_VOLUMES}" 1>>$LOG 2>>$ERR
	ssh -o StrictHostKeyChecking=no $ADDRESS "nova-manage project quota ${PROJECT} cores ${QUOTA_CORES}" 1>>$LOG 2>>$ERR
done

# Add entry to baseline_quota.cfg so quota monitor will keep it updated.
if [ -s "$QUOTA_CFG" ]
then
  echo "adding $PROJECT to $QUOTA_CFG."
  echo "project=$PROJECT $EMAIL, gigabytes=$QUOTA_GIGABYTES, floating_ips=$QUOTA_FLOATING_IPS, instances=$QUOTA_INSTANCES, volumes=$QUOTA_VOLUMES, cores=$QUOTA_CORES" >> $QUOTA_CFG
else
  echo "$QUOTA_CFG not found. Manually enter this project's quotas so they can be balanced."
  echo "project=$PROJECT $EMAIL, gigabytes=$QUOTA_GIGABYTES, floating_ips=$QUOTA_FLOATING_IPS, instances=$QUOTA_INSTANCES, volumes=$QUOTA_VOLUMES, cores=$QUOTA_CORES"
fi

log "Done.  Congratulations!"
log "Please review '$LOG' and '$ERR' for more details"
log "=============================================="
log ""
