import unittest

from privatepartynet.smoke import run


class SmokeTests(unittest.TestCase):
    def test_every_page_is_served_over_https(self):
        self.assertEqual(run(), 0)


if __name__ == "__main__":
    unittest.main()
