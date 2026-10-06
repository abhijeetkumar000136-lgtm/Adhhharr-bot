import requests
import socket
import json
import base64
import uuid
import re
from datetime import datetime, timedelta
import os
import sys
import time
import logging
import html
from io import BytesIO
from urllib.parse import urlparse, urlunparse
try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import random
import string
try:
    from pymongo import MongoClient, ReturnDocument
except ImportError:
    MongoClient = None
    ReturnDocument = None
import certifi
try:
    import ddddocr
except ImportError:
    ddddocr = None

# ================= RENDER KEEP-ALIVE WEB SERVER =================
from flask import Flask

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Bot is running healthy 24/7!"

def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

# Run lightweight web server in background thread so Render marks service as healthy
threading.Thread(target=start_web_server, daemon=True).start()
# ===============================================================

SPINNER_FRAMES = [
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"
]
try:
    from PIL import Image, ImageFilter, ImageEnhance
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

ocr_solver_beta = ddddocr.DdddOcr(show_ad=False, beta=True) if ddddocr else None
ocr_solver_std  = ddddocr.DdddOcr(show_ad=False, beta=False) if ddddocr else None

def preprocess_captcha(image_bytes):
    if not _PIL_AVAILABLE:
        return image_bytes
    try:
        img = Image.open(BytesIO(image_bytes)).convert('L')
        new_w = img.width * 2
        new_h = img.height * 2
        img = img.resize((new_w, new_h), Image.LANCZOS)
        img = img.filter(ImageFilter.SHARPEN)
        img = ImageEnhance.Contrast(img).enhance(2.2)
        out = BytesIO()
        img.save(out, format='PNG')
        return out.getvalue()
    except Exception:
        return image_bytes

def solve_captcha_robust(image_bytes, chat_id="system"):
    start_time = time.time()
    captcha_code = ""

    try:
        prep_bytes = preprocess_captcha(image_bytes)
        r = ocr_solver_beta.classification(prep_bytes)
        if r:
            code = re.sub(r'[^a-zA-Z0-9]', '', r.strip())
            if len(code) == 6:
                captcha_code = code
    except Exception:
        pass

    if not captcha_code:
        try:
            r = ocr_solver_beta.classification(image_bytes)
            if r:
                code = re.sub(r'[^a-zA-Z0-9]', '', r.strip())
                if len(code) == 6:
                    captcha_code = code
        except Exception:
            pass

    ocr_dur = time.time() - start_time
    if captcha_code:
        log_activity(chat_id, "solve_captcha", "success", duration=ocr_dur)
    else:
        log_activity(chat_id, "solve_captcha", "failed", duration=ocr_dur, error="Failed to solve captcha locally")

    return captcha_code

def get_session_proxy(proxy_url: str, session_id: int) -> str:
    if not proxy_url:
        return None
    try:
        parsed = urlparse(proxy_url)
        if parsed.username:
            session_suffix = f"-{session_id}"
            if parsed.username.endswith(session_suffix):
                return proxy_url
            new_username = f"{parsed.username}-{session_id}"
            netloc = f"{new_username}:{parsed.password}@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        pass
    return proxy_url

UIDAI_PROXIES = []
PROXY_CONFIG = {
    'use_proxy': bool(UIDAI_PROXIES) or bool(os.environ.get('PROXY_URL', '').strip()),
    'http': None,
    'https': None
}

_proxy_failures: dict = {p: 0 for p in UIDAI_PROXIES}
_proxy_lock = threading.Lock()

def _pick_proxy() -> str:
    with _proxy_lock:
        if not UIDAI_PROXIES:
            return None
        min_fail = min(_proxy_failures.get(p, 0) for p in UIDAI_PROXIES)
        candidates = [p for p in UIDAI_PROXIES if _proxy_failures.get(p, 0) == min_fail]
        return random.choice(candidates)

def _mark_proxy_ok(proxy_url: str):
    with _proxy_lock:
        _proxy_failures[proxy_url] = 0

def _mark_proxy_fail(proxy_url: str):
    with _proxy_lock:
        _proxy_failures[proxy_url] = _proxy_failures.get(proxy_url, 0) + 1
        logger.warning(f"Proxy failure count for {proxy_url.split('@')[-1]}: {_proxy_failures[proxy_url]}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8947656897:AAGzLv6reIl1hZU2gIJMNCVWM63wE20ZWrU")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "8669812402"))

class FastHTTPAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, socket_options=None, **kwargs):
        self.socket_options = socket_options
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        if self.socket_options is not None:
            pool_kwargs['socket_options'] = self.socket_options
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        if self.socket_options is not None:
            proxy_kwargs['socket_options'] = self.socket_options
        return super().proxy_manager_for(proxy, **proxy_kwargs)

def create_session(use_proxy=False, proxy_string=None):
    session = requests.Session()
    socket_opts = [
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.SOL_SOCKET, socket.SO_RCVBUF, 524288),
        (socket.SOL_SOCKET, socket.SO_SNDBUF, 262144),
    ]
    adapter = FastHTTPAdapter(
        socket_options=socket_opts,
        pool_connections=50,
        pool_maxsize=50,
        max_retries=1,
        pool_block=False
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        'Connection': 'keep-alive',
        'Accept-Encoding': 'gzip, deflate',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    if use_proxy and proxy_string:
        parsed = urlparse(proxy_string)
        proxy_url = f"{parsed.scheme}://{parsed.netloc}"
        session.proxies = {'http': proxy_url, 'https': proxy_url}
    return session

captcha_prefetch_cache = {}
captcha_prefetch_lock = threading.Lock()

def prefetch_captcha_async(chat_id, revamp=False):
    def _worker():
        try:
            image_bytes, captcha_txn_id, transaction_id, captcha_proxy = bot.get_captcha(chat_id, revamp=revamp)
            if image_bytes:
                captcha_code = solve_captcha_robust(image_bytes, chat_id)
                if captcha_code and len(captcha_code) >= 5:
                    with captcha_prefetch_lock:
                        captcha_prefetch_cache[chat_id] = {
                            'image_bytes': image_bytes,
                            'captcha_txn_id': captcha_txn_id,
                            'transaction_id': transaction_id,
                            'captcha_proxy': captcha_proxy,
                            'captcha_code': captcha_code,
                            'time': time.time(),
                            'revamp': revamp
                        }
        except Exception as e:
            logger.debug(f"Prefetch captcha error: {e}")
    threading.Thread(target=_worker, daemon=True).start()

def get_prefetched_captcha(chat_id, revamp=False):
    with captcha_prefetch_lock:
        item = captcha_prefetch_cache.pop(chat_id, None)
        if item and item.get('revamp') == revamp and (time.time() - item['time'] < 90):
            return item
    return None

thread_local = threading.local()

def reset_uidai_session():
    thread_local.uidai_session = None

_telegram_global_session = None
_telegram_session_lock = threading.Lock()

def get_telegram_session():
    global _telegram_global_session
    if _telegram_global_session is None:
        with _telegram_session_lock:
            if _telegram_global_session is None:
                s = requests.Session()
                socket_opts = [
                    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                ]
                adapter = FastHTTPAdapter(
                    socket_options=socket_opts,
                    pool_connections=100, pool_maxsize=100, max_retries=2, pool_block=False
                )
                s.mount('https://', adapter)
                s.mount('http://', adapter)
                s.headers.update({
                    'Connection': 'keep-alive',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                _telegram_global_session = s
    return _telegram_global_session

def get_uidai_session(proxy_hint: str | None = None, session_id: int | None = None, force_direct: bool = False):
    if not force_direct and PROXY_CONFIG['use_proxy']:
        env_proxy = os.environ.get('PROXY_URL', '').strip()
        if env_proxy:
            return create_session(True, env_proxy), env_proxy
        elif UIDAI_PROXIES:
            proxy = proxy_hint if proxy_hint else _pick_proxy()
            if session_id and not proxy_hint:
                safe_session_id = (abs(int(session_id)) % 99999) + 1
                proxy = get_session_proxy(proxy, safe_session_id)
            if _proxy_failures.get(proxy, 0) < 3:
                return create_session(True, proxy), proxy
    return create_session(False), None

def clean_user_error(err_str: str) -> str:
    if not err_str:
        return "Government Portal Busy / Technical Error. Please try again."
    err_lower = str(err_str).lower()
    if any(x in err_lower for x in ["httpsconnectionpool", "connection", "read timed out", "timeout", "timed out", "ssl", "max retries", "socket", "proxy"]):
        return "Government Portal Busy / Network Timeout. Please retry."
    if "captcha" in err_lower:
        return "Government Portal Busy / Captcha Server Error. Please try again."
    if any(x in err_lower for x in ["technical difficulties", "server error", "500", "502", "503", "service unavailable"]):
        return "Government Portal is experiencing technical difficulties. Please try again."
    if "no record" in err_lower or "not found" in err_lower:
        return "No Records Found for the provided details."
    if "permissible limit" in err_lower or "exceeded" in err_lower:
        return "OTP limit exceeded. Please request a NEW OTP after a few minutes."
    if "otp" in err_lower and any(x in err_lower for x in ["invalid", "incorrect", "expired", "wrong"]):
        return "Invalid or Expired OTP. Please retry."
    if "eid" in err_lower and ("invalid" in err_lower or "format" in err_lower):
        return "Invalid EID format. Please check and retry."
    if any(tech in err_lower for tech in ["http", "host", "port", "traceback", "exception", "line ", ".py"]):
        return "Govt Portal Technical Error. Please try again."
    clean = re.sub(r'\s+', ' ', str(err_str)).strip()
    return clean[:50] if len(clean) > 50 else clean

class PDFPasswordCracker:
    def __init__(self):
        self.found_password = None
        self.stop_flag = False

    def decrypt_pdf(self, pdf_path, password, output_path=None):
        try:
            if output_path is None:
                output_path = pdf_path.replace('.pdf', '_decrypted.pdf')
            with open(pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                pdf_reader.decrypt(password)
                pdf_writer = PyPDF2.PdfWriter()
                for page in pdf_reader.pages:
                    pdf_writer.add_page(page)
                with open(output_path, 'wb') as output_file:
                    pdf_writer.write(output_file)
            return output_path
        except Exception:
            return None

    def crack_pdf(self, pdf_path, name, dob=None, progress_callback=None):
        self.found_password = None
        self.stop_flag = False
        try:
            with open(pdf_path, 'rb') as f:
                pdf_bytes = f.read()
        except Exception:
            return False, None, None

        name_upper = name.upper()
        unique_prefixes = []
        name_letters = "".join(c for c in name_upper if c.isalpha())
        if name_letters:
            unique_prefixes.append(name_letters)
        name_no_spaces = name_upper.replace(" ", "")
        if name_no_spaces:
            unique_prefixes.append(name_no_spaces)
        words = [w for w in name_upper.split() if w]
        if words:
            unique_prefixes.append(words[0])
        unique_prefixes.append(name_upper)

        prefixes_to_use = list(dict.fromkeys(p.strip() for p in unique_prefixes if p.strip()))

        birth_year = None
        if dob:
            match = re.search(r'\b(19\d\d|20[0-2]\d)\b', str(dob))
            if match:
                birth_year = int(match.group(1))

        if birth_year:
            common_years = [birth_year, birth_year - 1, birth_year + 1]
        else:
            current_year = datetime.now().year
            common_years = list(range(1980, 2011)) + list(range(1970, 1980)) + list(range(2011, current_year + 1)) + list(range(1940, 1970))

        candidates = []
        for year in common_years:
            for pref in prefixes_to_use:
                candidates.append(f"{pref[:4]}{year}")
        for year in common_years:
            for pref in prefixes_to_use:
                candidates.append(f"{pref[:4].lower()}{year}")
                candidates.append(f"{pref[:4].title()}{year}")

        prioritized = list(dict.fromkeys(candidates))
        num_workers = 16
        chunk_size = (len(prioritized) + num_workers - 1) // num_workers
        chunks = [prioritized[i:i+chunk_size] for i in range(0, len(prioritized), chunk_size)]
        state = {'found': False, 'password': None}

        def worker_task(pdf_data, password_chunk, worker_state):
            try:
                reader = PyPDF2.PdfReader(BytesIO(pdf_data))
                for pwd in password_chunk:
                    if worker_state['found'] or self.stop_flag:
                        break
                    if reader.decrypt(pwd):
                        worker_state['found'] = True
                        worker_state['password'] = pwd
                        self.stop_flag = True
                        return True, pwd
            except Exception:
                pass
            return False, None

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker_task, pdf_bytes, chunk, state) for chunk in chunks]
            for future in as_completed(futures):
                try:
                    success, found_pwd = future.result()
                    if success:
                        self.found_password = found_pwd
                except Exception:
                    pass

        if self.found_password:
            decrypted_path = self.decrypt_pdf(pdf_path, self.found_password)
            return True, self.found_password, decrypted_path
        return False, None, None

class AadhaarBot:
    def __init__(self):
        self.base_headers = {
            'Accept': 'application/json, text/plain, */*',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'en_IN',
            'Content-Type': 'application/json',
            'Origin': 'https://myaadhaar.uidai.gov.in',
            'Referer': 'https://myaadhaar.uidai.gov.in/',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
            'appid': 'MYAADHAAR',
        }
        self.stable_download_headers = self.base_headers.copy()
        self.stable_download_headers['Accept-Encoding'] = 'gzip, deflate, br, zstd'
        self.cracker = PDFPasswordCracker()

    def _get_sess(self, proxy_hint=None, session_id=None, force_direct=False):
        sess, proxy_url = get_uidai_session(proxy_hint, session_id, force_direct=force_direct)
        sess.headers.update(self.base_headers)
        return sess, proxy_url

    def _get_sess_revamp(self, proxy_hint=None, session_id=None, force_direct=False):
        sess, proxy_url = get_uidai_session(proxy_hint, session_id, force_direct=force_direct)
        revamp_headers = self.base_headers.copy()
        revamp_headers.update({
            "Origin": "https://myaadhaarbeta.uidai.gov.in",
            "Referer": "https://myaadhaarbeta.uidai.gov.in/",
            "appid": "MYAADHAARREVAMP",
        })
        sess.headers.update(revamp_headers)
        return sess, proxy_url

    def generate_transaction_id(self):
        return str(uuid.uuid4())

    def is_base64(self, s):
        if not isinstance(s, str) or len(s) < 100:
            return False
        if s.startswith('data:'):
            s = s.split(',')[1] if ',' in s else s
        return len(s) % 4 == 0

    def detect_file_type(self, file_bytes):
        if file_bytes[:4] == b'%PDF':
            return 'pdf'
        elif file_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            return 'png'
        elif file_bytes[:2] == b'\xff\xd8':
            return 'jpg'
        return 'unknown'

    def extract_pdf_bytes_fast(self, data):
        if isinstance(data, dict):
            for key in ['aadhaarPdf', 'pdfData', 'pdf', 'fileBytes', 'file', 'data', 'byteArray', 'aadhaarData']:
                val = data.get(key)
                if isinstance(val, str) and len(val) > 100:
                    try:
                        clean_b64 = val.split(',')[1] if val.startswith('data:') and ',' in val else val
                        b = base64.b64decode(clean_b64)
                        if self.detect_file_type(b) == 'pdf':
                            return b
                    except Exception:
                        pass
        return None

    def get_captcha(self, user_id, _retries=3, _delay=0.1, revamp=False):
        captcha_data = {'captchaLength': '6', 'captchaType': '2', 'audioCaptchaRequired': False}
        for attempt in range(1, _retries + 1):
            transaction_id = self.generate_transaction_id()
            reset_uidai_session()
            if attempt > 1:
                time.sleep(_delay)
            proxy_url = None
            try:
                force_direct = (attempt == _retries)
                sess, proxy_url = self._get_sess_revamp(session_id=user_id, force_direct=force_direct) if revamp else self._get_sess(session_id=user_id, force_direct=force_direct)
                sess.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
                response = sess.post('https://tathya.uidai.gov.in/audioCaptchaService/api/captcha/v3/generation', json=captcha_data, timeout=(15, 20))
                if response.status_code != 200:
                    if proxy_url: _mark_proxy_fail(proxy_url)
                    continue
                resp_json = response.json()
                captcha_txn_id = resp_json.get('transactionId')
                captcha_base64 = resp_json.get('imageBase64')
                if not captcha_base64:
                    continue
                if captcha_base64.startswith('data:image'):
                    captcha_base64 = captcha_base64.split(',')[1]
                image_bytes = base64.b64decode(captcha_base64)
                if proxy_url: _mark_proxy_ok(proxy_url)
                return image_bytes, captcha_txn_id, transaction_id, proxy_url
            except Exception:
                if proxy_url: _mark_proxy_fail(proxy_url)
        return None, None, None, None

    def send_aadhaar_otp(self, user_id, eid_number, captcha_value, captcha_txn_id, transaction_id, proxy_hint=None):
        sess, proxy_url = self._get_sess(proxy_hint, session_id=user_id)
        sess.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
        clean_id = str(eid_number).strip().replace(' ', '').replace('/', '')
        otp_request_data = {
            'uidNumber' if len(clean_id) == 12 else 'eidNumber': clean_id,
            'idType': 'uid' if len(clean_id) == 12 else 'eid',
            'captchaTxnId': captcha_txn_id, 'captchaValue': captcha_value,
            'transactionId': transaction_id, 'resendOTP': False
        }
        for attempt in range(1, 4):
            try:
                response = sess.post('https://tathya.uidai.gov.in/unifiedAppAuthService/api/v2/generate/aadhaar/otp', json=otp_request_data, timeout=20)
                if response.status_code == 200:
                    resp_json = response.json()
                    otp_txn_id = resp_json.get('txnId') or resp_json.get('transactionId') or resp_json.get('otpTxnId')
                    if otp_txn_id and resp_json.get('status') == "Success":
                        if proxy_url: _mark_proxy_ok(proxy_url)
                        return True, otp_txn_id, resp_json.get('message')
                    return False, None, resp_json.get('message')
            except Exception:
                sess, proxy_url = self._get_sess(None, session_id=user_id, force_direct=(attempt >= 2))
                sess.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
                time.sleep(0.5)
        return False, None, "Failed to connect to UIDAI service"

    def download_aadhaar_pdf(self, user_id, eid_number, otp, otp_txn_id, transaction_id, mask=False, proxy_hint=None):
        clean_id = str(eid_number).strip().replace(' ', '').replace('/', '')
        download_data = {
            'uidNumber' if len(clean_id) == 12 else 'eid': clean_id,
            'mask': mask, 'otp': str(otp).strip(), 'otpTxnId': otp_txn_id
        }
        if len(clean_id) == 12:
            download_data['uid'] = clean_id

        sess, proxy_url = self._get_sess(proxy_hint, session_id=user_id)
        sess.headers.update(self.stable_download_headers)
        sess.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
        try:
            response = sess.post('https://tathya.uidai.gov.in/downloadAadhaarService/api/aadhaar/download', json=download_data, timeout=90)
            if response.status_code == 200:
                resp_json = response.json()
                pdf_bytes = self.extract_pdf_bytes_fast(resp_json)
                if pdf_bytes:
                    filename = os.path.abspath(f"decoded_pdf_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
                    with open(filename, 'wb') as f:
                        f.write(pdf_bytes)
                    return True, filename
            return False, "Download failed or invalid OTP"
        except Exception as e:
            return False, str(e)

    def send_eid_otp(self, user_id, mobile, name, captcha_code, captcha_txn_id, transaction_id, proxy_hint=None):
        reset_uidai_session()
        sess, proxy_url = self._get_sess_revamp(proxy_hint, session_id=user_id)
        sess.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
        request_data = {
            'mobileNumber': mobile, 'dob': None, 'email': None,
            'name': name.upper(), 'option': 'EID', 'otp': None,
            'otpTxnId': None, 'captchaTxnId': captcha_txn_id,
            'captcha': captcha_code, 'resendOtp': False
        }
        try:
            response = sess.post('https://tathya.uidai.gov.in/retrieveEidUid/ext/v1/generic/retrieveuideid', json=request_data, timeout=45)
            if response.status_code == 200:
                resp_json = response.json()
                resp_data = resp_json.get('responseData', {})
                if resp_data.get('otpTxnId') and resp_data.get('status') == "Success":
                    return True, resp_data.get('otpTxnId')
                return False, resp_data.get('message', 'Failed to send OTP')
            return False, f"HTTP {response.status_code}"
        except Exception as e:
            return False, str(e)

    def verify_eid_otp(self, user_id, mobile, name, otp_code, otp_txn_id, captcha_txn_id, captcha_code, proxy_hint=None):
        reset_uidai_session()
        sess, proxy_url = self._get_sess_revamp(proxy_hint, session_id=user_id)
        sess.headers.update({'x-request-id': self.generate_transaction_id()})
        verify_data = {
            'mobileNumber': mobile, 'dob': None, 'name': name.upper(),
            'email': None, 'option': 'EID', 'otp': otp_code,
            'otpTxnId': otp_txn_id, 'captchaTxnId': captcha_txn_id,
            'captcha': captcha_code, 'resendOtp': False
        }
        try:
            response = sess.post('https://tathya.uidai.gov.in/retrieveEidUid/ext/v1/generic/retrieveuideid', json=verify_data, timeout=45)
            if response.status_code == 200:
                resp_data = response.json().get('responseData')
                if isinstance(resp_data, dict) and resp_data.get('eidNumber'):
                    return True, resp_data.get('eidNumber'), resp_data.get('name', name), resp_data.get('dob')
            return False, None, "Verification failed", None
        except Exception as e:
            return False, None, str(e), None

    def send_uid_sms_otp(self, user_id, mobile, name, captcha_code, captcha_txn_id, transaction_id, proxy_hint=None):
        reset_uidai_session()
        sess, proxy_url = self._get_sess_revamp(proxy_hint, session_id=user_id)
        sess.headers.update({'x-request-id': transaction_id, 'transactionId': transaction_id})
        request_data = {
            'mobileNumber': mobile, 'dob': None, 'email': None,
            'name': name.upper(), 'option': 'UID', 'otp': None,
            'otpTxnId': None, 'captchaTxnId': captcha_txn_id,
            'captcha': captcha_code, 'resendOtp': False
        }
        try:
            response = sess.post('https://tathya.uidai.gov.in/retrieveEidUid/ext/v1/generic/retrieveuideid', json=request_data, timeout=45)
            if response.status_code == 200:
                resp_data = response.json().get('responseData', {})
                if resp_data.get('otpTxnId') and resp_data.get('status') == "Success":
                    return True, resp_data.get('otpTxnId')
            return False, "Failed to send SMS OTP"
        except Exception as e:
            return False, str(e)

    def verify_uid_sms_otp(self, user_id, mobile, name, otp_code, otp_txn_id, captcha_txn_id, captcha_code, proxy_hint=None):
        reset_uidai_session()
        sess, proxy_url = self._get_sess_revamp(proxy_hint, session_id=user_id)
        sess.headers.update({'x-request-id': self.generate_transaction_id()})
        verify_data = {
            'mobileNumber': mobile, 'dob': None, 'name': name.upper(),
            'email': None, 'option': 'UID', 'otp': otp_code,
            'otpTxnId': otp_txn_id, 'captchaTxnId': captcha_txn_id,
            'captcha': captcha_code, 'resendOtp': False
        }
        try:
            response = sess.post('https://tathya.uidai.gov.in/retrieveEidUid/ext/v1/generic/retrieveuideid', json=verify_data, timeout=45)
            if response.status_code == 200:
                resp_json = response.json()
                resp_data = resp_json.get('responseData', {})
                uid_num = resp_data.get('uidNumber') or resp_data.get('uid')
                if uid_num or str(resp_json.get('status')).lower() == 'success':
                    return True, uid_num, resp_data.get('name', name), resp_data.get('dob', 'N/A')
            return False, None, "Verification failed", None
        except Exception as e:
            return False, None, str(e), None

    def crack_pdf_with_name(self, pdf_path, name, dob=None, progress_callback=None):
        return self.cracker.crack_pdf(pdf_path, name, dob, progress_callback) + (None,)

bot = AadhaarBot()

DIVIDER = "━━━━━━━━━━━━━━━"
BOT_NAME = "✜ 𝐌𝐨𝐝𝐱𝐀𝐚𝐝𝐡𝐚𝐫"
SESSION_TIMEOUT = 180

class _MemoryCollection:
    def __init__(self):
        self._docs = {}
        self._lock = threading.RLock()

    def find_one(self, query):
        with self._lock:
            return dict(self._docs[query['_id']]) if query.get('_id') in self._docs else None

    def insert_one(self, document):
        with self._lock:
            doc = dict(document)
            self._docs[doc.setdefault('_id', str(uuid.uuid4()))] = doc

    def update_one(self, query, update, upsert=False):
        with self._lock:
            key = query.get('_id')
            if key not in self._docs:
                if not upsert: return
                self._docs[key] = {'_id': key}
            doc = self._docs[key]
            for field, val in update.get('$set', {}).items(): doc[field] = val
            for field, val in update.get('$inc', {}).items(): doc[field] = doc.get(field, 0) + val

users_col = _MemoryCollection()
logs_col = _MemoryCollection()

def log_activity(chat_id, action, status, duration=0.0, error=None):
    pass

def ensure_user(user_id, referrer_id=None, first_name=None, username=None):
    uid = str(user_id)
    if users_col.find_one({"_id": uid}) is None:
        users_col.insert_one({"_id": uid, "first_name": first_name or "Unknown", "username": username, "searches": 0})
        return True
    return False

user_sessions = {}
_sessions_lock = threading.RLock()

def get_session(chat_id):
    with _sessions_lock:
        return user_sessions.get(chat_id, {'step': 'main', 'data': {}, 'last_activity': time.time()})

def set_session(chat_id, step, data=None):
    with _sessions_lock:
        d = data if data is not None else user_sessions.get(chat_id, {}).get('data', {})
        user_sessions[chat_id] = {'step': step, 'data': d, 'last_activity': time.time()}

def clear_session(chat_id):
    with _sessions_lock:
        user_sessions[chat_id] = {'step': 'main', 'data': {}, 'last_activity': time.time()}

def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        return get_telegram_session().post(url, json=data, timeout=10).json()
    except Exception:
        return None

def send_photo(chat_id, photo, caption="", reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    data = {'chat_id': chat_id, 'parse_mode': 'HTML', 'caption': caption}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        files = {'photo': ('captcha.png', photo, 'image/png')}
        return get_telegram_session().post(url, data=data, files=files, timeout=20).json()
    except Exception:
        return None

def edit_message(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    data = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        return get_telegram_session().post(url, json=data, timeout=10).json()
    except Exception:
        return send_message(chat_id, text, reply_markup)

def delete_message(chat_id, message_id):
    if not message_id: return
    try:
        get_telegram_session().post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage", json={'chat_id': chat_id, 'message_id': message_id}, timeout=5)
    except Exception:
        pass

def send_document(chat_id, file_path, caption=None, filename="Aadhaar.pdf"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, 'rb') as f:
            files = {'document': (filename, f, 'application/pdf')}
            data = {'chat_id': chat_id, 'parse_mode': 'HTML', 'caption': caption}
            return get_telegram_session().post(url, data=data, files=files, timeout=30).json()
    except Exception:
        return None

def get_main_keyboard():
    return {
        'keyboard': [
            ['◆  Mobile Number', '◆  Aadhaar Number'],
            ['◆  EID', '◆  Aadhaar SMS'],
            ['◇  About Bot']
        ],
        'resize_keyboard': True
    }

def get_cancel_keyboard():
    return {'inline_keyboard': [[{'text': '✗  Cancel', 'callback_data': 'cancel'}]]}

def deliver_pdf(chat_id, pdf_path, verified_name, dob=None):
    try:
        crack_success, password, decrypted_path, _ = bot.crack_pdf_with_name(pdf_path, verified_name, dob)
        path_to_send = decrypted_path if crack_success and decrypted_path and os.path.exists(decrypted_path) else pdf_path
        caption = f"<b>{BOT_NAME}</b>\n{DIVIDER}\n<b>〔 Document Ready ✓ 〕</b>\n\nName: {verified_name}\nStatus: {'Unlocked' if crack_success else 'Protected'}"
        send_document(chat_id, path_to_send, caption=caption)
        for p in {pdf_path, decrypted_path}:
            if p and os.path.exists(p): os.remove(p)
    except Exception as e:
        logger.error(f"Delivery error: {e}")
    finally:
        clear_session(chat_id)
        send_message(chat_id, "Download complete! Select an option below for another search.", reply_markup=get_main_keyboard())

def handle_callback(chat_id, callback_query_id, data, message_id=None):
    if data == 'cancel':
        clear_session(chat_id)
        edit_message(chat_id, message_id, "Session cancelled.")
        return

    session = get_session(chat_id)
    d = session.get('data', {})

    if data in ('eid_name_auto', 'aadhaar_sms_name_auto'):
        mobile = d.get('mobile')
        if not mobile: return
        set_session(chat_id, 'awaiting_otp' if data == 'eid_name_auto' else 'awaiting_aadhaar_sms_otp', {**d, 'name': 'MR'})
        edit_message(chat_id, message_id, "Processing request...")
        flow_func = bot.send_eid_otp if data == 'eid_name_auto' else bot.send_uid_sms_otp
        # Prefetch or fetch captcha
        img, c_txn, txn, proxy = bot.get_captcha(chat_id, revamp=True)
        code = solve_captcha_robust(img, chat_id)
        if code:
            ok, res = flow_func(chat_id, mobile, 'MR', code, c_txn, txn, proxy)
            if ok:
                set_session(chat_id, 'awaiting_otp' if data == 'eid_name_auto' else 'awaiting_aadhaar_sms_otp', {**d, 'eid_otp_txn_id': res, 'captcha1_txn_id': c_txn, 'captcha_code': code, 'proxy_hint': proxy, 'name': 'MR'})
                send_message(chat_id, "OTP Sent! Enter 6-digit OTP:", reply_markup=get_cancel_keyboard())
            else:
                send_message(chat_id, f"Error: {res}")
        return

def handle_message(chat_id, msg):
    text = msg.get('text', '').strip()
    ensure_user(chat_id, first_name=msg.get('from', {}).get('first_name'))

    lower = text.lower()
    if lower in ['/cancel', 'cancel']:
        clear_session(chat_id)
        send_message(chat_id, "Session cancelled.", reply_markup=get_main_keyboard())
        return

    if 'mobile number' in lower:
        set_session(chat_id, 'awaiting_mobile')
        send_message(chat_id, "Enter 10-digit mobile number:", reply_markup=get_cancel_keyboard())
        return
    elif 'aadhaar number' in lower:
        set_session(chat_id, 'awaiting_aadhaar')
        send_message(chat_id, "Enter 12-digit Aadhaar number:", reply_markup=get_cancel_keyboard())
        return

    session = get_session(chat_id)
    step = session.get('step')
    d = session.get('data', {})

    if step == 'awaiting_mobile':
        if re.match(r'^\d{10}$', text):
            set_session(chat_id, 'awaiting_eid_name_choice', {**d, 'mobile': text})
            kb = {'inline_keyboard': [[{'text': 'Auto Name (Mr)', 'callback_data': 'eid_name_auto'}], [{'text': 'Cancel', 'callback_data': 'cancel'}]]}
            send_message(chat_id, "Select name type:", reply_markup=kb)
        else:
            send_message(chat_id, "Please enter a valid 10-digit number.")
    elif step == 'awaiting_otp':
        if re.match(r'^\d{6}$', text):
            send_message(chat_id, "Verifying OTP...")
            ok, eid, name, dob = bot.verify_eid_otp(chat_id, d['mobile'], d.get('name', 'MR'), text, d['eid_otp_txn_id'], d['captcha1_txn_id'], d['captcha_code'], d.get('proxy_hint'))
            if ok:
                send_message(chat_id, f"Verified! EID: {eid}\nFetching PDF...")
                img, c_txn, txn, proxy = bot.get_captcha(chat_id)
                code = solve_captcha_robust(img, chat_id)
                ok_otp, pdf_txn, _ = bot.send_aadhaar_otp(chat_id, eid, code, c_txn, txn, proxy)
                if ok_otp:
                    set_session(chat_id, 'awaiting_pdf_otp', {**d, 'eid': eid, 'verified_name': name, 'dob': dob, 'pdf_otp_txn_id': pdf_txn, 'transaction_id2': txn, 'proxy_hint': proxy})
                    send_message(chat_id, "Enter OTP received for PDF download:", reply_markup=get_cancel_keyboard())
            else:
                send_message(chat_id, "OTP Verification failed. Please restart.")
                clear_session(chat_id)
    elif step == 'awaiting_pdf_otp':
        if re.match(r'^\d{6}$', text):
            send_message(chat_id, "Downloading PDF...")
            ok, pdf_path = bot.download_aadhaar_pdf(chat_id, d['eid'], text, d['pdf_otp_txn_id'], d['transaction_id2'], False, d.get('proxy_hint'))
            if ok:
                deliver_pdf(chat_id, pdf_path, d.get('verified_name', 'Mr.'), d.get('dob'))
            else:
                send_message(chat_id, f"Download failed: {pdf_path}")
                clear_session(chat_id)

def get_updates(offset=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {'timeout': 5, 'allowed_updates': ['message', 'callback_query']}
        if offset: params['offset'] = offset
        r = get_telegram_session().get(url, params=params, timeout=8).json()
        return r.get('result', []) if r.get('ok') else []
    except Exception:
        return []

def main():
    print("Bot starting on Render...")
    last_update_id = 0
    while True:
        try:
            updates = get_updates(last_update_id + 1)
            for u in updates:
                last_update_id = u.get('update_id')
                if 'message' in u:
                    handle_message(u['message']['chat']['id'], u['message'])
                elif 'callback_query' in u:
                    cq = u['callback_query']
                    handle_callback(cq['message']['chat']['id'], cq['id'], cq.get('data', ''), cq['message'].get('message_id'))
            time.sleep(0.3)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Loop error: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
