import io
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch

from fb_automator.image_downloader import (
    _PinnedHTTPSConnection,
    _public_addresses,
    download_image,
    is_allowed_image_url,
)


PUBLIC_IP = "157.240.1.35"
PUBLIC_ADDRESS = (socket.AF_INET, (PUBLIC_IP, 443))


def dns_answer(ip=PUBLIC_IP):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    destination = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", destination)


class ImageResponse(io.BytesIO):
    def __init__(self, body=b"image", *, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": "image/jpeg"} if headers is None else headers
        self.read_sizes = []
        self.bytes_read = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, amount=-1):
        self.read_sizes.append(amount)
        result = super().read(amount)
        self.bytes_read += len(result)
        return result


class ImageUrlTests(unittest.TestCase):
    def test_accepts_https_hosts_on_domain_boundaries(self):
        for value in (
            "https://fbcdn.net/image.jpg",
            "https://scontent.example.fbcdn.net/image.jpg?sig=a%2Fb&expires=1",
            "https://facebook.com/image.jpg",
            "https://www.facebook.com:443/image.jpg",
            "https://SContent.Example.FBCDN.NET/image.jpg",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_allowed_image_url(value))

    def test_rejects_untrusted_origins_and_ambiguous_urls(self):
        for value in (
            "https://127.0.0.1/image?facebook.com",
            "https://169.254.169.254/fbcdn.net",
            "https://[::1]/image?facebook.com",
            "https://facebook.com.attacker.invalid/image.jpg",
            "https://attacker.invalid/fbcdn.net/image.jpg",
            "https://evilfacebook.com/image.jpg",
            "https://fbcdn.net.attacker.invalid/image.jpg",
            "https://attacker.invalid/#https://fbcdn.net/image.jpg",
            "https://user:password@fbcdn.net/image.jpg",
            "https://fbcdn.net@attacker.invalid/image.jpg",
            "https://attacker.invalid@fbcdn.net/image.jpg",
            "https://fbcdn.net:8443/image.jpg",
            "https://fbcdn.net:invalid/image.jpg",
            "https://scontent.example.fbcdn.net/image.jpg#fragment",
            "http://fbcdn.net/image.jpg",
            "file:///fbcdn.net/image.jpg",
            "//fbcdn.net/image.jpg",
            "https://fbcdn.net./image.jpg",
            "https://.fbcdn.net/image.jpg",
            "https://a..fbcdn.net/image.jpg",
            "https://fbcdn.net\\@attacker.invalid/image.jpg",
            "https://fbcdn.net\n.attacker.invalid/image.jpg",
            "https://fbcdn.net/image.jpg\r\nCookie: value",
            " https://fbcdn.net/image.jpg",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_allowed_image_url(value))


class ImageDestinationTests(unittest.TestCase):
    def test_accepts_public_ipv4_and_ipv6_and_deduplicates_answers(self):
        answers = [dns_answer(), dns_answer(), dns_answer("2a03:2880:f10c:83:face:b00c:0:25de")]
        with patch("fb_automator.image_downloader.socket.getaddrinfo", return_value=answers):
            addresses = _public_addresses("scontent.example.fbcdn.net")
        self.assertEqual(len(addresses), 2)
        self.assertEqual(addresses[0], PUBLIC_ADDRESS)

    def test_rejects_private_reserved_and_mixed_dns_answers(self):
        for ip in (
            "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
            "0.0.0.0", "100.64.0.1", "192.0.2.1", "224.0.0.1", "240.0.0.1",
            "::1", "::", "fd00::1", "fe80::1", "ff02::1", "::ffff:127.0.0.1",
            "2002:7f00:1::", "2001::7f00:1",
        ):
            with self.subTest(ip=ip), patch(
                "fb_automator.image_downloader.socket.getaddrinfo",
                return_value=[dns_answer(), dns_answer(ip)],
            ):
                with self.assertRaises(ValueError):
                    _public_addresses("scontent.example.fbcdn.net")

    def test_rejects_ipv6_scoped_address(self):
        answer = list(dns_answer("2a03:2880:f10c:83:face:b00c:0:25de"))
        answer[-1] = (answer[-1][0], 443, 0, 1)
        with patch("fb_automator.image_downloader.socket.getaddrinfo", return_value=[answer]):
            with self.assertRaises(ValueError):
                _public_addresses("scontent.example.fbcdn.net")

    def test_connects_to_pinned_ip_with_original_tls_hostname_without_dns_retry(self):
        with (
            patch("fb_automator.image_downloader.ssl.create_default_context") as tls_factory,
            patch("fb_automator.image_downloader.socket.socket") as socket_factory,
            patch("fb_automator.image_downloader.socket.getaddrinfo") as lookup,
        ):
            connection = _PinnedHTTPSConnection("scontent.example.fbcdn.net", PUBLIC_ADDRESS)
            connection.connect()
        lookup.assert_not_called()
        socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        socket_factory.return_value.connect.assert_called_once_with((PUBLIC_IP, 443))
        tls_factory.return_value.wrap_socket.assert_called_once_with(
            socket_factory.return_value, server_hostname="scontent.example.fbcdn.net"
        )

    def test_closes_socket_if_tls_validation_fails(self):
        with (
            patch("fb_automator.image_downloader.ssl.create_default_context") as tls_factory,
            patch("fb_automator.image_downloader.socket.socket") as socket_factory,
        ):
            tls_factory.return_value.wrap_socket.side_effect = ssl.SSLCertVerificationError("synthetic")
            connection = _PinnedHTTPSConnection("scontent.example.fbcdn.net", PUBLIC_ADDRESS)
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.connect()
        socket_factory.return_value.close.assert_called_once()


