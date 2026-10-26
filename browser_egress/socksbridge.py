"""极简 SOCKS5 桥：本地无认证 127.0.0.1:1080 -> 上游带认证 SOCKS5。

Chromium 不支持 SOCKS5 用户名/密码认证，而池只提供认证版 socks5。
本桥只做协议转发（无任何依赖）。

用法: python socksbridge.py <listen_port> <up_host> <up_port> <user> <pass>
"""

import asyncio
import struct
import sys

LISTEN_PORT = int(sys.argv[1])
UP_HOST, UP_PORT = sys.argv[2], int(sys.argv[3])
UP_USER, UP_PASS = sys.argv[4].encode(), sys.argv[5].encode()


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(cr, cw):
    try:
        # 1) 客户端握手（无认证）
        ver_n = await cr.readexactly(2)
        if ver_n[0] != 5:
            return
        await cr.readexactly(ver_n[1])
        cw.write(b"\x05\x00")
        await cw.drain()

        # 2) 客户端请求（CONNECT）
        head = await cr.readexactly(4)
        cmd, atyp = head[1], head[3]
        if cmd != 1:
            cw.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await cw.drain()
            return
        if atyp == 1:
            host = ".".join(str(b) for b in await cr.readexactly(4))
        elif atyp == 3:
            ln = (await cr.readexactly(1))[0]
            host = (await cr.readexactly(ln)).decode()
        else:  # IPv6
            raw = await cr.readexactly(16)
            host = ":".join(f"{raw[i]:02x}{raw[i+1]:02x}" for i in range(0, 16, 2))
        port = struct.unpack("!H", await cr.readexactly(2))[0]

        # 3) 连接上游并做认证握手
        ur, uw = await asyncio.open_connection(UP_HOST, UP_PORT)
        uw.write(b"\x05\x02\x00\x02")
        await uw.drain()
        resp = await ur.readexactly(2)
        if resp[1] == 2:
            uw.write(b"\x01" + bytes([len(UP_USER)]) + UP_USER
                     + bytes([len(UP_PASS)]) + UP_PASS)
            await uw.drain()
            auth = await ur.readexactly(2)
            if auth[1] != 0:
                raise RuntimeError("upstream auth failed")
        # 4) 上游 CONNECT
        host_b = host.encode()
        uw.write(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                 + struct.pack("!H", port))
        await uw.drain()
        rep = await ur.readexactly(4)
        if rep[1] != 0:
            raise RuntimeError(f"upstream connect rep={rep[1]}")
        if rep[3] == 1:
            await ur.readexactly(4)
        elif rep[3] == 3:
            ln = (await ur.readexactly(1))[0]
            await ur.readexactly(ln)
        else:
            await ur.readexactly(16)
        await ur.readexactly(2)
        cw.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await cw.drain()

        # 5) 双向转发
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    except Exception:
        pass
    finally:
        for w in (cw,):
            try:
                w.close()
            except Exception:
                pass


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", LISTEN_PORT)
    async with server:
        await server.serve_forever()


asyncio.run(main())
