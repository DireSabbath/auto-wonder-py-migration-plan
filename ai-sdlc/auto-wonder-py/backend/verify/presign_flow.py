"""用预签名地址对 MinIO 和本地 OSS 各做一次上传再下载。

访问密钥沿用 compose 里的 MinIO 账号。签名查询串不写入 verdict。
双栈部分调用 Java 的 S3ObjectStorage 与 AliyunOssObjectStorage，比对地址形态，
并用一边的 PUT 地址写入、另一边的 GET 地址读回。
"""

import base64
import hashlib
import hmac
import os
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from botocore.exceptions import ClientError

from autowonder.storage.oss import AliyunOssObjectStorage
from autowonder.storage.s3 import S3ObjectStorage

_MINIO = "http://127.0.0.1:9000"
_ACCESS = "autowonder"
_SECRET = "autowonder-secret"
_REGION = "us-east-1"
_MINIO_BUCKET = "aw-presign"
_OSS_BUCKET = "aw-oss-presign"
_OSS_KEY = "aw-oss-key"
_OSS_SECRET = "aw-oss-secret"
_KEY = "round/trip.bin"
_DUAL_KEY = "dual/stack.bin"
_BODY = b"presign-round-trip"
_DUAL_BODY = b"presign-dual-stack"
_TTL = 120
_JAVA_MAIN = r"""
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.time.Duration;

public class DualPresign {
    public static void main(String[] args) throws Exception {
        String mode = args[0];
        if ("s3".equals(mode)) {
            Class<?> type = Class.forName("com.aliyun.autowonder.storage.S3ObjectStorage");
            Constructor<?> ctor = type.getConstructor(
                    String.class, String.class, String.class,
                    String.class, String.class, boolean.class);
            Object storage = ctor.newInstance(
                    args[1], args[2], args[3], args[4], args[5],
                    Boolean.parseBoolean(args[6]));
            String bucket = args[7];
            String key = args[8];
            int ttl = Integer.parseInt(args[9]);
            Method put = type.getMethod(
                    "presignPut", String.class, String.class, Duration.class);
            Method get = type.getMethod("presignGet", String.class, int.class);
            System.out.println("PUT " + put.invoke(
                    storage, bucket, key, Duration.ofSeconds(ttl)));
            System.out.println("GET " + get.invoke(storage, bucket + "/" + key, ttl));
            return;
        }
        Class<?> type = Class.forName("com.aliyun.autowonder.storage.AliyunOssObjectStorage");
        Constructor<?> ctor = type.getConstructor(
                String.class, String.class, String.class, String.class);
        Object storage = ctor.newInstance(args[1], args[2], args[3], args[4]);
        String bucket = args[5];
        String key = args[6];
        int ttl = Integer.parseInt(args[7]);
        Method put = type.getMethod(
                "presignPut", String.class, String.class, Duration.class);
        Method get = type.getMethod("presignGet", String.class, int.class);
        System.out.println("PUT " + put.invoke(
                storage, bucket, key, Duration.ofSeconds(ttl)));
        System.out.println("GET " + get.invoke(storage, bucket + "/" + key, ttl));
    }
}
"""


def presign_flow(base_url: str) -> dict[str, object]:
    """签发 PUT 和 GET，确认两边读回同一段字节。"""
    del base_url
    chain = _Chain()
    server = _start_oss()
    try:
        chain.walk(server.base)
    finally:
        server.stop()
    return chain.verdict()


class _OssServer:
    def __init__(self, httpd: ThreadingHTTPServer, thread: threading.Thread, base: str) -> None:
        self.httpd = httpd
        self.thread = thread
        self.base = base

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    objects: dict[str, bytes] = {}

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        expires = _query_one(query, "Expires")
        signature = _query_one(query, "Signature")
        path = unquote(parsed.path)
        if not _signature_matches(method, path, expires, signature):
            self._reply(403, b"forbidden")
            return
        if int(expires) <= int(time.time()):
            self._reply(403, b"expired")
            return
        if method == "PUT":
            length = int(self.headers.get("Content-Length", "0"))
            _Handler.objects[path] = self.rfile.read(length)
            self._reply(200, b"")
            return
        stored = _Handler.objects.get(path)
        if stored is None:
            self._reply(404, b"missing")
            return
        self._reply(200, stored)

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Check:
    def __init__(self, name: str, passed: bool, note: str) -> None:
        self.name = name
        self.passed = passed
        self.note = note

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "pass": self.passed, "note": self.note}


