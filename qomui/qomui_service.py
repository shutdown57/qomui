#!/usr/bin/env python3

from PyQt5 import QtCore
import sys, os, time
import pexpect
import re
import shlex
import threading
import shutil
import psutil
import logging
import logging.handlers
import json
from subprocess import Popen, PIPE, check_output, CalledProcessError, STDOUT
import dbus
import dbus.service
from dbus.mainloop.pyqt5 import DBusQtMainLoop

from qomui import firewall 

OPATH = "/org/qomui/service"
IFACE = "org.qomui.service"
BUS_NAME = "org.qomui.service"
ROOTDIR = "/usr/share/qomui"

class GuiLogHandler(logging.Handler):
    def __init__(self, send_log, parent = None):
        super().__init__()
        self.send_log = send_log

    def handle(self, record):
        msg = self.format(record)
        self.send_log(msg)
    
class QomuiDbus(dbus.service.Object):
    pid_list = [] 
    firewall_opt = 1
    hop_dict = {"none" : "none"}
    tun = "tun0"
    connect_status = 0
    
    def __init__(self):
        self.sys_bus = dbus.SystemBus()
        self.bus_name = dbus.service.BusName(BUS_NAME, bus=self.sys_bus)
        dbus.service.Object.__init__(self, self.bus_name, OPATH)
        self.logger = logging.getLogger()
        self.gui_handler = GuiLogHandler(self.send_log)
        self.gui_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger.addHandler(self.gui_handler)
        self.filehandler = logging.handlers.RotatingFileHandler("%s/qomui.log" %(ROOTDIR), 
                                                       maxBytes=2*1024*1024, backupCount=1) 
        self.logger.addHandler(self.filehandler)
        self.filehandler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger.setLevel(logging.DEBUG)
        self.logger.debug("Dbus-service successfully initialized")
        self.load_firewall()
    
    @dbus.service.method(BUS_NAME, in_signature='s')
    def share_log(self, msg):
        record = json.loads(msg)
        log = logging.makeLogRecord(record)
        self.filehandler.handle(log)
        self.gui_handler.handle(log)
    
    @dbus.service.method(BUS_NAME, in_signature='a{ss}', out_signature='')
    def qomuiConnect(self, ovpn_dict):
        self.ovpn_dict = ovpn_dict
        self.hop = self.ovpn_dict["hop"]
        self.connect_thread = threading.Thread(target=self.run)
        self.connect_thread.start()
        self.logger.debug("New thread for OpenVPN process started")  
        
    @dbus.service.method(BUS_NAME, in_signature='a{ss}', out_signature='')
    def hopConnect(self, ovpn_dict):
        self.hop_dict = ovpn_dict 
        
    def add_pid(self, pid):
        self.pid_list.append(pid)

    @dbus.service.signal(BUS_NAME, signature='s')
    def send_log(self, msg):
        return msg

    @dbus.service.method(BUS_NAME, in_signature='', out_signature='')
    def load_firewall(self):
        try:
            with open('%s/config.json' % (ROOTDIR), 'r') as c:
                config = json.load(c)
                firewall.apply_rules(config["firewall"])
                self.disable_ipv6(config["ipv6_disable"])
        except KeyError:
            self.logger.warning('Could not read all values from config file')
            
    @dbus.service.method(BUS_NAME, in_signature='i', out_signature='')
    def disable_ipv6(self, i):
        if i == 1:
            disable_ipv6 = Popen(['sysctl', '-w', 'net.ipv6.conf.all.disable_ipv6=1'])
            self.logger.info('Disabled ipv6')
        else:
            disable_ipv6 = Popen(['sysctl', '-w', 'net.ipv6.conf.all.disable_ipv6=0'])
            self.logger.info('(Re-)enabled ipv6')
    
    @dbus.service.method(BUS_NAME, in_signature='', out_signature='s')
    def return_tun_device(self):
        return self.tun
    
    @dbus.service.method(BUS_NAME, in_signature='', out_signature='')
    def disconnect(self):
        self.restore_default_dns()
        for i in self.pid_list:
            if psutil.pid_exists(i):
                try:
                    self.logger.debug("OS: process %s killed" % (i)) 
                    stop_processes = Popen(['kill', '%s' %(i)])
                except:
                    self.logger.debug("OS: process %s does not exist anymore" % (i)) 

    @dbus.service.method(BUS_NAME, in_signature='s', out_signature='')
    def allowUpdate(self, provider): 
        if provider == "airvpn":
            server = "airvpn.org"
        elif provider == "mullvad":
            server = "mullvad.net"
        self.allow_dns()
        self.logger.info("iptables: Temporarily creating rule to allow access to %s" % server)
        dig_cmd = ["dig", "%s" %(server), "+short"]
        ip = check_output(dig_cmd).decode("utf-8")
        ip = ip.split("\n")[0]
        allow = firewall.add_rule(['-I', 'OUTPUT', '1', '-d', '%s' % (ip), '-j', 'ACCEPT'])

    @dbus.service.method(BUS_NAME, in_signature='', out_signature='')
    def allow_dns(self):
        self.logger.debug("iptables: temporarily allowing dns requests via opendns servers - allowed")
        ipt_dns_out_add = firewall.add_rule(['-I', 'OUTPUT','1', '-p', 'udp',
                                 '-d', "208.67.222.222", '--dport', '53','-j', 'ACCEPT'])
        ipt_dns_in_add = firewall.add_rule(['-I', 'INPUT','2', '-p', 'udp',
                                '-s', "208.67.222.222", '--sport', '53','-j', 'ACCEPT'])
        ipt_dns_out_add_alt = firewall.add_rule(['-I', 'OUTPUT','3', '-p', 'udp',
                                     '-d', "208.67.220.220", '--dport', '53','-j', 'ACCEPT'])
        ipt_dns_in_add_alt = firewall.add_rule(['-I', 'INPUT','4', '-p', 'udp',
                                    '-s', "208.67.220.220", '--sport', '53','-j', 'ACCEPT'])
        self.update_dns("208.67.222.222", "208.67.220.220")
        
    @dbus.service.method(BUS_NAME, in_signature='', out_signature='')
    def block_dns(self):
        self.logger.debug("iptables: deleting rule for dns requests via opendns servers - blocked")
        ipt_dns_out_del = firewall.add_rule(['-D', 'OUTPUT','-p', 'udp',
                                 '-d', "208.67.222.222", '--dport', '53','-j', 'ACCEPT'])
        ipt_dns_in_del = firewall.add_rule(['-D', 'INPUT','-p', 'udp',
                                '-s', "208.67.222.222", '--sport', '53','-j', 'ACCEPT'])
        ipt_dns_out_del_alt = firewall.add_rule(['-D', 'OUTPUT','-p', 'udp',
                                     '-d', "208.67.220.220", '--dport', '53','-j', 'ACCEPT'])
        ipt_dns_in_del_alt = firewall.add_rule(['-D', 'INPUT','-p', 'udp',
                                    '-s', "208.67.220.220", '--sport', '53','-j', 'ACCEPT']) 
        
    @dbus.service.method(BUS_NAME, in_signature='', out_signature='')
    def save_default_dns(self):
        shutil.copyfile("/etc/resolv.conf", "/etc/resolv.conf.qomui.bak")
        self.logger.debug("Created backup of /etc/resolv.conf")
            
    @dbus.service.method(BUS_NAME, in_signature='', out_signature='')
    def restore_default_dns(self):
        try:
            shutil.copyfile("/etc/resolv.conf.qomui.bak", "/etc/resolv.conf")
            self.logger.debug("Restored backup of /etc/resolv.conf")
        except FileNotFoundError:
            self.logger.warning("Default DNS settings not restore. Could not find backup of /etc/resolv.conf")
    
    @dbus.service.method(BUS_NAME, in_signature='ss', out_signature='s')
    def copyCerts(self, provider, certpath):
        if not os.path.exists("%s/certs" %ROOTDIR):
            os.makedirs("%s/certs" %ROOTDIR)
        
        if provider == "airvpn":
            shutil.copyfile("%s/sshtunnel.key" % (certpath), "%s/certs/sshtunnel.key" % (ROOTDIR))
            shutil.copyfile("%s/stunnel.crt" % (certpath), "%s/certs/stunnel.crt" % (ROOTDIR))
            shutil.copyfile("%s/ca.crt" % (certpath), "%s/certs/ca.crt" % (ROOTDIR))
            shutil.copyfile("%s/ta.key" % (certpath), "%s/certs/ta.key" % (ROOTDIR))
            shutil.copyfile("%s/user.key" % (certpath), "%s/certs/user.key" % (ROOTDIR))
            shutil.copyfile("%s/user.crt" % (certpath), "%s/certs/user.crt" % (ROOTDIR))
        elif provider == "mullvad":
            shutil.copyfile("%s/ca.crt" % (certpath), "%s/certs/mullvad_ca.crt" % (ROOTDIR))
            shutil.copyfile("%s/crl.pem" % (certpath), "%s/certs/mullvad_crl.pem" % (ROOTDIR))
            shutil.copyfile("%s/mullvad_userpass.txt" % (certpath), "%s/certs/mullvad_userpass.txt" % (ROOTDIR))
            
        elif provider == "custom":
            if not os.path.exists("%s/authfiles" % (ROOTDIR)):
               os.makedirs("%s/authfiles" % (ROOTDIR))
            split_path = certpath.split(" ")
            shutil.copyfile(split_path[0], split_path[1])
  
        self.logger.debug("Copied certificates and keys to %s/certs" %ROOTDIR)
        self.logger.debug("Removed temporary files")
        for key in [file for file in os.listdir("%s/certs" % (ROOTDIR))]:

            Popen(['chown', 'root', '%s/certs/%s' % (ROOTDIR, key)])
            Popen(['chmod', '0600', '%s/certs/%s' % (ROOTDIR, key)])
        return "copied"
       
    
    @dbus.service.method(BUS_NAME, in_signature='ss', out_signature='')
    def update_dns(self, dns1, dns2):
        dns = open("/etc/resolv.conf", "w")
        dns.write("nameserver %s\nnameserver %s" % (dns1, dns2))
        self.logger.info("DNS: Overwriting /etc/resolv.conf with %s and %s" %(dns1, dns2))
        
    @dbus.service.signal(BUS_NAME, signature='s')
    def reply(self, msg):
        return msg
    
    def run(self):
        self.connect_status = 0
        provider = self.ovpn_dict["provider"]
        ip = self.ovpn_dict["ip"]
        firewall.add_rule(['-I', 'OUTPUT', '1', '-d', '%s' % (ip), '-j', 'ACCEPT'])
     
        self.logger.info("iptables: created rule for %s" %ip)
        path = "%s/temp.ovpn" %ROOTDIR
        cwd_ovpn = None
        try:
            port = self.ovpn_dict["port"]
            protocol = self.ovpn_dict["protocol"]
        except KeyError:
            pass
              
        if provider == "airvpn":
            if protocol == "SSL":
                with open("%s/ssl_config" %ROOTDIR, "r") as ssl_edit:
                    ssl_config = ssl_edit.readlines()
                    for line, value in enumerate(ssl_config):
                        if value.startswith("connect") is True:
                            ssl_config[line] = "connect = %s:443\n" % (ip) 
                    with open("%s/temp.ssl" % ROOTDIR, "w") as ssl_dump:
                        ssl_dump.writelines(ssl_config)
                        ssl_dump.close()
                    ssl_edit.close()
                self.write_config(provider, ip, port, protocol)
                self.ssl_thread = threading.Thread(target=self.ssl, args=(ip,))
                self.ssl_thread.start()
                logging.info("Started Stunnel process in new thread")
            elif protocol == "SSH":
                self.write_config(provider, ip, port, protocol)
                self.ssh_thread = threading.Thread(target=self.ssh, args=(ip,port,))
                self.ssh_thread.start()
                logging.info("Started SSH process in new thread")
                time.sleep(2)
            else:
                self.write_config(provider, ip, port, protocol)

        elif provider == "mullvad":
            self.write_config(provider, ip, port, protocol)
            
        elif provider == "custom":
            path = self.ovpn_dict["path"]
            cwd_ovpn=os.path.dirname(path) 
            
        if self.hop == "2":
            firewall_hop_add = firewall.add_rule(['-I', 'OUTPUT', '1', '-d', '%s' % (self.hop_dict["ip"]), '-j', 'ACCEPT'])
            if self.hop_dict["provider"] != "custom":
                hop_path = "%s/hop.ovpn" %ROOTDIR
                self.write_config(self.hop_dict["provider"], self.hop_dict["ip"], 
                                self.hop_dict["port"], self.hop_dict["protocol"],
                                edit="hop")
            else:
                hop_path = self.hop_dict["path"]
                cwd_ovpn=os.path.dirname(path) 
            self.hop_thread = threading.Thread(target=self.ovpn, args=(hop_path, 
                                                                       "1", cwd_ovpn,))
            self.hop_thread.start()
            while self.connect_status == 0:
                time.sleep(1)
            
        self.ovpn(path, self.hop, cwd_ovpn)
            
    def write_config(self, provider, ip, port, protocol, edit="temp"):
        with open("%s/%s_config" %(ROOTDIR, provider), "r") as ovpn_edit:    
            config = ovpn_edit.readlines()
            if protocol == "SSL":
               config.insert(13, "route %s 255.255.255.255 net_gateway\n" % (ip))
               ip = "127.0.0.1"
               port = "1413"
               protocol = "tcp"
               
            elif protocol == "SSH":
               config.insert(13, "route %s 255.255.255.255 net_gateway\n" % (ip))
               ip = "127.0.0.1"
               port = "1412"
               protocol = "tcp"

            for line, value in enumerate(config):
                if value == "proto \n":
                    config[line] = "proto %s \n" % (protocol.lower()) 
                elif value == "remote \n":
                    config[line] = "remote %s %s \n" % (ip.replace("\n", ""), port)
            
            with open("%s/%s.ovpn" %(ROOTDIR, edit), "w") as ovpn_dump:
                    ovpn_dump.writelines(config)
                    ovpn_dump.close()
            ovpn_edit.close()
        logging.debug("Temporary config file(s) for requested server written") 
        
    def ovpn(self, ovpn_file, h, cwd_ovpn):
        logging.info("Establishing new OpenVPN tunnel")
        name = self.ovpn_dict["name"]
        last_ip = self.ovpn_dict["ip"]
        if h == "1":
            name = self.hop_dict["name"]
            self.logger.info("Establishing connection to %s - first hop" %name)
            last_ip = self.hop_dict["ip"]
            cmd_ovpn = ['/usr/bin/openvpn',
                        '--config', '%s' %(ovpn_file), 
                        '--route-nopull', 
                        '--script-security', '2', 
                        '--up', '/usr/share/qomui/hop.sh -f %s %s' %(self.hop_dict["ip"], self.ovpn_dict["ip"]),
                        '--down', '/usr/share/qomui/hop_down.sh %s' %(self.hop_dict["ip"])
                        ]
            
        elif h == "2":
            self.logger.info("Establishing connection to %s - second hop" %name)
            cmd_ovpn = ['/usr/bin/openvpn',
                        '--config', '%s' %(ovpn_file), 
                        '--route-nopull', 
                        '--script-security', '2', 
                        '--up', '%s/hop.sh -s' %(ROOTDIR)
                        ]
            
        else:
            self.logger.info("Establishing connection to %s" %name)
            cmd_ovpn = ['openvpn','%s' % ovpn_file]
        
        ovpn_exe = Popen(cmd_ovpn, stdout=PIPE, stderr=STDOUT, cwd=cwd_ovpn, bufsize=1, universal_newlines=True)
        self.add_pid(ovpn_exe.pid)
        line = ovpn_exe.stdout.readline()
        while line.find("SIGTERM[hard,] received, process exiting") == -1:
                line_format = ("OpenVPN:" + line.replace('%s' %(time.asctime()), '').replace('\n', ''))
                logging.info(line_format)
                if line.find("Initialization Sequence Completed") != -1:
                    self.connect_status = 1
                    self.reply("success")
                    self.logger.info("Successfully connected to %s" %name)
                elif line.find('TUN/TAP device') != -1:
                    self.tun = line_format.split(" ")[3]
                elif line.find("Restart pause, 10 second(s)") != -1:
                    self.reply("fail1")
                    self.logger.info("Connection attempt failed") 
                elif line.find('SIGTERM[soft,auth-failure]') != -1:
                    self.reply("fail2")
                    self.logger.info("Authentication error while trying to connect")
                elif line == '':
                    break
                #elif line.find("SIGTERM[hard,] received, process exiting") != -1:
                 #break
                line = ovpn_exe.stdout.readline()
        logging.info("OpenVPN:" + line.replace('%s' %(time.asctime()), '').replace('\n', ''))
        ovpn_exe.stdout.close()
        self.reply("kill")
        self.logger.info("OpenVPN - process killed")
        firewall_del = firewall.add_rule(['-D', 'OUTPUT',
                                  '-d', '%s' % (last_ip), '-j', 'ACCEPT'])



    def ssl(self, ip):
        cmd_ssl = ['stunnel','%s' % ("%s/temp.ssl" % (ROOTDIR))]
        ssl_exe = Popen(cmd_ssl, stdout=PIPE, stderr=STDOUT, bufsize=1, universal_newlines=True)
        self.add_pid(ssl_exe.pid)
        line = ssl_exe.stdout.readline()
        while line.find('SIGINT') == -1:
                logging.info("Stunnel: " + line.replace('\n', ''))
                if line == '':
                    break
                elif line.find("Configuration succesful") != -1:
                    logging.info("Stunnel: Successfully opened SSL tunnel to %s" %(self.ip)) 
                line = ssl_exe.stdout.readline()
        ssl_exe.stdout.close()
        
    def ssh(self, ip, port):
        cmd_ssh = "ssh -i %s/certs/sshtunnel.key -L 1412:127.0.0.1:2018 sshtunnel@%s -p %s -N -T -v" % (ROOTDIR, ip, port)
        ssh_exe = pexpect.spawn(cmd_ssh)
        ssh_newkey = b'Are you sure you want to continue connecting'
        ssh_success = 'Forced command'
        self.add_pid(ssh_exe.pid)  
        i = ssh_exe.expect([ssh_newkey, ssh_success])
        if i == 0:
            ssh_exe.sendline('yes')
            logging.info("SSH: Accepted SHA fingerprint from %s" %(ip))
        
        before = ssh_exe.before.decode("utf-8")
        after = ssh_exe.after.decode("utf-8")
        full = (before + after)
        
        for line in full.split("\n"):
            logging.info("SSH: " + line.replace("\r", ""))

        logging.info("SSH: Successfully opened SSH tunnel to %s" %(ip)) 
        ssh_exe.wait()

def main():
    DBusQtMainLoop(set_as_default=True)
    app = QtCore.QCoreApplication([])
    service = QomuiDbus()
    app.exec_()
  
if __name__ == '__main__':
    main()