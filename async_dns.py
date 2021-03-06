#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2014-2015 clowwindy
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function, \
    with_statement

import os
import socket
import struct
import re
import logging
import asyncio
import time
import aiodns
from shadowsocks import common, lru_cache, eventloop, shell


CACHE_SWEEP_INTERVAL = 30

VALID_HOSTNAME = re.compile(br"(?!-)[A-Z\d-]{1,63}(?<!-)$", re.IGNORECASE)

common.patch_socket()

# rfc1035
# format
# +---------------------+
# |        Header       |
# +---------------------+
# |       Question      | the question for the name server
# +---------------------+
# |        Answer       | RRs answering the question
# +---------------------+
# |      Authority      | RRs pointing toward an authority
# +---------------------+
# |      Additional     | RRs holding additional information
# +---------------------+
#
# header
#                                 1  1  1  1  1  1
#   0  1  2  3  4  5  6  7  8  9  0  1  2  3  4  5
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                      ID                       |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |QR|   Opcode  |AA|TC|RD|RA|   Z    |   RCODE   |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                    QDCOUNT                    |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                    ANCOUNT                    |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                    NSCOUNT                    |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                    ARCOUNT                    |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+

QTYPE_ANY = 255
QTYPE_A = 1
QTYPE_AAAA = 28
QTYPE_CNAME = 5
QTYPE_NS = 2
QCLASS_IN = 1


def build_address(address):
    address = address.strip(b'.')
    labels = address.split(b'.')
    results = []
    for label in labels:
        l = len(label)
        if l > 63:
            return None
        results.append(common.chr(l))
        results.append(label)
    results.append(b'\0')
    return b''.join(results)


def build_request(address, qtype):
    request_id = os.urandom(2)
    header = struct.pack('!BBHHHH', 1, 0, 1, 0, 0, 0)
    addr = build_address(address)
    qtype_qclass = struct.pack('!HH', qtype, QCLASS_IN)
    return request_id + header + addr + qtype_qclass


def parse_ip(addrtype, data, length, offset):
    if addrtype == QTYPE_A:
        return socket.inet_ntop(socket.AF_INET, data[offset:offset + length])
    elif addrtype == QTYPE_AAAA:
        return socket.inet_ntop(socket.AF_INET6, data[offset:offset + length])
    elif addrtype in [QTYPE_CNAME, QTYPE_NS]:
        return parse_name(data, offset)[1]
    else:
        return data[offset:offset + length]


def parse_name(data, offset):
    p = offset
    labels = []
    l = common.ord(data[p])
    while l > 0:
        if (l & (128 + 64)) == (128 + 64):
            # pointer
            pointer = struct.unpack('!H', data[p:p + 2])[0]
            pointer &= 0x3FFF
            r = parse_name(data, pointer)
            labels.append(r[1])
            p += 2
            # pointer is the end
            return p - offset, b'.'.join(labels)
        else:
            labels.append(data[p + 1:p + 1 + l])
            p += 1 + l
        l = common.ord(data[p])
    return p - offset + 1, b'.'.join(labels)


# rfc1035
# record
#                                    1  1  1  1  1  1
#      0  1  2  3  4  5  6  7  8  9  0  1  2  3  4  5
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                                               |
#    /                                               /
#    /                      NAME                     /
#    |                                               |
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                      TYPE                     |
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                     CLASS                     |
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                      TTL                      |
#    |                                               |
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                   RDLENGTH                    |
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--|
#    /                     RDATA                     /
#    /                                               /
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
def parse_record(data, offset, question=False):
    nlen, name = parse_name(data, offset)
    if not question:
        record_type, record_class, record_ttl, record_rdlength = struct.unpack(
            '!HHiH', data[offset + nlen:offset + nlen + 10]
        )
        ip = parse_ip(record_type, data, record_rdlength, offset + nlen + 10)
        return nlen + 10 + record_rdlength, \
            (name, ip, record_type, record_class, record_ttl)
    else:
        record_type, record_class = struct.unpack(
            '!HH', data[offset + nlen:offset + nlen + 4]
        )
        return nlen + 4, (name, None, record_type, record_class, None, None)


