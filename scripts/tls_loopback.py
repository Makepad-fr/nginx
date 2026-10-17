"""Send isolated fixture requests to loopback with the tenant's real TLS SNI."""

import http.client
import socket


class LoopbackHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        peer = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        self.sock = self._context.wrap_socket(peer, server_hostname=self.host)


def request(host, port, context, path, data=None, headers=None):
    connection = LoopbackHTTPSConnection(host, port, context=context, timeout=15)
    try:
        request_headers = dict(headers or {})
        # The fixture's published loopback port is ephemeral. Send the
        # tenant's actual authority, as a public HTTPS client would, rather
        # than http.client's implicit Host: tenant:<fixture-port>.
        if not any(name.lower() == "host" for name in request_headers):
            request_headers["Host"] = host
        connection.request("POST" if data is not None else "GET", path, body=data, headers=request_headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()
