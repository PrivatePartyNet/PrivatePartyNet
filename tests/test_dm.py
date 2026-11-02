import base64
import http.client
import json
import ssl
import tempfile
import time
import unittest
from pathlib import Path

from privatepartynet.cert import ensure_certificate
from privatepartynet.dm import MAX_ENVELOPE, MAX_IMAGE, PER_MINUTE, DmService, scope_for
from privatepartynet.identity import Identity
from privatepartynet.store import Store
from privatepartynet.vault import Vault

FAST = {"n": 2 ** 10, "r": 8, "p": 1}
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


class Person:
    def __init__(self, test, name, auto=False):
        folder = Path(test.folder) / name
        self.vault = Vault(folder / "keys", scrypt_params=FAST)
        self.identity = Identity.create()
        self.store = Store(self.vault, folder / "store")
        test.addCleanup(self.store.close)
        certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
        self.name = name
        self.live = None  # what the resolver says: (address, port) while this person is "visible"
        self.service = DmService(self.identity, self.store, lambda: name, certfile, keyfile, port=0, bind="127.0.0.1", auto=auto)
        self.service.start()
        test.addCleanup(self.service.stop)

    def card(self):
        return self.service.card()

    def know(self, other):
        self.store.save_contact({"id": other.identity.user_id, "name": other.name, "sign": other.identity.sign_public.hex(),
                                 "dh": other.identity.dh_public.hex()})

    def see(self, other):
        self.service._resolver = lambda user_id: ("127.0.0.1", other.service.port) if user_id == other.identity.user_id else None


class DmTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = temp.name
        self.alice = Person(self, "Alice")
        self.bob = Person(self, "Bob")

    def post(self, person, envelope):
        connection = http.client.HTTPSConnection("127.0.0.1", person.service.port, timeout=5,
                                                 context=ssl._create_unverified_context())
        body = json.dumps(envelope).encode("utf-8") if not isinstance(envelope, bytes) else envelope
        connection.request("POST", "/dm", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def envelope(self, sender, to, payload, **overrides):
        plain = json.dumps(payload).encode("utf-8")
        sealed = sender.identity.seal_to(to.identity.dh_public, plain, f"dm|{to.identity.user_id}|{sender.identity.user_id}".encode())
        envelope = {"v": 1, "from": sender.card(), "to": to.identity.user_id, "sealed": sealed.hex()}
        envelope.update(overrides)
        return envelope


class DeliveryTests(DmTestCase):
    def test_a_message_arrives_saved_and_sealed_and_is_delivered_once(self):
        self.alice.know(self.bob)
        self.alice.see(self.bob)
        message_id = self.alice.service.send(self.bob.identity.user_id, "hello bob")
        self.assertEqual(self.alice.service.flush(), 1)
        (got,) = self.bob.store.history(scope_for(self.alice.identity.user_id))
        self.assertEqual((got["id"], got["body"]["text"], got["sender"]), (message_id, "hello bob", self.alice.identity.user_id))
        (mine,) = self.alice.store.history(scope_for(self.bob.identity.user_id))
        self.assertEqual(mine["body"]["text"], "hello bob", "the sender keeps their own copy")
        self.assertEqual(self.alice.store.outbox_count(), 0)
        self.assertEqual(self.alice.service.flush(), 0)
        self.assertEqual(self.bob.service.events.get_nowait()[0], "dm")

    def test_the_stranger_becomes_a_new_contact_with_pinned_keys_and_a_way_back(self):
        self.alice.know(self.bob)
        self.alice.see(self.bob)
        self.alice.service.send(self.bob.identity.user_id, "hi")
        self.alice.service.flush()
        contact = self.bob.store.contact(self.alice.identity.user_id)
        self.assertEqual((contact["name"], contact["sign"], contact["new"]), ("Alice", self.alice.identity.sign_public.hex(), True))
        self.assertEqual(contact["port"], self.alice.service.port)
        self.bob.see(self.alice)
        self.bob.service.send(self.alice.identity.user_id, "hi back")
        self.assertEqual(self.bob.service.flush(), 1)
        self.assertEqual(self.alice.store.history(scope_for(self.bob.identity.user_id))[-1]["body"]["text"], "hi back")

    def test_nothing_readable_crosses_the_wire(self):
        envelope = self.envelope(self.alice, self.bob, {"id": "a" * 16, "ts": time.time(), "text": "swordfish"})
        self.assertNotIn(b"swordfish", json.dumps(envelope).encode())

    def test_a_message_to_someone_offline_waits_and_goes_when_they_are_seen(self):
        self.alice.know(self.bob)
        self.alice.service.send(self.bob.identity.user_id, "are you there")
        self.assertEqual(self.alice.service.flush(), 0, "no way to reach Bob yet")
        self.assertEqual(self.alice.store.outbox_count(), 1)
        self.assertEqual(self.alice.service.pending(self.bob.identity.user_id), {self.alice.store.due_outgoing(now=float("inf"))[0]["id"]})
        self.alice.see(self.bob)
        self.alice.store._db.execute("update outbox set next_try=0")
        self.alice.store._db.commit()
        self.assertEqual(self.alice.service.flush(), 1)
        self.assertEqual(self.bob.store.count(scope_for(self.alice.identity.user_id)), 1)
        self.assertEqual(self.alice.service.pending(self.bob.identity.user_id), set())

    def test_the_outbox_thread_delivers_without_being_asked(self):
        self.alice.service.stop()
        self.alice = Person(self, "Alice2", auto=True)
        self.alice.know(self.bob)
        self.alice.see(self.bob)
        self.alice.service.send(self.bob.identity.user_id, "background")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not self.bob.store.count(scope_for(self.alice.identity.user_id)):
            time.sleep(0.2)
        self.assertEqual(self.bob.store.count(scope_for(self.alice.identity.user_id)), 1)

    def test_a_saved_address_is_tried_when_the_resolver_knows_nothing(self):
        self.alice.store.save_contact({"id": self.bob.identity.user_id, "name": "Bob", "sign": self.bob.identity.sign_public.hex(),
                                       "dh": self.bob.identity.dh_public.hex(), "addr": "127.0.0.1", "port": self.bob.service.port})
        self.alice.service.send(self.bob.identity.user_id, "by saved address")
        self.assertEqual(self.alice.service.flush(), 1)

    def test_a_picture_travels_and_is_stored_by_both(self):
        self.alice.know(self.bob)
        self.alice.see(self.bob)
        self.alice.service.send(self.bob.identity.user_id, "look", image=PNG)
        self.alice.service.flush()
        (got,) = self.bob.store.history(scope_for(self.alice.identity.user_id))
        self.assertEqual(self.bob.store.get_blob(got["body"]["image"]["hash"]), PNG)
        (mine,) = self.alice.store.history(scope_for(self.bob.identity.user_id))
        self.assertEqual(self.alice.store.get_blob(mine["body"]["image"]["hash"]), PNG)

    def test_sending_refuses_bad_input_before_anything_is_queued(self):
        self.alice.know(self.bob)
        with self.assertRaises(KeyError):
            self.alice.service.send("f" * 32, "who")
        with self.assertRaises(ValueError):
            self.alice.service.send(self.bob.identity.user_id, "   ")
        with self.assertRaises(ValueError):
            self.alice.service.send(self.bob.identity.user_id, "x", image=b"<svg/>")
        with self.assertRaises(ValueError):
            self.alice.service.send(self.bob.identity.user_id, "x", image=PNG + bytes(MAX_IMAGE))
        self.assertEqual(self.alice.store.outbox_count(), 0)


class SecurityTests(DmTestCase):
    def payload(self, **extra):
        return {"id": "b" * 16, "ts": time.time(), "text": "hi", **extra}

    def test_a_message_that_claims_to_be_from_someone_else_is_refused(self):
        mallory = Person(self, "Mallory")
        envelope = self.envelope(mallory, self.bob, self.payload())
        envelope["from"] = {**self.alice.card()}  # Mallory's seal under Alice's name
        status, _ = self.post(self.bob, envelope)
        self.assertEqual(status, 400)
        self.assertEqual(self.bob.store.count(), 0)

    def test_an_id_that_does_not_match_its_keys_is_refused(self):
        envelope = self.envelope(self.alice, self.bob, self.payload())
        envelope["from"]["id"] = "c" * 32
        self.assertEqual(self.post(self.bob, envelope)[0], 403)

    def test_a_message_for_someone_else_is_refused(self):
        envelope = self.envelope(self.alice, self.bob, self.payload())
        envelope["to"] = Identity.create().user_id
        self.assertEqual(self.post(self.bob, envelope)[0], 400)

    def test_new_keys_under_a_known_id_are_refused(self):
        self.bob.know(self.alice)
        impostor = Person(self, "Impostor")
        card = impostor.card()
        card["id"] = self.alice.identity.user_id  # same id, other keys: the id cannot match them
        envelope = self.envelope(impostor, self.bob, self.payload())
        envelope["from"] = card
        self.assertIn(self.post(self.bob, envelope)[0], (403, 409))
        self.assertEqual(self.bob.store.count(), 0)

    def test_pinned_keys_must_match(self):
        self.bob.store.save_contact({"id": self.alice.identity.user_id, "name": "Alice", "sign": "00" * 32, "dh": "11" * 32})
        status, _ = self.post(self.bob, self.envelope(self.alice, self.bob, self.payload()))
        self.assertEqual(status, 409)

    def test_a_changed_byte_is_refused(self):
        envelope = self.envelope(self.alice, self.bob, self.payload())
        sealed = bytearray(bytes.fromhex(envelope["sealed"]))
        sealed[60] ^= 1
        envelope["sealed"] = bytes(sealed).hex()
        self.assertEqual(self.post(self.bob, envelope)[0], 400)

    def test_a_replayed_message_is_saved_once_and_still_answered_ok(self):
        envelope = self.envelope(self.alice, self.bob, self.payload())
        self.assertEqual(self.post(self.bob, envelope)[0], 200)
        self.assertEqual(self.post(self.bob, envelope)[0], 200)
        self.assertEqual(self.bob.store.count(), 1)

    def test_a_flood_from_one_sender_is_slowed(self):
        statuses = []
        for number in range(PER_MINUTE + 3):
            envelope = self.envelope(self.alice, self.bob, self.payload(id=f"{number:016x}"))
            statuses.append(self.post(self.bob, envelope)[0])
        self.assertEqual(statuses[:PER_MINUTE], [200] * PER_MINUTE)
        self.assertEqual(statuses[PER_MINUTE:], [429] * 3)

    def test_oversized_and_malformed_requests_are_refused(self):
        self.assertEqual(self.post(self.bob, b"not json")[0], 400)
        self.assertEqual(self.post(self.bob, {"v": 1})[0], 400)
        try:
            status = self.post(self.bob, b"x" * (MAX_ENVELOPE + 10))[0]
        except (ssl.SSLError, ConnectionError):
            status = 413  # the listener answered and hung up before the whole upload was sent: the refusal
        self.assertEqual(status, 413)

    def test_text_is_cleaned_and_a_fake_picture_is_refused(self):
        status, _ = self.post(self.bob, self.envelope(self.alice, self.bob, self.payload(text="a\x00b\x07c" + "z" * 5000)))
        self.assertEqual(status, 200)
        (got,) = self.bob.store.history(scope_for(self.alice.identity.user_id))
        self.assertEqual(len(got["body"]["text"]), 4000)
        self.assertTrue(got["body"]["text"].startswith("abc"))
        fake = base64.b64encode(b"<svg onload=alert(1)/>").decode()
        status, _ = self.post(self.bob, self.envelope(self.alice, self.bob, self.payload(id="d" * 16, image=fake)))
        self.assertEqual(status, 400)

    def test_a_wrong_timestamp_is_replaced_by_the_arrival_time(self):
        self.post(self.bob, self.envelope(self.alice, self.bob, self.payload(ts=1.0)))
        (got,) = self.bob.store.history(scope_for(self.alice.identity.user_id))
        self.assertLess(abs(got["ts"] - time.time()), 10)

    def test_only_the_dm_path_exists(self):
        connection = http.client.HTTPSConnection("127.0.0.1", self.bob.service.port, timeout=5, context=ssl._create_unverified_context())
        connection.request("POST", "/elsewhere", body=b"{}")
        self.assertEqual(connection.getresponse().status, 404)
        connection.close()


if __name__ == "__main__":
    unittest.main()


class ReviewDmTests(DmTestCase):
    def payload(self, **extra):
        return {"id": extra.pop("id", "e" * 16), "ts": time.time(), "text": "hi", **extra}

    def test_new_made_up_identities_cannot_flood_one_pc(self):
        from unittest import mock
        from privatepartynet import dm as dm_module

        statuses = []
        with mock.patch.object(dm_module, "NEW_PER_HOUR", 5):
            for number in range(8):
                stranger = Identity.create()
                stranger_person = type("P", (), {"identity": stranger, "card": lambda self_, s=stranger: {**s.card("S"), "port": 1}})()
                statuses.append(self.post(self.bob, self.envelope(stranger_person, self.bob, self.payload(id=f"{number:016x}")))[0])
        self.assertEqual(statuses[:5], [200] * 5)
        self.assertEqual(statuses[5:], [429] * 3)
        self.assertEqual(len(self.bob.store.contacts()), 5)

    def test_one_address_is_limited_whoever_it_claims_to_be(self):
        from unittest import mock
        from privatepartynet import dm as dm_module

        with mock.patch.object(dm_module, "PER_ADDRESS_MINUTE", 3):
            statuses = [self.post(self.bob, self.envelope(self.alice, self.bob, self.payload(id=f"{n:016x}")))[0] for n in range(5)]
        self.assertEqual(statuses, [200, 200, 200, 429, 429])

    def test_a_refused_message_is_marked_not_dropped(self):
        self.alice.know(self.bob)
        self.alice.see(self.bob)
        self.bob.store.save_contact({"id": self.alice.identity.user_id, "name": "Alice", "sign": "00" * 32, "dh": "11" * 32})
        message_id = self.alice.service.send(self.bob.identity.user_id, "will be refused")
        self.assertEqual(self.alice.service.flush(), 0)
        (mine,) = self.alice.store.history(scope_for(self.bob.identity.user_id))
        self.assertTrue(mine["body"]["refused"])
        self.assertEqual(self.alice.store.outbox_count(), 0, "a refusal is not retried for ever")
        self.assertEqual(self.alice.service.events.get_nowait(), ("refused", {"peer": self.bob.identity.user_id, "id": message_id}))
