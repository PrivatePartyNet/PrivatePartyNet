import time
import unittest

from privatepartynet.cpu import sample


class CpuTests(unittest.TestCase):
    def test_a_reading_is_a_percentage_of_the_whole_pc(self):
        sample()  # the first call only sets the baseline
        time.sleep(0.6)
        value = sample()
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0)

    def test_busy_work_shows_as_cpu_use(self):
        sample()
        end = time.monotonic() + 0.8
        while time.monotonic() < end:
            pass
        self.assertGreater(sample(), 0.0)


class DetailTests(unittest.TestCase):
    def test_the_split_adds_up_and_the_whole_pc_is_a_percentage(self):
        from privatepartynet.cpu import sample_detail

        sample_detail()
        end = time.monotonic() + 0.8
        while time.monotonic() < end:
            pass
        detail = sample_detail()
        self.assertEqual(set(detail), {"app", "own", "engine", "pc"})
        self.assertAlmostEqual(detail["app"], detail["own"] + detail["engine"], delta=0.11)
        self.assertGreater(detail["own"], 0.0)
        for value in detail.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 100.0)
        self.assertGreaterEqual(detail["pc"], detail["app"] - 1.0, "the whole PC cannot be less busy than the app")

    def test_the_figure_is_the_share_of_all_logical_processors(self):
        from privatepartynet import cpu

        self.assertEqual(cpu._share(2.0, 1.0), round(2.0 / cpu._cores * 100, 1))
        self.assertEqual(cpu._share(-1.0, 1.0), 0.0)
        self.assertEqual(cpu._share(10_000.0, 1.0), 100.0)


if __name__ == "__main__":
    unittest.main()
