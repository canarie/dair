#!/bin/sh
IPTABLES=/sbin/iptables

# Flush
$IPTABLES -F
$IPTABLES -X

# Set defaults
$IPTABLES -P INPUT ACCEPT
$IPTABLES -P OUTPUT ACCEPT
$IPTABLES -P FORWARD ACCEPT

# Custom Chains
$IPTABLES -N services

# Rules for specific services
$IPTABLES -A INPUT -p tcp --dport 3306 -j services
$IPTABLES -A INPUT -p tcp --dport 389 -j services
$IPTABLES -A INPUT -p tcp --dport 636 -j services
$IPTABLES -A INPUT -p tcp --dport 9191 -j services
$IPTABLES -A INPUT -p tcp --dport 9292 -j services

# Custom "services" chain
# Allows access to certain ports from certain sources
# Drops everything else
$IPTABLES -A services -m state --state RELATED,ESTABLISHED -j ACCEPT 
$IPTABLES -A services -i lo -j ACCEPT
$IPTABLES -A services -s 10.0.0.0/8 -j ACCEPT
$IPTABLES -A services -s 192.168.0.0/16 -j ACCEPT
$IPTABLES -A services -s 169.254.0.0/16 -j ACCEPT
$IPTABLES -A services -s 208.75.75.10 -j ACCEPT
$IPTABLES -A services -d 10.0.0.0/8 -j ACCEPT
$IPTABLES -A services -d 192.168.0.0/16 -j ACCEPT
$IPTABLES -A services -d 169.254.0.0/16 -j ACCEPT
$IPTABLES -A services -j DROP

# Nova-network
restart nova-network

