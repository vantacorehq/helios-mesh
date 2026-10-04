import os
import unittest

import helios as H
from helios import DTNQueue, EnergyScheduler, choose_channel, Router
storage = frames = nav = H


class T(unittest.TestCase):
    def test_rs_any_8_of_12(self):
        data = os.urandom(1000)
        shards, n = storage.encode(data)
        for lost in [(0, 1, 2, 3), (8, 9, 10, 11), (0, 5, 9, 11), (2, 4, 6, 7)]:
            av = {i: s for i, s in enumerate(shards) if i not in lost}
            self.assertEqual(storage.decode(av, n), data)
        with self.assertRaises(ValueError):
            storage.decode({i: shards[i] for i in range(7)}, n)

    def test_cid_stable(self):
        self.assertEqual(storage.cid(b"x"), storage.cid(b"x"))

    def test_frame(self):
        k = b"k" * 16
        f = frames.pack(k, b"AAAA", b"BBBB", 7, b"hi")
        self.assertEqual(frames.unpack(k, f)["payload"], b"hi")
        bad = bytearray(f); bad[3] ^= 1
        with self.assertRaises(ValueError):
            frames.unpack(k, bytes(bad))
        self.assertEqual(frames.unpack(k, frames.forward(f, k))["ttl"], 7)

    def test_routing_avoids_low_soc(self):
        r = Router()
        for n, s in [("A", .9), ("B", .05), ("C", .9), ("E", .9)]:
            r.add_node(n, s)
        r.add_link("A", "B", 1, "lora"); r.add_link("B", "E", 1, "lora")
        r.add_link("A", "C", 2, "lora"); r.add_link("C", "E", 2, "lora")
        self.assertEqual(r.route("A", "E")[0], ["A", "C", "E"])

    def test_energy_hysteresis(self):
        e = EnergyScheduler("eco")
        self.assertEqual(e.update(0.62), "eco")
        self.assertEqual(e.update(0.70), "full")
        self.assertEqual(e.update(0.62), "full")
        self.assertEqual(e.update(0.10), "sleep")

    def test_dtn(self):
        q = DTNQueue()
        q.push("X", b"a", 5); q.push("Y", b"b", 1, expires=10)
        self.assertEqual(q.pop_ready({"X"}).payload, b"a")
        self.assertEqual(q.expire(11), 1)

    def test_channel(self):
        self.assertEqual(choose_channel(100, 5, {"lora", "uhf"}), "uhf")
        self.assertEqual(choose_channel(100, 2000, {"lora", "hf"}), "hf")
        self.assertIsNone(choose_channel(100, 2000, {"lora", "sat"}))

    def test_nav(self):
        self.assertAlmostEqual(nav.julian_date(2000, 1, 1, 12), 2451545.0)
        self.assertAlmostEqual(nav.gmst_deg(2451545.0), 280.46, places=1)
        self.assertTrue(nav.gnss_spoof_suspect(50.0, 55.0))
        self.assertFalse(nav.gnss_spoof_suspect(55.0, 55.1))


if __name__ == "__main__":
    unittest.main()
