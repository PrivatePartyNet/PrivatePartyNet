"""Files on a server (K, 2026-11-04): any file can be sent and is kept by the host; each server has a per-file limit and a
storage cap, each none or a size, and the cap is at least ten times the limit when both are set; the sender, the owner and
admins may delete; members see how much is kept. Downloads always save, never show as a page."""

import os
import queue
import socket
import ssl
import time
from urllib.parse import urlencode

from privatepartynet.server import BadRequest, Forbidden, Full, NotFound, Party, check_file_settings
from test_server import insecure_context
from test_server_servers import ServerTestCase

M = 1024 * 1024
MEMBER = "privatepartynet_member"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(200)


class FileTestCase(ServerTestCase):
    def text_channel(self, party):
        return next(c["id"] for c in party._channels if c["kind"] == "text")

    def upload(self, party, session, data, name="notes.txt", mime="text/plain", channel=None, content_type="application/octet-stream"):
        query = urlencode({"channel": channel or self.text_channel(party), "name": name, "type": mime})
        response = self.open(party, "POST", "/file?" + query, data,
                             {"Content-Type": content_type, "Cookie": f"{party.cookie_name(MEMBER)}={session}"})
        return response.status, response.read()

    def download(self, party, session, channel, message, headers=None):
        response = self.open(party, "GET", "/file?" + urlencode({"channel": channel, "message": message}),
                             headers={"Cookie": f"{party.cookie_name(MEMBER)}={session}", **(headers or {})})
        return response, response.read()

    def sent(self, status, body):
        self.assertEqual(status, 200, body)
        import json
        return json.loads(body)


class FileSettingsTests(FileTestCase):
    def test_each_setting_is_none_or_a_size_and_the_cap_is_ten_times_the_limit(self):
        self.assertEqual(check_file_settings(None, None), "")
        self.assertEqual(check_file_settings(25 * M, None), "", "a limit with no cap")
        self.assertEqual(check_file_settings(None, 5 * M), "", "a cap with no limit")
        self.assertEqual(check_file_settings(10 * M, 100 * M), "")
        self.assertIn("ten times", check_file_settings(10 * M, 100 * M - 1))
        for bad in (0, M - 1, -5, True, 2.5, "10", 65 * 1024 ** 4):
            with self.subTest(bad=bad):
                self.assertNotEqual(check_file_settings(bad, None), "")
                self.assertNotEqual(check_file_settings(None, bad), "")

    def test_only_the_owner_and_admins_set_them_and_they_are_kept(self):
        party = self.party()
        member, _ = self.member(party, "Sam", user="sam-user")
        admin, _ = self.member(party, "Ada", user="ada-user", role="admin")
        with self.assertRaises(Forbidden):
            party.set_file_settings(member, M, 10 * M)
        with self.assertRaises(BadRequest):
            party.set_file_settings(admin, 10 * M, 20 * M)
        info = party.set_file_settings(admin, 2 * M, None)
        self.assertEqual((info["limit"], info["cap"]), (2 * M, None))
        party.set_file_settings(party.host_id, None, 50 * M)
        party.stop()
        again = self.party()
        self.assertEqual((again.storage_info()["limit"], again.storage_info()["cap"]), (None, 50 * M), "kept with the server")

    def test_the_settings_route_answers_with_the_reason(self):
        party = self.party()
        _, owner_session = None, party.host_token
        status, body = self.post(party, "/storage", {"limit": 10 * M, "cap": 20 * M}, owner_session, cookie="privatepartynet_host")
        self.assertEqual(status, 400)
        self.assertIn(b"ten times", body)
        status, body = self.post(party, "/storage", {"limit": 10 * M, "cap": None}, owner_session, cookie="privatepartynet_host")
        self.assertEqual(status, 200, body)
        _, session = self.member(party, "Sam", user="sam-user")
        status, _ = self.post(party, "/storage", {"limit": None, "cap": None}, session)
        self.assertEqual(status, 403)