def parse_header(data):
    if len(data) >= 12:
        header = struct.unpack('!HBBHHHH', data[:12])
        res_id = header[0]
        res_qr = header[1] & 128
        res_tc = header[1] & 2
        res_ra = header[2] & 128
        res_rcode = header[2] & 15
        # assert res_tc == 0
        # assert res_rcode in [0, 3]
        res_qdcount = header[3]
        res_ancount = header[4]
        res_nscount = header[5]
        res_arcount = header[6]
        return (res_id, res_qr, res_tc, res_ra, res_rcode, res_qdcount,
                res_ancount, res_nscount, res_arcount)
    return None


def parse_response(data):
    try:
        if len(data) >= 12:
            header = parse_header(data)
            if not header:
                return None
            res_id, res_qr, res_tc, res_ra, res_rcode, res_qdcount, \
                res_ancount, res_nscount, res_arcount = header

            qds = []
            ans = []
            offset = 12
            for i in range(0, res_qdcount):
                l, r = parse_record(data, offset, True)
                offset += l
                if r:
                    qds.append(r)
            for i in range(0, res_ancount):
                l, r = parse_record(data, offset)
                offset += l
                if r:
                    ans.append(r)
            for i in range(0, res_nscount):
                l, r = parse_record(data, offset)
                offset += l
            for i in range(0, res_arcount):
                l, r = parse_record(data, offset)
                offset += l
            response = DNSResponse()
            if qds:
                response.hostname = qds[0][0]
            for an in qds:
                response.questions.append((an[1], an[2], an[3]))
            for an in ans:
                response.answers.append((an[1], an[2], an[3]))
            return response
    except Exception as e:
        shell.print_exception(e)
        return None


def is_valid_hostname(hostname):
    if len(hostname) > 255:
        return False
    if hostname[-1] == b'.':
        hostname = hostname[:-1]
    return all(VALID_HOSTNAME.match(x) for x in hostname.split(b'.'))


class DNSResponse(object):
    def __init__(self):
        self.hostname = None
        self.questions = []  # each: (addr, type, class)
        self.answers = []  # each: (addr, type, class)

    def __str__(self):
        return '%s: %s' % (self.hostname, str(self.answers))


STATUS_IPV4 = 0
STATUS_IPV6 = 1

count = 0


class MyClass:

    __slots__ = ['xx', ]

    def __init__(self):
        self.xx = 1

a = MyClass()
a.xx = 1


