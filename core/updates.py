"""Update check against a GitHub repository's latest release.

Set the repository in core/version.py (UPDATE_REPO = "owner/name") or in
Documents\\Hatchling\\settings.json as {"update_repo": "owner/name"}. Nothing is sent
except a normal request for the public release page.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .paths import user_dir
from .version import UPDATE_REPO, VERSION


GITHUB_HOSTS = ("https://github.com/", "https://objects.githubusercontent.com/",
                "https://release-assets.githubusercontent.com/")
MAX_INSTALLER = 400 * 1024 * 1024


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    url: str           # installer download (or the release page if there's no installer)
    notes: str
    sha256: str = ""   # from the release asset's digest, or the .sha256 file published next to it
    size: int = 0
    page: str = ""

    @property
    def installer(self) -> bool:
        return self.url.lower().endswith((".exe", ".tar.gz"))


# ---- Linux / Raspberry Pi: installed by install-linux.sh as
#   ~/.local/share/hatchling/venv, .../versions/<version>/, .../current -> versions/<version>
def linux_home() -> str:
    """The Linux install folder when Hatchling runs from it, otherwise ''."""
    from .paths import app_dir
    here = os.path.dirname(os.path.realpath(app_dir()))
    if os.path.basename(here) == "versions":
        return os.path.dirname(here)
    return ""


def _asset_for_platform(assets: list):
    if sys.platform == "win32":
        return next((a for a in assets if a.get("name", "").lower().endswith(".exe")), None)
    if sys.platform.startswith("linux") and linux_home():
        return next((a for a in assets if re.fullmatch(r"hatchling-linux-[\d.]+\.tar\.gz",
                                                        a.get("name", "").lower())), None)
    return None


def parse_version(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(remote: str, local: str = VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def configured_repo() -> str:
    try:
        with open(user_dir("settings.json"), encoding="utf-8") as f:
            return json.load(f).get("update_repo", "") or UPDATE_REPO
    except (OSError, ValueError):
        return UPDATE_REPO


def check(timeout: float = 6.0) -> Optional[Release]:
    """Returns the newer release, or None if up to date / not configured. Raises on network errors."""
    repo = configured_repo()
    if not repo or "/" not in repo:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "Hatchling"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    if not is_newer(tag):
        return None
    assets = [a for a in data.get("assets", []) if isinstance(a, dict)]
    exe = _asset_for_platform(assets)
    page = data.get("html_url", "")
    if not exe:
        return Release(tag.lstrip("v"), page, (data.get("body") or "").strip()[:8000], page=page)
    sha = ""
    digest = str(exe.get("digest") or "")
    if digest.startswith("sha256:"):
        sha = digest[7:].lower()
    if not sha:
        side = next((a for a in assets if a.get("name", "").lower() == exe["name"].lower() + ".sha256"), None)
        if side:
            try:
                req = urllib.request.Request(side["browser_download_url"], headers={"User-Agent": "Hatchling"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    sha = r.read(500).decode("ascii", "ignore").split()[0].lower()
            except (OSError, IndexError):
                sha = ""
    return Release(tag.lstrip("v"), exe["browser_download_url"], (data.get("body") or "").strip()[:8000],
                   sha if re.fullmatch(r"[0-9a-f]{64}", sha) else "", int(exe.get("size") or 0), page)


# ---- release notes for the update window --------------------------------------------
_BOILERPLATE = ("download `hatchling-setup", "use at your own risk", "feedback welcome", "the source code is attached",
                "beta testing release")


_HEADING = re.compile(r"^(?:what's new in|what was new in|new in)\s+v?(\d+(?:\.\d+)*)", re.I)


def whats_new(notes: str, version: str, since: str = VERSION) -> str:
    """The release notes for every version after `since` up to `version`, newest first - without the
    download instructions. Falls back to the notes minus boilerplate if they have no version headings."""
    lines = (notes or "").replace("\r\n", "\n").split("\n")
    sections, cur = [], None
    for l in lines:
        m = _HEADING.match(l.strip())
        if m:
            cur = [m.group(1), []]
            sections.append(cur)
        elif cur is not None:
            if l.strip().lower().startswith("how to install") or any(b in l.lower() for b in _BOILERPLATE):
                cur = None
                continue
            cur[1].append(l)
    wanted = [(v, "\n".join(body).strip()) for v, body in sections
              if parse_version(since) < parse_version(v) <= parse_version(version)]
    wanted = [(v, b) for v, b in wanted if b]
    if wanted:
        if len(wanted) == 1:
            return wanted[0][1]
        return "\n\n".join(f"Version {v}\n{b}" for v, b in wanted)
    keep = [l for l in lines if not any(b in l.lower() for b in _BOILERPLATE)]
    return "\n".join(keep).strip()


def notes_html(text: str) -> str:
    """The little Markdown the release notes use (bold, bullets, paragraphs) as HTML."""
    import html as _html
    out, in_list = [], False
    for raw in text.split("\n"):
        l = raw.strip()
        body = _html.escape(l[2:] if l.startswith(("- ", "* ")) else l)
        body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
        body = re.sub(r"`(.+?)`", r"\1", body)
        if l.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul style='margin:2px 0 6px -20px'>")
                in_list = True
            out.append(f"<li>{body}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if l:
            out.append(f"<p style='margin:6px 0 2px'><b>{body}</b></p>" if not l.endswith(".") and len(l) < 40
                       else f"<p style='margin:4px 0'>{body}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


# ---- downloading and installing in the app ------------------------------------------
def download_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "Hatchling-update")


def downloaded_path(rel: Release) -> str:
    ver = re.sub(r'[^0-9A-Za-z.]', '', rel.version)
    if rel.url.lower().endswith(".tar.gz"):
        return os.path.join(download_dir(), f"Hatchling-linux-{ver}.tar.gz")
    return os.path.join(download_dir(), f"Hatchling-Setup-{ver}.exe")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def can_install() -> bool:
    """Hatchling can update itself here: the Windows installer, or a Linux copy set up by install-linux.sh."""
    return sys.platform == "win32" or (sys.platform.startswith("linux") and bool(linux_home()))


def download(rel: Release, progress: Optional[Callable[[int, int], None]] = None, timeout: float = 30.0) -> str:
    """Downloads and checks the update. Windows: returns the installer. Linux: unpacks it beside the running
    version and returns that folder."""
    path = _download_file(rel, progress, timeout)
    if path.endswith(".tar.gz"):
        return prepare_linux(path, rel.version)
    return path


def _download_file(rel: Release, progress: Optional[Callable[[int, int], None]] = None, timeout: float = 30.0) -> str:
    """Downloads the installer next to the other update files and checks it. Only GitHub downloads are
    accepted, and the file must match the checksum GitHub publishes for it. Returns the file path."""
    if not rel.installer:
        raise UpdateError("this release has no installer to download")
    if not rel.url.startswith(GITHUB_HOSTS):
        raise UpdateError("the installer isn't on GitHub, so it wasn't downloaded")
    if not rel.sha256:
        raise UpdateError("the release has no checksum, so it can't be checked - download it from the website")
    path = downloaded_path(rel)
    if os.path.isfile(path) and _sha256(path) == rel.sha256:
        return path   # already downloaded earlier
    os.makedirs(download_dir(), exist_ok=True)
    part = path + ".part"
    if os.path.exists(part):
        os.remove(part)
    last = ""
    # A dropped connection carries on from where it stopped (up to 5 times); a file that still doesn't
    # match its checksum is downloaded once more from scratch before giving up.
    for fresh in range(2):
        for attempt in range(6):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            try:
                done = _fetch(rel, part, have, progress, timeout)
            except UpdateError as e:
                last = str(e)
                continue
            if done:
                break
            last = "the connection dropped part way"
        else:
            raise UpdateError(f"download failed - {last}. Check the internet connection and try again")
        if _sha256(part) == rel.sha256:
            os.replace(part, path)
            return path
        os.remove(part)
        last = "the download didn't match its checksum"
    raise UpdateError("the download didn't match its checksum twice, so it was deleted - try again later")


def _fetch(rel: Release, part: str, have: int, progress, timeout: float) -> bool:
    """Appends to `part` from byte `have`. True when the whole file is there."""
    headers = {"User-Agent": "Hatchling", "Accept": "application/octet-stream"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(rel.url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if have and r.status != 206:      # server ignored the range: start again
                have = 0
            length = int(r.headers.get("Content-Length") or 0)
            total = have + length if length else rel.size
            if total > MAX_INSTALLER:
                raise UpdateError("the download is unexpectedly large")
            done = have
            with open(part, "ab" if have else "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_INSTALLER:
                        raise UpdateError("the download is unexpectedly large")
                    f.write(chunk)
                    if progress:
                        progress(done, total)
    except UpdateError:
        raise
    except Exception as e:   # network errors, timeouts, incomplete reads
        raise UpdateError(f"download interrupted ({getattr(e, 'reason', e)})")
    expected = rel.size or total
    return not expected or os.path.getsize(part) >= expected


def prepare_linux(tarball: str, version: str) -> str:
    """Unpacks a downloaded Linux release next to the running one and installs any new Python packages.
    Nothing changes for the running app until install() switches to it. Returns the new folder."""
    import shutil
    import subprocess
    import tarfile
    home = linux_home()
    if not home:
        raise UpdateError("this copy of Hatchling wasn't installed with install-linux.sh")
    ver = re.sub(r"[^0-9A-Za-z.]", "", version)
    dest = os.path.join(home, "versions", ver)
    tmp = dest + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(tarball, "r:gz") as tf:
        for m in tf.getmembers():
            name = m.name.split("/", 1)[1] if "/" in m.name else ""
            if not name or name.startswith("/") or ".." in name.split("/") or not (m.isfile() or m.isdir()):
                continue      # only plain files inside the top folder
            m.name = name
            tf.extract(m, tmp)
    if not os.path.isfile(os.path.join(tmp, "main.py")):
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError("the download doesn't contain Hatchling")
    from .paths import app_dir
    new_req, old_req = os.path.join(tmp, "requirements.txt"), os.path.join(app_dir(), "requirements.txt")
    try:
        same = open(new_req).read() == open(old_req).read()
    except OSError:
        same = False
    if not same:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", new_req], capture_output=True,
                           text=True, timeout=1800)
        if r.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise UpdateError("the new version's packages couldn't be installed: " + (r.stderr or "")[-300:])
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return dest


def install(path: str, relaunch: bool = True, show_progress: bool = True):
    """Starts the installer silently (Windows asks once for permission) and returns straight away -
    the caller should then close Hatchling so its files can be replaced. The installer reopens
    Hatchling when it's finished if relaunch is True."""
    # /SILENT shows only the installer's progress window (no questions); /VERYSILENT shows nothing
    if sys.platform.startswith("linux"):
        _install_linux(path, relaunch)
        return
    args = ("/SILENT" if show_progress else "/VERYSILENT") + " /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /SP-"
    if relaunch:
        args += " /RELAUNCH=1"
    log = os.path.join(download_dir(), "install-log.txt")
    args += f' /LOG="{log}"'
    if sys.platform != "win32":
        raise UpdateError("updates install on Windows and Linux only")
    try:
        os.startfile(path, "open", args)   # ShellExecute: shows the Windows permission prompt
    except OSError as e:
        raise UpdateError(f"the installer couldn't start ({e})")


def _install_linux(version_dir: str, relaunch: bool):
    """Points 'current' at the new version (one rename, so it's never half-switched) and starts it."""
    import subprocess
    home = linux_home()
    if not home or not os.path.isfile(os.path.join(version_dir, "main.py")):
        raise UpdateError("the new version isn't ready")
    link = os.path.join(home, "current")
    tmp = link + ".new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(version_dir, tmp)
    os.replace(tmp, link)
    if relaunch:
        subprocess.Popen([sys.executable, os.path.join(link, "main.py")], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def clean_old(keep: str = ""):
    """Removes earlier downloads once Hatchling is running the newer version."""
    d = download_dir()
    if not os.path.isdir(d):
        return
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        if fn.lower().endswith((".exe", ".part", ".tar.gz")) and p != keep:
            try:
                os.remove(p)
            except OSError:
                pass
