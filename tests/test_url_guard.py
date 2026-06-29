"""Tests for SSRF guard (app/tools/_url_guard.py)."""
import pytest
from unittest.mock import patch

from app.tools._url_guard import SSRFError, assert_public_url


def test_valid_https_url_passes():
    with patch("app.tools._url_guard.socket.getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 0))]):
        assert_public_url("https://wttr.in/Beijing?format=3")


def test_valid_http_url_passes():
    with patch("app.tools._url_guard.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 0))]):
        assert_public_url("http://example.com/page")


def test_ftp_scheme_rejected():
    with pytest.raises(SSRFError, match="scheme"):
        assert_public_url("ftp://example.com/file")


def test_file_scheme_rejected():
    with pytest.raises(SSRFError, match="scheme"):
        assert_public_url("file:///etc/passwd")


def test_localhost_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://localhost/admin")


def test_localhost_port_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://localhost:8180/api")


def test_127_0_0_1_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://127.0.0.1/")


def test_127_x_x_x_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://127.1.2.3/")


def test_10_x_x_x_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://10.0.0.1/")


def test_192_168_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://192.168.1.1/")


def test_172_16_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://172.16.0.1/")


def test_cloud_metadata_link_local_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://169.254.169.254/latest/meta-data/")


def test_local_suffix_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://myapp.local/")


def test_internal_suffix_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://db.internal/")


def test_userinfo_rejected():
    with pytest.raises(SSRFError, match="userinfo"):
        assert_public_url("http://user:pass@example.com/")


def test_empty_host_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http:///path")


def test_ipv6_loopback_rejected():
    with pytest.raises(SSRFError):
        assert_public_url("http://[::1]/")
