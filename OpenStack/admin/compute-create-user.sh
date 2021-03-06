#! /bin/bash

LOG_DIR="/var/log/dair"
LOG="$LOG_DIR/compute-create-user.log"
ERR="$LOG_DIR/compute-create-user-error.log"
VENV="/usr/local/openstack-dashboard/dair/openstack-dashboard/tools/with_venv.sh"
MANAGE="/usr/local/openstack-dashboard/dair/openstack-dashboard/dashboard/manage.py"

set -o nounset

function usage {
	echo "usage: $0 <project> <first-name> <last-name> <username> <email>"
}

function prompt {
	set +u
	read -e -i "$3" -p "$1 : " $2
	set -u
}

function log {
	echo $(date): $1 | tee -a $LOG 
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

if [ "$#" -lt 5 ]; then
	usage
	prompt "project name" PROJECT
	prompt "User's first name" FIRSTNAME
	prompt "User's last name" LASTNAME
	prompt "User's username" USERNAME $(echo $FIRSTNAME.$LASTNAME  | tr '[A-Z]' '[a-z]')
	prompt "User's email" EMAIL
else
	PROJECT="$1"
	FIRSTNAME="$2"
	LASTNAME="$3"
	USERNAME="$4" 
	EMAIL="$5"
fi

log "project = '$PROJECT', first name = '$FIRSTNAME', \
last name = '$LASTNAME', username = '$USERNAME', email = '$EMAIL'"

if [ "$(project_exists)" != "true" ]; then
	log "project $PROJECT does not exist"
	exit
fi

MYSQL_USER=$(grep sql_connection /etc/nova/nova.conf | cut -d '/' -f3 | cut -d ':' -f1)
MYSQL_PW=$(grep sql_connection /etc/nova/nova.conf | cut -d ':' -f3 | cut -d '@' -f1)
RESULT=$(mysql -u$MYSQL_USER -p$MYSQL_PW dashboard -e "select * from auth_user where email='$EMAIL'")

if [[ "$RESULT" != '' ]]; then
	log "A user with email $EMAIL already exists"
	exit
fi

log "Creating user '$USERNAME'..."
$VENV $MANAGE  createuser --username="$USERNAME" --email="$EMAIL" \
	--firstname="$FIRSTNAME" --lastname="$LASTNAME" --noinput 1>>$LOG 2>>$ERR 

if [ $? -ne 0 ]; then
	log "error while creating user $USERNAME"
	exit
fi

log "Resetting password..."
log "Sending notification to '$EMAIL'..."
$VENV $MANAGE passwordreset --email="$EMAIL" 1>>$LOG 2>>$ERR 

log "adding $USERNAME to $PROJECT"
nova-manage project add $PROJECT $USERNAME 1>>$LOG 2>>$ERR 

log "Assigning netadmin role..."
nova-manage role add $USERNAME netadmin 1>>$LOG 2>>$ERR 
nova-manage role add $USERNAME netadmin $PROJECT 1>>$LOG 2>>$ERR 

log "Assigning sysadmin role..."
nova-manage role add $USERNAME sysadmin 1>>$LOG 2>>$ERR 
nova-manage role add $USERNAME sysadmin $PROJECT 1>>$LOG 2>>$ERR 

log "Done.  Congratulations!"
log "Please review '$LOG' and '$ERR' for more details"
log "=============================================="
log ""