class _Chain:
    def __init__(self) -> None:
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self.client = httpx.Client(timeout=30.0, verify=False)

    def close(self) -> None:
        self.client.close()

    def walk(self, oss_base: str) -> None:
        try:
            self._minio()
            if self.stopped != "":
                return
            self._oss(oss_base)
            if self.stopped != "":
                return
            self._dual(oss_base)
        finally:
            self.close()

    def verdict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "command": "presign",
            "ok": self.fail_count == 0 and self.stopped == "",
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped != "":
            body["stopped"] = self.stopped
        return body

    def _minio(self) -> None:
        _ensure_bucket()
        storage = S3ObjectStorage(_MINIO, _MINIO, _REGION, _ACCESS, _SECRET, True)
        put_url = storage.presign_put(_MINIO_BUCKET, _KEY, _TTL)
        put = self.client.put(put_url, content=_BODY)
        self.facts["minioPutStatus"] = put.status_code
        self._expect("minio_put", put.status_code == 200, "presigned PUT stored the object")
        if self.stopped != "":
            return
        get_url = storage.presign_get(_MINIO_BUCKET + "/" + _KEY, _TTL)
        got = self.client.get(get_url)
        self.facts["minioGetStatus"] = got.status_code
        self.facts["minioBytes"] = len(got.content)
        same = got.status_code == 200 and got.content == _BODY
        self._expect("minio_get", same, "presigned GET returned the same bytes")

    def _oss(self, oss_base: str) -> None:
        storage = AliyunOssObjectStorage(oss_base, oss_base, _OSS_KEY, _OSS_SECRET)
        put_url = storage.presign_put(_OSS_BUCKET, _KEY, _TTL)
        put = self.client.put(put_url, content=_BODY)
        self.facts["ossPutStatus"] = put.status_code
        self._expect("oss_put", put.status_code == 200, "OSS presigned PUT stored the object")
        if self.stopped != "":
            return
        get_url = storage.presign_get(_OSS_BUCKET + "/" + _KEY, _TTL)
        self.facts["ossGetScheme"] = urlsplit(get_url).scheme
        got = self.client.get(get_url)
        self.facts["ossGetStatus"] = got.status_code
        self.facts["ossBytes"] = len(got.content)
        same = got.status_code == 200 and got.content == _BODY
        https = urlsplit(get_url).scheme == "https"
        self._expect(
            "oss_get",
            same and https,
            "OSS presigned GET is https and returned the same bytes",
        )

    def _dual(self, oss_base: str) -> None:
        java_s3 = _java_presign(
            "s3",
            [
                _MINIO,
                _MINIO,
                _REGION,
                _ACCESS,
                _SECRET,
                "true",
                _MINIO_BUCKET,
                _DUAL_KEY,
                str(_TTL),
            ],
        )
        python_s3 = S3ObjectStorage(_MINIO, _MINIO, _REGION, _ACCESS, _SECRET, True)
        python_put = python_s3.presign_put(_MINIO_BUCKET, _DUAL_KEY, _TTL)
        python_get = python_s3.presign_get(_MINIO_BUCKET + "/" + _DUAL_KEY, _TTL)
        self._expect(
            "s3_put_shape",
            _s3_view(java_s3["PUT"]) == _s3_view(python_put),
            "Java and Python S3 PUT URLs share host, path, expiry and credential scope",
        )
        if self.stopped != "":
            return
        self._expect(
            "s3_get_shape",
            _s3_view(java_s3["GET"]) == _s3_view(python_get),
            "Java and Python S3 GET URLs share host, path, expiry and credential scope",
        )
        if self.stopped != "":
            return
        self._cross("s3_java_put_python_get", java_s3["PUT"], python_get, _DUAL_BODY)
        if self.stopped != "":
            return
        self._cross("s3_python_put_java_get", python_put, java_s3["GET"], _DUAL_BODY + b"-py")
        if self.stopped != "":
            return
        java_oss = _java_presign(
            "oss",
            [
                oss_base,
                oss_base,
                _OSS_KEY,
                _OSS_SECRET,
                _OSS_BUCKET,
                _DUAL_KEY,
                str(_TTL),
            ],
        )
        python_oss = AliyunOssObjectStorage(oss_base, oss_base, _OSS_KEY, _OSS_SECRET)
        oss_put = python_oss.presign_put(_OSS_BUCKET, _DUAL_KEY, _TTL)
        oss_get = python_oss.presign_get(_OSS_BUCKET + "/" + _DUAL_KEY, _TTL)
        self._expect(
            "oss_put_shape",
            _oss_view(java_oss["PUT"]) == _oss_view(oss_put),
            "Java and Python OSS PUT URLs share host, decoded path and query keys",
        )
        if self.stopped != "":
            return
        self._expect(
            "oss_get_shape",
            _oss_view(java_oss["GET"]) == _oss_view(oss_get),
            "Java and Python OSS GET URLs are https and share the decoded path",
        )
        if self.stopped != "":
            return
        self._cross("oss_java_put_python_get", java_oss["PUT"], oss_get, _DUAL_BODY)
        if self.stopped != "":
            return
        self._cross("oss_python_put_java_get", oss_put, java_oss["GET"], _DUAL_BODY + b"-py")

    def _cross(self, name: str, put_url: str, get_url: str, body: bytes) -> None:
        put = self.client.put(put_url, content=body)
        got = self.client.get(get_url)
        same = put.status_code == 200 and got.status_code == 200 and got.content == body
        self.facts[name + "Put"] = put.status_code
        self.facts[name + "Get"] = got.status_code
        self._expect(name, same, "the other stack read the bytes written by this stack")

    def _expect(self, name: str, passed: bool, note: str) -> None:
        self.checks.append(_Check(name, passed, note))
        if passed:
            self.pass_count += 1
            return
        self.fail_count += 1
        if self.stopped == "":
            self.stopped = name


