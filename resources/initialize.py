from os import system, chdir
chdir("..")

print "Creating Host-Only interface..."
#system("vboxmanage hostonlyif create")
system("vboxmanage hostonlyif ipconfig vboxnet0 --ip 192.168.56.1")

print "Forwarding traffic to Host-Only interface..."
system("sudo iptables -A FORWARD -p tcp -o eth0 -i vboxnet0 -s 192.168.56.0/24 -m conntrack --ctstate NEW -j ACCEPT")
system("sudo iptables -A FORWARD -p tcp -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT")
system("sudo iptables -A POSTROUTING -t nat -j MASQUERADE")
system("sudo sysctl -w net.ipv4.ip_forward=1")

print "Capture traffic..."
system("sudo iptables -t nat -A PREROUTING -i vboxnet0 -p tcp -j REDIRECT --to-ports 8100")

print "Start HoneyProxy Client...."
system("nohup python honeyproxyserver.py > server.log 2> server.err < /dev/null &")
system("sudo iptables -t nat -A PREROUTING -p tcp -i eth0 --dport 80 -j REDIRECT --to-port 8000")
print "done!"
