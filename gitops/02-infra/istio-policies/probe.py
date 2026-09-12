import socket, sys
SSLREQ = b"\x00\x00\x00\x08\x04\xd2\x16\x2f"   # Postgres SSLRequest
PING   = b"PING\r\n"
HTTP   = b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
def probe(host, port, payload, label, expect):
    s = socket.socket(); s.settimeout(5)
    try:
        s.connect((host, port)); s.sendall(payload)
        data = s.recv(32)
        got = "ДОСТУП" if data else "отказ(закрыто)"
    except ConnectionResetError:
        got = "отказ(reset)"
    except Exception as e:
        got = f"отказ({type(e).__name__})"
    finally:
        s.close()
    ok = "OK " if got.startswith("ДОСТУП") == (expect == "ДОСТУП") else "!! "
    print(f"  {ok}{label:<44}{got:<16}ожидалось: {expect}")
for line in sys.argv[1:]:
    host, port, kind, label, expect = line.split("|")
    probe(host, int(port), {"pg": SSLREQ, "redis": PING, "http": HTTP}[kind], label, expect)