def _start_oss() -> _OssServer:
    directory = tempfile.mkdtemp(prefix="aw-oss-")
    key_path = os.path.join(directory, "key.pem")
    cert_path = os.path.join(directory, "cert.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            key_path,
            "-out",
            cert_path,
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return _OssServer(httpd, thread, "https://" + str(host) + ":" + str(port))


def _ensure_bucket() -> None:
    import boto3
    from botocore.client import Config

    client = boto3.client(
        "s3",
        endpoint_url=_MINIO,
        region_name=_REGION,
        aws_access_key_id=_ACCESS,
        aws_secret_access_key=_SECRET,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        client.create_bucket(Bucket=_MINIO_BUCKET)
    except ClientError as error:
        code = ""
        response = error.response
        info = response.get("Error")
        if isinstance(info, dict):
            value = info.get("Code")
            if isinstance(value, str):
                code = value
        if code != "BucketAlreadyOwnedByYou" and code != "BucketAlreadyExists":
            raise


def _signature_matches(method: str, path: str, expires: str, signature: str) -> bool:
    if expires == "" or signature == "":
        return False
    canonical = "\n".join([method, "", "", expires, path])
    digest = hmac.new(_OSS_SECRET.encode(), canonical.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    if len(expected) != len(signature):
        return False
    return hmac.compare_digest(expected, signature)


def _java_presign(mode: str, args: list[str]) -> dict[str, str]:
    """编译反射入口，调用 fat jar 里的 Java 存储类签发地址。"""
    root = os.environ["AUTOWONDER_JAVA_ROOT"]
    jar = os.path.join(root, "target", "auto-wonder.jar")
    directory = tempfile.mkdtemp(prefix="aw-dual-presign-")
    source = os.path.join(directory, "DualPresign.java")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write(_JAVA_MAIN)
    subprocess.run(
        ["javac", "--release", "17", source],
        check=True,
        capture_output=True,
    )
    proc = subprocess.run(
        [
            "java",
            "-cp",
            jar,
            "-Dloader.main=DualPresign",
            "-Dloader.path=" + directory,
            "org.springframework.boot.loader.PropertiesLauncher",
            mode,
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        kind, url = line.split(" ", 1)
        found[kind] = url
    return found


def _s3_view(url: str) -> tuple[object, ...]:
    """去掉会随时间变化的签名和日期，留下可对拍的 S3 地址形态。"""
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    credential = query["X-Amz-Credential"][0].split("/")
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port,
        unquote(parsed.path),
        query["X-Amz-Algorithm"][0],
        query["X-Amz-Expires"][0],
        query["X-Amz-SignedHeaders"][0],
        credential[0],
        credential[2],
        credential[3],
        credential[4],
    )


def _oss_view(url: str) -> tuple[object, ...]:
    """去掉过期时刻和签名，留下可对拍的 OSS 地址形态。斜杠按解码后比较。"""
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port,
        unquote(parsed.path),
        tuple(sorted(query)),
        query["OSSAccessKeyId"][0],
    )


def _query_one(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name)
    if values is None or len(values) == 0:
        return ""
    return values[0]