class DNSProtocol(asyncio.DatagramProtocol):

    num = 1
    sending = set()
    receiving = set()
    conn_lost = set()
    used_port = {}

    def checkout_port_usage(self, used_port):
        for port, dnsprotocals in used_port.items():
            if len(dnsprotocals) > 1:
                print("------- port reuse found begin ------------")
                print(dnsprotocals)
                print("-------- port reuse found end---------------------")

    def __init__(self, hostname, callback):
        self.hostname = hostname
        self.transport = None
        self.callback = callback
        self.num = DNSProtocol.num
        self.loop = asyncio.get_event_loop()
        self.port = None
        self.file_no = None
        self.receive_data_num = 0
        DNSProtocol.num += 1

    def connection_made(self, transport):
        self.transport = transport
        self.port = transport._sock.getsockname()[1]
        self.file_no = self.transport._sock.fileno()

        req = build_request(self.hostname, QTYPE_A)
        self.transport.sendto(req)
        DNSProtocol.sending.add(self)
        if DNSProtocol.used_port.get(self.port):
            DNSProtocol.used_port[self.port].append(self)
        else:
            DNSProtocol.used_port[self.port] = [self, ]
        # print("connection made, {}".format(self))
        self.checkout_port_usage(used_port=self.used_port)
    def datagram_received(self, data, addr):
        if len(data) != 90:
            print("receive data length is not 90, {}".format(data))
        DNSProtocol.receiving.add(self)
        response = parse_response(data)
        port = self.transport._sock.getsockname()[1]
        if not DNSProtocol.used_port.get(port, None):
            print("no dnsprotocal found")
        if len(DNSProtocol.used_port[port]) == 1:
            try:
                DNSProtocol.used_port[port].remove(self)
            except Exception:
                print("remove dns protocal from used port fail, {}".format(self))
                pass
            # print("{}, close port {}".format(self, self.transport._sock.getsockname()[1]))
        elif len(DNSProtocol.used_port[port]) >= 1:
            for x in DNSProtocol.used_port[port]:
                print("{} with sock fileno {}".format(x, x.transport._sock.fileno()))
            try:
                DNSProtocol.used_port[port].remove(self)
            except Exception:
                print("remove dns protocal from used port fail, {}".format(self))
        else:
            print('error, no port used,...port: {}'.format(port))
        # don
        # self.timer.cancel()
        # print("receive data, {} close ".format(self))
        self.transport.close()
        self.callback((response, self))

    def error_received(self, exc):
        print('Error received:', exc)

    def connection_lost(self, exc):
        # print("connection lost, {} close".format(self))
        self.transport.close()

    def __repr__(self):
        return "<DNSProtocal: {}, f: {}, p: {}>".format(self.num, self.file_no, self.port)


loop = asyncio.get_event_loop()


async def get_hostinfo(hostname):
    future = asyncio.Future()

    def callback(result):
        if future.cancelled():
            return
        else:
            # print("set result for future, {}".format(result[1]))
            future.set_result(result)


    dns_request = loop.create_datagram_endpoint(
        lambda: DNSProtocol(hostname, callback),
        remote_addr=('127.0.1.1', 53),
        family=socket.AF_INET,
        proto=socket.SOL_UDP
    )
    transport, protocal = await dns_request
    response, num = await future
    # if (response == "error"):
    #     print(response, num)
    if len(DNSProtocol.sending.difference(DNSProtocol.receiving)) < 40:
        print("sending num: {}".format(len(DNSProtocol.sending)))
        remaining = DNSProtocol.sending.difference(DNSProtocol.receiving)
        print("remaining num: {}, doing {}".format(len(remaining), remaining))
    # print("get response from {}".format(protocal))
    return response

async def test_aiodns():
    resolver = aiodns.DNSResolver(loop=loop)
    res = await resolver.query("www.163.com", "A")
    return res

tasks1 = [test_aiodns() for i in range(1000)]

if __name__ == '__main__':
    # test()
    # begin = time.time()
    # loop.run_until_complete(asyncio.wait(tasks))
    # print("last for {}".format(time.time() - begin))
    #
    # begin = time.time()
    # loop.run_until_complete(asyncio.wait(tasks1))
    # print("last for {}".format(time.time() - begin))
    # dns_connect = loop.create_datagram_endpoint(
    #     lambda: DNSProtocol('abc', loop),
    #     remote_addr=('127.0.1.1', 53)
    # )
    tasks3 = [get_hostinfo(b"www.baidu.com") for i in range(200)]
    begin = time.time()
    loop.run_until_complete(asyncio.wait(tasks3))
    # loop.run_forever()
    print("last for {}".format(time.time() - begin))


def get_hostinfo_low(domain_name):
    # low level socket to get
    # open udp connection
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                   socket.SOL_UDP)
    sock.setblocking(False)

    # send request
    req = build_request(domain_name, QTYPE_A)
    sock.sendto(req, ('127.0.0.1', 53))


    future = asyncio.Future()
    def callback(sock):
        if future.cancelled():
            return
        else:
            # print("set result for future, {}".format(result[1]))
            sock.send
            future.set_result(result)
    loop.add_reader(sock.fileno(), callback)