class ImageDownloadTests(unittest.TestCase):
    def setUp(self):
        self.dns = patch("fb_automator.image_downloader.socket.getaddrinfo", return_value=[dns_answer()])
        self.lookup = self.dns.start()
        self.addCleanup(self.dns.stop)
        self.transport = patch("fb_automator.image_downloader._PinnedHTTPSConnection")
        self.factory = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.response = ImageResponse()
        self.factory.return_value.getresponse.return_value = self.response

    def test_preserves_signed_query_and_sends_no_credentials(self):
        url = "https://scontent.example.fbcdn.net/room.jpg?sig=a%2Fb%2BC&expires=123"
        referrer = "https://www.facebook.com/groups/synthetic/posts/42"
        self.assertEqual(download_image(url, referer=referrer), (b"image", "image/jpeg"))
        self.factory.assert_called_once_with("scontent.example.fbcdn.net", PUBLIC_ADDRESS)
        args, kwargs = self.factory.return_value.request.call_args
        self.assertEqual(args, ("GET", "/room.jpg?sig=a%2Fb%2BC&expires=123"))
        self.assertEqual(kwargs["headers"]["Referer"], referrer)
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertNotIn("Cookie", kwargs["headers"])
        self.assertNotIn("Authorization", kwargs["headers"])
        self.factory.return_value.close.assert_called_once()

    def test_invalid_url_never_resolves_or_connects(self):
        self.assertIsNone(download_image("https://127.0.0.1/image?facebook.com"))
        self.lookup.assert_not_called()
        self.factory.assert_not_called()

    def test_private_dns_never_connects(self):
        self.lookup.return_value = [dns_answer("127.0.0.1")]
        self.assertIsNone(download_image("https://scontent.example.fbcdn.net/image.jpg"))
        self.factory.assert_not_called()

    def test_dns_failure_is_ignored(self):
        self.lookup.side_effect = socket.gaierror("synthetic failure")
        self.assertIsNone(download_image("https://scontent.example.fbcdn.net/image.jpg"))
        self.factory.assert_not_called()

    def test_redirects_are_not_followed_even_to_allowlisted_hosts(self):
        for location in ("https://127.0.0.1/private", "https://other.fbcdn.net/image.jpg"):
            with self.subTest(location=location):
                self.response = ImageResponse(status=302, headers={"Location": location})
                self.factory.return_value.getresponse.return_value = self.response
                self.assertIsNone(download_image("https://scontent.example.fbcdn.net/image.jpg"))
                self.assertEqual(self.response.read_sizes, [])
        self.assertEqual(self.lookup.call_count, 2)
        self.assertEqual(self.factory.return_value.request.call_count, 2)

    def test_rejects_non_image_or_compressed_response_before_reading(self):
        for headers in (
            {"Content-Type": "text/html"},
            {"Content-Type": "image/svg+xml"},
            {"Content-Type": "image/png", "Content-Encoding": "gzip"},
        ):
            with self.subTest(headers=headers):
                response = ImageResponse(headers=headers)
                self.factory.return_value.getresponse.return_value = response
                self.assertIsNone(download_image("https://scontent.example.fbcdn.net/image.jpg"))
                self.assertEqual(response.read_sizes, [])

    def test_rejects_oversized_or_invalid_content_length_without_reading(self):
        for length in ("10485761", "-1", "0", "invalid", "4, 5"):
            with self.subTest(length=length):
                response = ImageResponse(headers={"Content-Type": "image/png", "Content-Length": length})
                self.factory.return_value.getresponse.return_value = response
                self.assertIsNone(download_image("https://scontent.example.fbcdn.net/image.jpg"))
                self.assertEqual(response.read_sizes, [])

    def test_stream_limit_reads_only_one_overflow_byte(self):
        response = ImageResponse(b"x" * 100)
        self.factory.return_value.getresponse.return_value = response
        with patch("fb_automator.image_downloader.MAX_IMAGE_BYTES", 8):
            self.assertIsNone(download_image("https://scontent.example.fbcdn.net/image.jpg"))
        self.assertEqual(response.read_sizes, [9])
        self.assertEqual(response.bytes_read, 9)

    def test_exact_limit_is_accepted(self):
        response = ImageResponse(b"x" * 8)
        self.factory.return_value.getresponse.return_value = response
        with patch("fb_automator.image_downloader.MAX_IMAGE_BYTES", 8):
            self.assertEqual(download_image("https://scontent.example.fbcdn.net/image.jpg"), (b"x" * 8, "image/jpeg"))

    def test_empty_and_truncated_bodies_are_rejected(self):
        for body, length in ((b"", None), (b"x", "8")):
            headers = {"Content-Type": "image/jpeg"}
            if length is not None:
                headers["Content-Length"] = length
            self.factory.return_value.getresponse.return_value = ImageResponse(body, headers=headers)
            self.assertIsNone(download_image("https://scontent.example.fbcdn.net/image.jpg"))

    def test_unsafe_referrer_is_not_sent(self):
        download_image("https://scontent.example.fbcdn.net/image.jpg", referer="https://fbcdn.net/\r\nCookie: secret")
        self.assertNotIn("Referer", self.factory.return_value.request.call_args.kwargs["headers"])

    def test_can_fall_back_to_another_prevalidated_public_address(self):
        self.lookup.return_value = [dns_answer(), dns_answer("157.240.1.36")]
        first = MagicMock()
        first.request.side_effect = OSError("synthetic IPv4 failure")
        second = MagicMock()
        second.getresponse.return_value = self.response
        self.factory.side_effect = [first, second]
        self.assertEqual(download_image("https://scontent.example.fbcdn.net/image.jpg"), (b"image", "image/jpeg"))
        self.lookup.assert_called_once()
        first.close.assert_called_once()
        second.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