class FileTransferTests(FileTestCase):
    def test_a_file_is_posted_kept_and_saved_back_with_safe_headers(self):
        party = self.party()
        member, session = self.member(party, "Sam", user="sam-user")
        data = os.urandom(3 * M + 4321)
        message = self.sent(*self.upload(party, session, data, name="..\\maps/route <final>.zip", mime="Application/ZIP"))
        self.assertEqual(message["file"]["name"], "route final.zip", "no folder part, no reserved characters")
        self.assertEqual((message["file"]["size"], message["file"]["type"], message["text"]), (len(data), "application/zip", ""))
        channel = self.text_channel(party)
        self.assertTrue(any(m["id"] == message["id"] and m.get("file") for m in party.channel_history(member, channel)))
        self.assertEqual(party.storage_info()["used"], len(data))
        response, body = self.download(party, session, channel, message["id"])
        self.assertEqual(response.status, 200)
        self.assertEqual(body, data)
        self.assertEqual(response.getheader("Content-Type"), "application/octet-stream")
        self.assertTrue(response.getheader("Content-Disposition").startswith("attachment;"))
        self.assertIn("route%20final.zip", response.getheader("Content-Disposition"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("sandbox", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("Accept-Ranges"), "bytes")

    def test_part_of_a_file_can_be_asked_for(self):
        party = self.party()
        _, session = self.member(party, "Sam", user="sam-user")
        data = os.urandom(2 * M + 99)
        message = self.sent(*self.upload(party, session, data))
        channel = self.text_channel(party)
        for header, start, end in (("bytes=1000-1999", 1000, 2000), ("bytes=-50", len(data) - 50, len(data)),
                                   (f"bytes={M - 3}-", M - 3, len(data))):
            with self.subTest(header=header):
                response, body = self.download(party, session, channel, message["id"], {"Range": header})
                self.assertEqual(response.status, 206)
                self.assertEqual(body, data[start:end])
                self.assertEqual(response.getheader("Content-Range"), f"bytes {start}-{end - 1}/{len(data)}")
        response, _ = self.download(party, session, channel, message["id"], {"Range": f"bytes={len(data)}-"})
        self.assertEqual(response.status, 416)

    def test_a_file_needs_a_member_a_text_channel_and_raw_bytes(self):
        party = self.party()
        _, session = self.member(party, "Sam", user="sam-user")
        _, guest = self.member(party, "Gus", user="gus-user", role="guest")
        self.assertEqual(self.upload(party, guest, b"hello")[0], 403, "guests read but do not post")
        self.assertEqual(self.upload(party, "not-a-session", b"hello")[0], 401)
        voice = next(c["id"] for c in party._channels if c["kind"] == "voice")
        self.assertEqual(self.upload(party, session, b"hello", channel=voice)[0], 404)
        self.assertEqual(self.upload(party, session, b"hello", content_type="application/json")[0], 403)
        response, _ = self.download(party, session, self.text_channel(party), "nope")
        self.assertEqual(response.status, 404)

    def test_a_party_with_no_store_keeps_no_files(self):
        party = self.party(store=None)
        member, _ = self.member(party, "Sam", user="sam-user")
        with self.assertRaises(Full):
            party.begin_file(member, 10)
        self.assertFalse(party.storage_info()["files"])

    def test_an_upload_cut_off_halfway_leaves_nothing(self):
        party = self.party()
        _, session = self.member(party, "Sam", user="sam-user")
        raw = insecure_context().wrap_socket(socket.create_connection(("127.0.0.1", party.port), timeout=5),
                                             server_hostname="127.0.0.1")
        query = urlencode({"channel": self.text_channel(party), "name": "big.bin"})
        raw.sendall((f"POST /file?{query} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/octet-stream\r\n"
                     f"Content-Length: {4 * M}\r\nCookie: {party.cookie_name(MEMBER)}={session}\r\n\r\n").encode() + os.urandom(300_000))
        raw.close()
        files = self.store.folder / "files"
        deadline = time.time() + 5
        while time.time() < deadline and any(p.is_file() for p in files.rglob("*")):
            time.sleep(0.05)
        self.assertEqual([p for p in files.rglob("*") if p.is_file()], [])
        self.assertEqual(party.storage_info()["used"], 0)


class FileLimitTests(FileTestCase):
    def test_the_per_file_limit_and_the_cap_are_held_to(self):
        party = self.party()
        member, session = self.member(party, "Sam", user="sam-user")
        party.set_file_settings(party.host_id, M, 10 * M)
        with self.assertRaisesRegex(Full, "up to 1 MB"):
            party.begin_file(member, M + 1)
        # Over HTTP a refused body past MAX_DRAIN closes the connection rather than being read to the end, so the sender
        # may see either the answer or the closed connection. The room page checks the limits it knows before sending.
        try:
            status, _ = self.upload(party, session, os.urandom(M + 1))
            self.assertEqual(status, 413)
        except (ssl.SSLError, ConnectionError):
            pass
        size = M - 1000
        posted = [self.sent(*self.upload(party, session, os.urandom(size)))["id"] for _ in range(10)]
        self.assertEqual(party.storage_info()["used"], 10 * size)
        status, body = self.upload(party, session, os.urandom(size))
        self.assertEqual(status, 413)
        self.assertIn(b"storage is full", body)
        party.delete_message(member, self.text_channel(party), posted[0])
        self.assertEqual(party.storage_info()["used"], 9 * size, "deleting frees the space")
        self.sent(*self.upload(party, session, os.urandom(size)))

    def test_pictures_count_toward_the_limits_and_are_freed_when_deleted(self):
        party = self.party()
        member, _ = self.member(party, "Sam", user="sam-user")
        picture = PNG + os.urandom(5000)
        info = party.put_image(member, picture)
        message = party.chat(member, "", self.text_channel(party), {"hash": info["hash"], "w": 10, "h": 10})
        self.assertEqual(party.storage_info()["used"], len(picture))
        party.delete_message(member, self.text_channel(party), message["id"])
        self.assertEqual(party.storage_info()["used"], 0)
        self.assertFalse(self.store.has_blob(info["hash"]))
        party.set_file_settings(party.host_id, M, None)
        with self.assertRaises(Full):
            party.put_image(member, PNG + os.urandom(M))

    def test_a_picture_in_two_messages_stays_until_both_are_deleted(self):
        party = self.party()
        member, _ = self.member(party, "Sam", user="sam-user")
        channel = self.text_channel(party)
        info = party.put_image(member, PNG + b"same")
        first = party.chat(member, "", channel, {"hash": info["hash"], "w": 1, "h": 1})
        second = party.chat(member, "again", channel, {"hash": info["hash"], "w": 1, "h": 1})
        party.delete_message(member, channel, first["id"])
        self.assertTrue(self.store.has_blob(info["hash"]))
        party.delete_message(member, channel, second["id"])
        self.assertFalse(self.store.has_blob(info["hash"]))


class DeleteTests(FileTestCase):
    def test_the_sender_the_owner_and_admins_may_delete_and_nobody_else(self):
        party = self.party()
        sam, sam_session = self.member(party, "Sam", user="sam-user")
        kim, _ = self.member(party, "Kim", user="kim-user")
        ada, _ = self.member(party, "Ada", user="ada-user", role="admin")
        channel = self.text_channel(party)
        posts = [party.chat(sam, f"note {i}", channel) for i in range(3)]
        with self.assertRaises(Forbidden):
            party.delete_message(kim, channel, posts[0]["id"])
        listener = party.attach(kim)
        party.delete_message(sam, channel, posts[0]["id"])
        party.delete_message(ada, channel, posts[1]["id"])
        status, _ = self.post(party, "/delete", {"channel": channel, "id": posts[2]["id"]}, party.host_token, cookie="privatepartynet_host")
        self.assertEqual(status, 202)
        left = [m["id"] for m in party.channel_history(sam, channel)]
        self.assertFalse(set(left) & {p["id"] for p in posts})
        self.assertIsNone(self.store.message(posts[0]["id"], party._scope(channel)), "gone from the store too")
        seen = []
        try:
            while True:
                event = listener.get(timeout=0.5)
                if event["type"] == "deleted":
                    seen.append(event["id"])
        except queue.Empty:
            pass
        self.assertEqual(seen, [p["id"] for p in posts], "everyone is told")
        with self.assertRaises(NotFound):
            party.delete_message(sam, channel, posts[0]["id"])

    def test_a_deleted_file_can_no_longer_be_saved(self):
        party = self.party()
        sam, session = self.member(party, "Sam", user="sam-user")
        message = self.sent(*self.upload(party, session, os.urandom(5000)))
        file_id = message["file"]["id"]
        party.delete_message(sam, self.text_channel(party), message["id"])
        self.assertIsNone(self.store.file_size(file_id))
        response, _ = self.download(party, session, self.text_channel(party), message["id"])
        self.assertEqual(response.status, 404)

    def test_members_are_told_how_much_is_kept(self):
        party = self.party()
        sam, session = self.member(party, "Sam", user="sam-user")
        hello = party.attach(sam).get(timeout=1)
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["storage"]["used"], 0)
        self.assertTrue(hello["storage"]["files"])
        self.assertIsInstance(hello["storage"]["free"], int)
        status, body = self.get(party, "/storage", session)
        self.assertEqual(status, 200)
        self.assertNotIn(b"largest", body, "only the owner and admins see the biggest files")


if __name__ == "__main__":
    import unittest
    unittest.main()
