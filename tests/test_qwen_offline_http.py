import sys
from pathlib import Path

import httpx
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qwen_offline_http import HttpDownloadBlocker


def test_download_blocker_rejects_both_http_libraries_and_https_tunnels():
    blocker = HttpDownloadBlocker()
    try:
        proxies = {"http": blocker.url, "https": blocker.url}
        assert requests.get("http://hub-offline.invalid", proxies=proxies, timeout=3).status_code == 403
        with pytest.raises(requests.exceptions.ProxyError):
            requests.get("https://hub-offline.invalid", proxies=proxies, timeout=3)
        with httpx.Client(proxy=blocker.url, timeout=3, trust_env=False) as client:
            assert client.get("http://hub-offline.invalid").status_code == 403
            with pytest.raises(httpx.ProxyError):
                client.get("https://hub-offline.invalid")
        assert blocker.denied_requests == 4
        env = blocker.environment(
            {"http_proxy": "inherited", "HTTPS_PROXY": "inherited", "no_proxy": "*", "KEEP": "ok"}
        )
        assert env == dict(
            HTTP_PROXY=blocker.url,
            HTTPS_PROXY=blocker.url,
            ALL_PROXY=blocker.url,
            NO_PROXY="127.0.0.1,localhost,::1",
            KEEP="ok",
        )
    finally:
        blocker.close()
    assert not blocker.thread.is_alive()
