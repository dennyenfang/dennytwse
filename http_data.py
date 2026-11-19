"""Small HTTPS fetch helper that keeps certificate checks enabled."""

import shutil
import subprocess
import urllib.error
import urllib.request


def get_bytes(url, *, timeout=25, user_agent="DennyTWSE/1.0"):
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.URLError:
        # Some Windows OpenSSL builds reject a certificate chain that Schannel accepts.
        # curl still verifies certificates by default; never use -k/--insecure here.
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise
        try:
            result = subprocess.run(
                [curl, "--fail", "--silent", "--show-error", "--location", "--max-time", str(timeout),
                 "--user-agent", user_agent, url],
                check=True, capture_output=True, timeout=timeout + 3,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise urllib.error.URLError(f"curl TLS-verified request failed: {exc}") from exc
        return result.stdout
