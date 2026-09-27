import socket,struct,urllib.request,json
q=struct.pack("!HHHHHH",0x2711,0x100,1,0,0,0)+b"\x10security-fixture\x07invalid\x00"+struct.pack("!HH",1,1)
for transport in [socket.SOCK_DGRAM,socket.SOCK_STREAM]:
 s=socket.socket(socket.AF_INET,transport);s.settimeout(5);s.connect(("127.0.0.1",1053))
 if transport==socket.SOCK_DGRAM:s.send(q);r=s.recv(4096)
 else:
  s.sendall(struct.pack("!H",len(q))+q);length=struct.unpack("!H",s.recv(2))[0];r=s.recv(length)
 assert r[:2]==q[:2] and r[3]&15==0 and socket.inet_aton("192.0.2.42") in r,r
 s.close()
for port in [18080,18181]:
 with urllib.request.urlopen(f"http://127.0.0.1:{port}/"+('health' if port==18080 else 'ready'),timeout=5) as r:assert r.status==200
print(json.dumps({"udp_dns":"passed","tcp_dns":"passed","health":"passed","readiness":"passed"}))
