import json
import tempfile
import unittest

from privatepartynet.settings import DEFAULTS, load_settings, save_settings, settings_path, valid_port


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def test_a_missing_file_gives_the_defaults(self):
        self.assertEqual(load_settings(self.folder), DEFAULTS)

    def test_saved_values_come_back(self):
        save_settings({"port": 9100, "name": "Kitchen PC"}, self.folder)
        self.assertEqual(load_settings(self.folder), {"port": 9100, "name": "Kitchen PC", "theme": "dark", "server_id": None,
                                                      "game_server": None, "friend_relay": False, "friend_relay_mbps": 8,
                                                      "game_open": [], "accent": None, "text_scale": 100,
                                                      "density": "default"})

    def test_a_persons_appearance_comes_back_and_bad_values_fall_back(self):
        save_settings({"accent": "#16A34A", "text_scale": 115, "density": "compact"}, self.folder)
        loaded = load_settings(self.folder)
        self.assertEqual((loaded["accent"], loaded["text_scale"], loaded["density"]), ("#16a34a", 115, "compact"))
        for accent, scale, density in (("green", 300, "huge"), ("#12345", 89, None), (5, True, "")):
            save_settings({"accent": accent, "text_scale": scale, "density": density}, self.folder)
            loaded = load_settings(self.folder)
            self.assertEqual((loaded["accent"], loaded["text_scale"], loaded["density"]), (None, 100, "default"))

    def test_relaying_for_others_is_off_until_turned_on_and_its_limit_is_checked(self):
        self.assertFalse(load_settings(self.folder)["friend_relay"])
        save_settings({"friend_relay": True, "friend_relay_mbps": 20}, self.folder)
        loaded = load_settings(self.folder)
        self.assertEqual((loaded["friend_relay"], loaded["friend_relay_mbps"]), (True, 20))
        for bad in (0, 5000, True, "8"):
            save_settings({"friend_relay": "yes", "friend_relay_mbps": bad}, self.folder)
            loaded = load_settings(self.folder)
            self.assertEqual((loaded["friend_relay"], loaded["friend_relay_mbps"]), (False, 8))

    def test_the_name_defaults_to_none_meaning_this_pc(self):
        self.assertIsNone(load_settings(self.folder)["name"])
        settings_path(self.folder).write_text(json.dumps({"name": "   "}), encoding="utf-8")
        self.assertIsNone(load_settings(self.folder)["name"])

    def test_a_damaged_file_gives_the_defaults(self):
        settings_path(self.folder).write_text("{not json", encoding="utf-8")
        self.assertEqual(load_settings(self.folder), DEFAULTS)

    def test_bad_values_fall_back_one_by_one(self):
        settings_path(self.folder).write_text(json.dumps({"port": 80, "name": "   "}), encoding="utf-8")
        self.assertEqual(load_settings(self.folder), DEFAULTS)

    def test_the_windows_services_opened_to_the_game_network_are_kept_as_a_clean_list(self):
        self.assertEqual(load_settings(self.folder)["game_open"], [], "all closed unless opened")
        settings_path(self.folder).write_text(json.dumps({"game_open": ["sharing", "discovery", "sharing", 7, "a b", "x" * 40]}),
                                              encoding="utf-8")
        self.assertEqual(load_settings(self.folder)["game_open"], ["discovery", "sharing"])
        settings_path(self.folder).write_text(json.dumps({"game_open": "sharing"}), encoding="utf-8")
        self.assertEqual(load_settings(self.folder)["game_open"], [])

    def test_a_boolean_is_not_a_port(self):
        self.assertFalse(valid_port(True))
        self.assertTrue(valid_port(1024))
        self.assertTrue(valid_port(65535))
        self.assertFalse(valid_port(65536))


if __name__ == "__main__":
    unittest.main()
