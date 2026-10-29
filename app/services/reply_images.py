"""Prepare configured images for IM: bounded public download -> JPEG -> platform CDN."""
import asyncio
import hashlib
import io
import ipaddress
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import aiohttp
from PIL import Image, ImageOps

MAX_BYTES = 5 * 1024 * 1024
CACHE_SECONDS = 86400
PLATFORM_DOMAINS = ('alicdn.com', 'taobaocdn.com', 'tbcdn.cn', 'aliimg.com', 'cloud.goofish.com')


class ReplyImageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def initialize(db):
    with db.lock:
        db.conn.execute('''CREATE TABLE IF NOT EXISTS auto_reply_image_cache (
            cookie_id TEXT NOT NULL REFERENCES cookies(id) ON DELETE CASCADE,
            source_hash TEXT NOT NULL,url TEXT NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,
            updated_at REAL NOT NULL,PRIMARY KEY(cookie_id,source_hash))''')
        db.conn.commit()


def is_platform_image_url(url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        return (parts.scheme in ('http', 'https') and not parts.username and not parts.password
                and any(host == domain or host.endswith('.' + domain) for domain in PLATFORM_DOMAINS))
    except ValueError:
        return False


def validate_public_url(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError()
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError()
        host = parts.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except (TypeError, ValueError):
        raise ReplyImageError('image_url_invalid') from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await self.resolver.resolve(host, port, family)
        if not rows or any(not ipaddress.ip_address(row['host']).is_global for row in rows):
            raise ReplyImageError('image_url_invalid')
        return rows

    async def close(self):
        await self.resolver.close()


async def download(url):
    validate_public_url(url)
    resolver = PublicResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver, limit=2)
        async with asyncio.timeout(15):
            async with aiohttp.ClientSession(trust_env=False, connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
                    timeout=aiohttp.ClientTimeout(total=15)) as session:
                for _ in range(4):
                    validate_public_url(url)
                    async with session.get(url, allow_redirects=False, headers={'Accept':'image/*'}) as response:
                        if response.status in (301,302,303,307,308):
                            location = response.headers.get('Location')
                            if not location:
                                raise ReplyImageError('image_download_failed')
                            url = urljoin(url, location)
                            continue
                        if response.status != 200:
                            raise ReplyImageError('image_download_failed')
                        if response.content_length is not None and response.content_length > MAX_BYTES:
                            raise ReplyImageError('image_too_large')
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            data.extend(chunk)
                            if len(data) > MAX_BYTES:
                                raise ReplyImageError('image_too_large')
                        return bytes(data)
                raise ReplyImageError('image_download_failed')
    except ReplyImageError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError):
        raise ReplyImageError('image_download_failed') from None
    finally:
        await resolver.close()


def jpeg_image(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ReplyImageError('image_too_large')
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ('JPEG','PNG','GIF','WEBP') or source.width * source.height > 8 * 1024 * 1024:
                raise ReplyImageError('image_decode_failed')
            source.seek(0)
            source = ImageOps.exif_transpose(source)
            rgba = source.convert('RGBA')
            image = Image.new('RGB', rgba.size, 'white')
            image.paste(rgba, mask=rgba.getchannel('A'))
            image.thumbnail((1920,1920), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, 'JPEG', quality=85, optimize=True)
            data = output.getvalue()
            if len(data) > MAX_BYTES:
                raise ReplyImageError('image_too_large')
            return data, image.width, image.height
    except ReplyImageError:
        raise
    except Exception:
        raise ReplyImageError('image_decode_failed') from None


async def prepare(instance, url, db=None):
    # Only actual platform hosts bypass upload. An HTTPS suffix is not CDN evidence.
    if is_platform_image_url(url):
        width, height = await instance._get_image_size_from_url(url)
        return url, width or 800, height or 600
    path = None
    signature = url
    if url.startswith(('/static/uploads/', 'static/uploads/')):
        path = Path(url.lstrip('/')).resolve()
        if not path.is_relative_to(Path('static/uploads').resolve()) or not path.is_file():
            raise ReplyImageError('image_file_missing')
        stat = path.stat()
        if stat.st_size > MAX_BYTES:
            raise ReplyImageError('image_too_large')
        signature += f'|{stat.st_mtime_ns}|{stat.st_size}'
    else:
        validate_public_url(url)
    key = hashlib.sha256(signature.encode()).hexdigest()
    locks = getattr(instance, '_reply_image_locks', None)
    if locks is None:
        locks = instance._reply_image_locks = {}
    async with locks.setdefault(key, asyncio.Lock()):
        if db is not None:
            with db.lock:
                row = db.conn.execute('SELECT url,width,height FROM auto_reply_image_cache WHERE cookie_id=? AND source_hash=? AND updated_at>?',
                    (instance.cookie_id,key,time.time()-CACHE_SECONDS)).fetchone()
            if row and is_platform_image_url(row[0]):
                return tuple(row)
        raw = await asyncio.to_thread(path.read_bytes) if path else await download(url)
        clean, width, height = await asyncio.to_thread(jpeg_image, raw)
        from utils.image_uploader import ImageUploader
        with tempfile.TemporaryDirectory(prefix='bygoofish-reply-image-') as folder:
            upload = Path(folder)/'reply.jpg'
            upload.write_bytes(clean);upload.chmod(0o600)
            async with ImageUploader(instance.cookies_str) as uploader:
                result = await uploader.upload_image(str(upload))
            if not result or not is_platform_image_url(result):
                raise ReplyImageError('image_upload_failed')
        if db is not None:
            with db.lock:
                db.conn.execute('INSERT OR REPLACE INTO auto_reply_image_cache VALUES(?,?,?,?,?,?)',
                    (instance.cookie_id,key,result,width,height,time.time()))
                db.conn.execute('DELETE FROM auto_reply_image_cache WHERE updated_at<?',(time.time()-7*86400,))
                db.conn.commit()
        return result, width, height
