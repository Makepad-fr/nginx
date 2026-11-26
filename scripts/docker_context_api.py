"""Version-bound shared ingress updates through the workflow's Docker SSH context.

Importing this module performs no I/O. Callers supply the original service version
and full candidate spec, and own convergence checks and any recovery decision.
"""
import http.client
import io
import json
import os
import signal
import re
import subprocess

SERVICE = 'makepad-edge_nginx'


def _command(args, *, data=None, timeout=30):
    """Own the CLI and its SSH descendants, including timeout cleanup."""
    process = subprocess.Popen(list(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(data, timeout=timeout)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    except BaseException as primary:
        # The still-owned session ID is the child's PID. Kill the whole group
        # before reaping its leader, so an SSH child cannot retain our pipes.
        cleanup_error = None
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except BaseException as error:
            cleanup_error = error
        try:
            process.communicate(timeout=5)
        except BaseException as error:
            cleanup_error = error
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except BaseException:
                    pass
            try:
                process.wait(timeout=1)
            except BaseException as error:
                cleanup_error = error
        if cleanup_error is not None:
            raise RuntimeError('Docker transport cleanup unverified; reconcile owned process') from primary
        raise


def _read(*args):
    """Bounded metadata read; never include Docker output in an exception."""
    try:
        result = _command(args)
        if result.returncode or len(result.stdout) > 64 * 1024:
            raise RuntimeError('Docker context metadata unavailable')
        return result.stdout.decode()
    except (subprocess.TimeoutExpired, UnicodeDecodeError) as error:
        raise RuntimeError('Docker context metadata unavailable') from error


def update_service(before, spec):
    """One version-bound Engine request through the existing Docker SSH context.

    dial-stdio is Docker CLI's internal daemon stream transport. It retains the
    workflow's isolated SSH configuration; this never opens a runner-local socket.
    A missing/failed/ambiguous response is not acknowledgement and is not retried.
    """
    context = _read('docker', 'context', 'show').strip()
    if not re.fullmatch(r'visitaki-ingress-[0-9]+-[0-9]+', context):
        raise RuntimeError('Unexpected deployment Docker context')
    host = json.loads(_read('docker', 'context', 'inspect', context,
                         '--format', '{{json .Endpoints.docker.Host}}'))
    if host != 'ssh://makepad@135.181.141.31':
        raise RuntimeError('Deployment Docker endpoint changed')
    api = _read('docker', '--context', context, 'version', '--format', '{{.Server.APIVersion}}').strip()
    return _request_update(context, api, before, spec)


def _request_update(context, api, before, spec):
    """Internal primitive; production callers must use update_service's allowlist."""
    version = before['Version']['Index']
    if (not re.fullmatch(r'1\.[0-9]+', api) or not re.fullmatch(r'[a-z0-9]+', before['ID'])
            or type(version) is not int or version < 1 or spec.get('Name') != SERVICE):
        raise RuntimeError('Invalid version-bound service request')
    body = json.dumps(spec, separators=(',', ':')).encode()
    if len(body) > 1024 * 1024:
        raise RuntimeError('Service specification exceeds request bound')
    request = (f"POST /v{api}/services/{before['ID']}/update?version={version} HTTP/1.1\r\n"
               'Host: docker\r\nContent-Type: application/json\r\nConnection: close\r\n'
               f'Content-Length: {len(body)}\r\n\r\n').encode() + body
    try:
        result = _command(['docker', '--context', context, 'system', 'dial-stdio'], data=request)
        if result.returncode or len(result.stdout) > 72 * 1024:
            raise RuntimeError('Unacknowledged service update; reconcile before retrying')
        class Stream:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(result.stdout)
        response = http.client.HTTPResponse(Stream())
        response.begin()
        # stdout was bounded above; read() also rejects a truncated Content-Length.
        payload = response.read()
        if len(payload) > 64 * 1024 or response.status != 200:
            raise RuntimeError('Service update rejected; no automatic rollback')
        if not isinstance(json.loads(payload), dict):
            raise ValueError('Invalid acknowledgement')
    except (subprocess.TimeoutExpired, http.client.HTTPException, ValueError) as error:
        raise RuntimeError('Unacknowledged service update; reconcile before retrying') from error

