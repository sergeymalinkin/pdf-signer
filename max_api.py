"""MAX transport. Token stays in headers; media URLs never receive bot credentials."""
import ipaddress
import json
import socket
import ssl
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler

BASE = 'https://platform-api2.max.ru'
MEDIA_DOMAINS = ('max.ru', 'oneme.ru', 'okcdn.ru', 'mycdn.me')

class ApiError(Exception):
    def __init__(self, code, status=0):
        self.code, self.status = code, status
        super().__init__(str(code))

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def validate_media_url(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443)
            or not any(host == d or host.endswith('.'+d) for d in MEDIA_DOMAINS)):
        raise ValueError('Untrusted media host')
    addresses = socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Nonpublic media address')
    return url


class MaxAPI:
    def __init__(self, token, cafile=None):
        if not token: raise ValueError('MAX_BOT_TOKEN is missing')
        self.token = token
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
        self.last_call = 0

    def request(self, method, path, params=None, body=None):
        wait = .55 - (time.monotonic()-self.last_call)
        if wait > 0: time.sleep(wait)
        self.last_call = time.monotonic()
        url = BASE+path+('?' + urlencode(params) if params else '')
        data = json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        req = Request(url,data=data,method=method,headers={'Authorization':self.token,'Content-Type':'application/json'})
        try:
            with self.opener.open(req,timeout=40) as response:
                result = json.loads(response.read(2*1024*1024))
        except HTTPError as exc:
            try: error = json.loads(exc.read(4096)).get('code','HTTP_ERROR')
            except (ValueError,AttributeError): error='HTTP_ERROR'
            raise ApiError(error,exc.code) from None
        if isinstance(result,dict) and result.get('success') is False:
            raise ApiError('API_REJECTED')
        return result

    def send(self, *, user_id=None, chat_id=None, text='', attachments=None, reply_to=None):
        params = {'user_id':user_id} if user_id is not None else {'chat_id':chat_id}
        body = {'text':text}
        if attachments: body['attachments']=attachments
        if reply_to: body['link']={'type':'reply','mid':reply_to}
        # Retry only an explicit rejection that guarantees no message was created.
        for attempt in range(5):
            try: return self.request('POST','/messages',params,body)
            except ApiError as exc:
                if exc.code != 'attachment.not.ready' or attempt == 4: raise
                time.sleep(min(2**attempt,8))

    def answer(self, callback_id, text):
        return self.request('POST','/answers',{'callback_id':callback_id},{'notification':text})

    def message(self, mid):
        from urllib.parse import quote
        return self.request('GET','/messages/'+quote(mid,safe=''))

    def download(self, url, path, limit):
        for _ in range(4):
            validate_media_url(url)
            try:
                with self.opener.open(Request(url),timeout=40) as response:
                    if int(response.headers.get('Content-Length','0')) > limit: raise ValueError('PDF exceeds size limit')
                    total=0
                    with path.open('xb') as output:
                        while chunk := response.read(65536):
                            total += len(chunk)
                            if total > limit: raise ValueError('PDF exceeds size limit')
                            output.write(chunk)
                    if path.read_bytes()[:5] != b'%PDF-': raise ValueError('Not a PDF')
                    return
            except HTTPError as exc:
                if exc.code not in (301,302,303,307,308): raise ApiError('MEDIA_DOWNLOAD_FAILED',exc.code) from None
                url = urljoin(url,exc.headers['Location'])
        raise ValueError('Too many media redirects')

    def upload(self, path, filename='request_SIGNED.pdf'):
        from urllib.parse import quote
        upload = self.request('POST','/uploads',{'type':'file'})
        url=validate_media_url(upload['url'])
        boundary='pdf-signer-'+uuid.uuid4().hex
        # A safe ASCII upload filename; output filename remains separate locally.
        data=(f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="request_SIGNED.pdf"; filename*=UTF-8\'\'{quote(filename,safe="")}\r\nContent-Type: application/pdf\r\n\r\n'.encode()
              +path.read_bytes()+f'\r\n--{boundary}--\r\n'.encode())
        req=Request(url,data=data,method='POST',headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        with self.opener.open(req,timeout=90) as response:
            result=json.loads(response.read(65536))
        token=result.get('token')
        if not isinstance(token,str) or not token: raise ValueError('Upload did not return file token')
        return token
