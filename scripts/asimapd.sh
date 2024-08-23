#!/bin/sh
#
# PROVIDE: asimapd_daemon
# KEYWORD: FreeBSD
#
pid_file="/var/run/asimapd.pid"

asimapd_flags=${asimapd_flags-"--debug --ssl_certificate=/etc/ssl/imapscert.pem"}

. /etc/rc.subr

name=asimapd

rcvar=`set_rcvar`

start_cmd=asimapd_start
stop_cmd=asimapd_stop

export PATH=$PATH:/usr/local/bin
asimapd_bin=/usr/local/libexec/asimapd.py

asimapd_start() {
    checkyesno asimapd_enable && echo "Starting asimapd" && \
        ${asimapd_bin} ${asimapd_flags}
}

asimapd_stop() {
    if [ -f ${pid_file} ]
        then echo "Stopping asimapd" && kill `cat ${pid_file}` && rm ${pid_file}
    else
	echo "pid file ${pid_file} does not exist. asimapd not running?"
    fi
}

load_rc_config ${name}
run_rc_command "$1"
