"""عميل HTTP مهذب: تأخير عشوائي، طلب واحد في كل مرة، احترام robots.txt، وإعادة محاولة."""
import logging
import random
import re
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("sayd.net")


class FetchError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class Blocked(FetchError):
    pass


class Disallowed(FetchError):
    pass


class Robots:
    """robots.txt مع دعم * و $ (قاعدة أطول تطابق، والتعادل للسماح)."""

    def __init__(self, text):
        self.rules = []
        groups, agents, rules, last_was_agent = [], [], [], False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and agents:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(val.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if val:
                    rules.append((key == "allow", val))
        if agents:
            groups.append((agents, rules))
        for ags, rs in groups:
            if "*" in ags:
                self.rules.extend(rs)

    @staticmethod
    def _match(pattern, path):
        anchored = pattern.endswith("$")
        rx = re.escape(pattern.rstrip("$")).replace(r"\*", ".*")
        return re.match(rx + ("$" if anchored else ""), path) is not None

    def allowed(self, path):
        best = None
        for allow, pat in self.rules:
            if self._match(pat, path):
                cand = (len(pat), allow)
                if best is None or cand > best:
                    best = cand
        return True if best is None else best[1]


class Http:
    def __init__(self, user_agent, delay_min, delay_max, deadline=None):
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        self.delay = (delay_min, delay_max)
        self.deadline = deadline
        self._last = 0.0
        self._robots = {}
        self.requests = 0

    def time_left(self):
        return float("inf") if self.deadline is None else self.deadline - time.time()

    def _robots_for(self, base):
        if base not in self._robots:
            try:
                r = self._raw("GET", base + "/robots.txt")
                self._robots[base] = Robots(r.text if r.status_code == 200 else "")
            except FetchError:
                # تعذّر جلب robots: نتعامل بحذر ونسمح فقط بما سبق التحقق منه لاحقاً
                self._robots[base] = Robots("")
        return self._robots[base]

    def _wait(self):
        gap = random.uniform(*self.delay) - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)

    def _raw(self, method, url, **kw):
        self._wait()
        try:
            r = self.client.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__}") from None
        finally:
            self._last = time.time()
            self.requests += 1
        return r

    def get(self, url, *, params=None, headers=None, check_robots=True, retries=3):
        parts = urlsplit(url)
        if check_robots:
            base = f"{parts.scheme}://{parts.netloc}"
            path = parts.path + ("?" + parts.query if parts.query else "")
            if params:
                path += ("&" if "?" in path else "?") + "x"
            if not self._robots_for(base).allowed(path or "/"):
                raise Disallowed(f"robots.txt يمنع {parts.path}")
        err = None
        for attempt in range(retries + 1):
            try:
                r = self._raw("GET", url, params=params, headers=headers)
            except FetchError as e:
                err = e
            else:
                if r.status_code == 200:
                    return r
                body = r.text[:2000].lower()
                if r.status_code in (403, 503) and ("cloudflare" in body or "challenge" in body or "cf-ray" in str(r.headers).lower()):
                    raise Blocked(f"حظر/تحدي Cloudflare ({r.status_code})", r.status_code)
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise FetchError(f"HTTP {r.status_code}", r.status_code)
                err = FetchError(f"HTTP {r.status_code}", r.status_code)
            if attempt < retries:
                time.sleep(min(60, 5 * 2 ** attempt) + random.uniform(0, 2))
        raise err

    def close(self):
        self.client.close()
